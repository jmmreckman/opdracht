"""Bestanden per opdracht: uploaden, lezen door de assistent en meesturen."""
import email

from fastapi.testclient import TestClient

from app import agent, config, db, inbox, mailer, main


def _client():
    c = TestClient(main.app)
    c.post("/login", data={"wachtwoord": "test"})
    return c


def _upload(c, oid, naam="tekening.pdf", inhoud=b"%PDF-1.4 tekening", **velden):
    return c.post(f"/opdracht/{oid}/bestanden", data=velden,
                  files=[("bestanden", (naam, inhoud, "application/pdf"))], follow_redirects=False)


def test_upload_lijst_en_verwijderen(opdracht):
    c = _client()
    assert _upload(c, opdracht, omschrijving="Tekening voorzijde").status_code == 303
    d = db.documenten(opdracht)[0]
    assert d["naam"] == "tekening.pdf" and d["omschrijving"] == "Tekening voorzijde"
    assert d["mag_mee"] == 0 and d["gezien"] == 0
    assert (config.BIJLAGEN_DIR / d["bestand"]).read_bytes() == b"%PDF-1.4 tekening"

    pagina = c.get(f"/opdracht/{opdracht}").text
    assert "tekening.pdf" in pagina and "Tekening voorzijde" in pagina
    assert c.get(f"/bestand/bijlage/{d['bestand']}").content == b"%PDF-1.4 tekening"

    c.post(f"/opdracht/{opdracht}/bestand/{d['id']}/meesturen", data={"mag_mee": "1"})
    assert db.documenten(opdracht)[0]["mag_mee"] == 1
    c.post(f"/opdracht/{opdracht}/bestand/{d['id']}/meesturen", data={})
    assert db.documenten(opdracht)[0]["mag_mee"] == 0

    c.post(f"/opdracht/{opdracht}/bestand/{d['id']}/verwijderen")
    assert db.documenten(opdracht) == []
    assert not (config.BIJLAGEN_DIR / d["bestand"]).exists()


def test_upload_met_bericht_start_ronde(opdracht):
    c = _client()
    _upload(c, opdracht, mag_mee="1", bericht="Stuur deze tekening mee.")
    assert db.documenten(opdracht)[0]["mag_mee"] == 1
    with db.get_db() as conn:
        b = conn.execute("SELECT * FROM berichten WHERE soort = 'opdrachtgever'").fetchone()
        o = conn.execute("SELECT volgende_ronde_at FROM opdrachten WHERE id = ?", (opdracht,)).fetchone()
    assert b["tekst"] == "Stuur deze tekening mee." and o["volgende_ronde_at"]


def test_te_groot_en_andere_opdracht(opdracht, monkeypatch):
    c = _client()
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    assert _upload(c, opdracht, inhoud=b"x" * 11).status_code == 413
    assert db.documenten(opdracht) == []
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 25_000_000)
    _upload(c, opdracht)
    d = db.documenten(opdracht)[0]
    assert c.post(f"/opdracht/{opdracht + 1}/bestand/{d['id']}/verwijderen").status_code == 404


def test_context_toont_nieuw_bestand_eenmalig(opdracht, monkeypatch):
    _upload(_client(), opdracht, omschrijving="Tekening")
    blokken, _ = agent.bouw_context(opdracht, "test")
    assert blokken[0]["type"] == "document" and "tekening.pdf" in blokken[0]["title"]
    assert "[NIEUW, inhoud hierboven] [bestand 1] tekening.pdf" in blokken[-1]["text"]
    assert "NIET meesturen" in blokken[-1]["text"]

    stappen = iter([agent_antwoord()])
    monkeypatch.setattr(agent.claude, "vraag", lambda *a, **k: next(stappen))
    agent.draai_ronde(opdracht, "test")
    assert db.documenten(opdracht)[0]["gezien"] == 1
    blokken, _ = agent.bouw_context(opdracht, "test")
    assert len(blokken) == 1 and "[bestand 1] tekening.pdf" in blokken[0]["text"]
    assert "[NIEUW" not in blokken[0]["text"]


