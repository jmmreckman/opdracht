from fastapi.testclient import TestClient

from app import db, main


def _client():
    c = TestClient(main.app)
    c.post("/login", data={"wachtwoord": "test"})
    return c


def test_login_vereist():
    c = TestClient(main.app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/login"
    assert c.get("/health").json() == {"ok": True}


def test_extern_api_en_paginas():
    c = TestClient(main.app)
    assert c.post("/api/extern/opdrachten", json={"titel": "x", "brief": "y"}).status_code == 401
    r = c.post("/api/extern/opdrachten", headers={"Authorization": "Bearer geheim"},
               json={"titel": "E-bike onderzoek", "type": "onderzoek", "brief": "Beste e-bike tot 1000 euro"})
    oid = r.json()["id"]
    assert c.get(f"/api/extern/opdrachten/{oid}", headers={"Authorization": "Bearer geheim"}).json()["status"] == "actief"

    c = _client()
    with db.get_db() as conn:
        conn.execute("INSERT INTO partijen (opdracht_id, naam, email) VALUES (?, 'A', 'a@a.nl')", (oid,))
        conn.execute("INSERT INTO acties (opdracht_id, partij_id, soort, status, aan, onderwerp, tekst) "
                     "VALUES (?, 1, 'mail', 'wacht', 'a@a.nl', 'Onderwerp', 'Tekst')", (oid,))
        conn.execute("INSERT INTO acties (opdracht_id, soort, status, tekst) VALUES (?, 'vraag', 'wacht', 'Welk budget?')", (oid,))
    for pad in ["/", f"/opdracht/{oid}", f"/opdracht/{oid}/partij/1"]:
        r = c.get(pad)
        assert r.status_code == 200, pad
    pagina = c.get(f"/opdracht/{oid}").text
    assert "Welk budget?" in pagina and "Goedkeuren en versturen" in pagina

    c.post("/actie/1/goedkeuren", data={"onderwerp": "Aangepast", "tekst": "Nieuwe tekst"})
    c.post("/actie/2/beantwoorden", data={"antwoord": "Max 1000"})
    with db.get_db() as conn:
        a = conn.execute("SELECT * FROM acties WHERE id = 1").fetchone()
        b = conn.execute("SELECT * FROM berichten WHERE soort = 'opdrachtgever'").fetchone()
    assert a["status"] == "goedgekeurd" and a["onderwerp"] == "Aangepast"
    assert "Max 1000" in b["tekst"]

    r = c.get("/nieuw", follow_redirects=False)
    assert r.headers["location"].startswith("/intake/")
    assert c.get(r.headers["location"]).status_code == 200


def test_lees_token_mag_alleen_lezen(monkeypatch, tmp_path):
    from app import config, mailer
    monkeypatch.setenv("LEES_TOKEN", "lezen")
    c = TestClient(main.app)
    r = c.post("/api/extern/opdrachten", headers={"Authorization": "Bearer geheim"},
               json={"titel": "Dakkapel", "brief": "x"})
    oid = r.json()["id"]
    config.BIJLAGEN_DIR.mkdir(exist_ok=True)
    (config.BIJLAGEN_DIR / "ab_offerte.pdf").write_bytes(b"%PDF-1.4")
    with db.get_db() as conn:
        conn.execute("INSERT INTO berichten (opdracht_id, richting, van, onderwerp, tekst, bijlagen) VALUES "
                     "(?, 'in', 'a@a.nl', 'Offerte', 'Volledige tekst', ?)",
                     (oid, mailer.dump_bijlagen([{"naam": "offerte.pdf", "type": "application/pdf",
                                                  "bestand": "ab_offerte.pdf", "grootte": 8}])))
    lees = {"Authorization": "Bearer lezen"}
    data = c.get(f"/api/extern/opdrachten/{oid}?alles=1", headers=lees).json()
    assert data["berichten"][0]["tekst"] == "Volledige tekst"
    url = data["berichten"][0]["bijlagen"][0]["url"]
    assert c.get(url.replace("http://testserver", ""), headers=lees).content == b"%PDF-1.4"
    assert c.post(f"/api/extern/opdrachten/{oid}/bericht", headers=lees, json={"tekst": "x"}).status_code == 401
    assert c.post("/api/extern/opdrachten", headers=lees, json={"titel": "x", "brief": "y"}).status_code == 401


def test_logs_endpoint(monkeypatch):
    import logging
    monkeypatch.setenv("LEES_TOKEN", "lezen")
    logging.getLogger("opdracht").warning("testregel voor logs")
    c = TestClient(main.app)
    assert c.get("/api/extern/logs").status_code == 401
    data = c.get("/api/extern/logs", headers={"Authorization": "Bearer lezen"}).json()
    assert any("testregel voor logs" in r for r in data["log"])