def agent_antwoord():
    from types import SimpleNamespace as NS
    usage = NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
               cache_read_input_tokens=0, server_tool_use=None)
    return NS(content=[NS(type="text", text="Bestand gelezen.")], stop_reason="end_turn",
              usage=usage, model="claude-opus-5")


def test_bestand_bekijken(opdracht):
    c = _client()
    _upload(c, opdracht)
    c.post(f"/opdracht/{opdracht}/bestanden",
           files=[("bestanden", ("tekening.dwg", b"AC1027", "application/octet-stream"))])
    r = agent.Ronde(opdracht)
    blokken = r.t_bestand_bekijken(1)
    assert blokken[0]["type"] == "document"
    assert "kan ik niet lezen" in r.t_bestand_bekijken(2)
    try:
        r.t_bestand_bekijken(99)
        assert False
    except agent.ToolFout:
        pass


def test_mail_met_bijlage_alleen_als_het_mag(opdracht, monkeypatch):
    c = _client()
    _upload(c, opdracht)
    with db.get_db() as conn:
        conn.execute("INSERT INTO partijen (opdracht_id, naam, email) VALUES (?, 'A', 'a@a.nl')", (opdracht,))
    r = agent.Ronde(opdracht)
    try:
        r.t_mail_sturen(1, "Tekening", "Zie bijlage", bijlagen=[1])
        assert False
    except agent.ToolFout as e:
        assert "mag niet mee" in str(e)

    c.post(f"/opdracht/{opdracht}/bestand/1/meesturen", data={"mag_mee": "1"})
    assert "goedkeuring" in agent.Ronde(opdracht).t_mail_sturen(1, "Tekening", "Zie bijlage", bijlagen=[1, 1])
    with db.get_db() as conn:
        a = conn.execute("SELECT * FROM acties").fetchone()
    assert [x["naam"] for x in db.bijlagen(a)] == ["tekening.pdf"]
    assert "tekening.pdf" in c.get(f"/opdracht/{opdracht}").text

    verstuurd = []
    monkeypatch.setattr(mailer, "_smtp_verstuur", lambda msg: verstuurd.append(msg))
    c.post(f"/actie/{a['id']}/goedkeuren", data={})
    with db.get_db() as conn:
        a = conn.execute("SELECT * FROM acties").fetchone()
    assert inbox.voer_actie_uit(a) == "verstuurd"
    msg = email.message_from_bytes(verstuurd[0].as_bytes())
    delen = [d for d in msg.walk() if d.get_filename()]
    assert delen[0].get_filename() == "tekening.pdf"
    assert delen[0].get_payload(decode=True) == b"%PDF-1.4 tekening"
    with db.get_db() as conn:
        b = conn.execute("SELECT * FROM berichten WHERE richting = 'uit'").fetchone()
    assert db.bijlagen(b)[0]["naam"] == "tekening.pdf"

    # Verwijderen na versturen: de kopie bij de verstuurde mail blijft bestaan.
    c.post(f"/opdracht/{opdracht}/bestand/1/verwijderen")
    assert (config.BIJLAGEN_DIR / db.bijlagen(b)[0]["bestand"]).exists()


def test_extern_api_geeft_bestanden(opdracht, monkeypatch):
    monkeypatch.setenv("LEES_TOKEN", "lezen")
    _upload(_client(), opdracht, omschrijving="Tekening")
    c = TestClient(main.app)
    data = c.get(f"/api/extern/opdrachten/{opdracht}", headers={"Authorization": "Bearer lezen"}).json()
    d = data["documenten"][0]
    assert d["naam"] == "tekening.pdf" and d["omschrijving"] == "Tekening"
    assert c.get(d["url"].replace("http://testserver", ""),
                 headers={"Authorization": "Bearer lezen"}).content == b"%PDF-1.4 tekening"


def test_oude_database_krijgt_bijlagen_kolom(tmp_path):
    with db.get_db() as conn:
        conn.executescript("DROP TABLE acties; CREATE TABLE acties (id INTEGER PRIMARY KEY, opdracht_id INTEGER, "
                           "soort TEXT, status TEXT, tekst TEXT);")
    db.init_db()
    with db.get_db() as conn:
        assert "bijlagen" in {r["name"] for r in conn.execute("PRAGMA table_info(acties)")}
