"""Een complete werkronde met een nagebootste Claude: controleert dat tools
de database goed bijwerken en de goedkeuringsregels kloppen."""
from types import SimpleNamespace as NS

from app import agent, claude, db, inbox


class Blok(NS):
    def model_dump(self, **kw):
        return {k: v for k, v in vars(self).items()}


def _antwoord(*blokken, stop="tool_use"):
    usage = NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
               cache_read_input_tokens=5000, server_tool_use=NS(web_search_requests=2))
    return NS(content=list(blokken), stop_reason=stop, usage=usage, model="claude-opus-5")


def _tool(id_, toolnaam, **invoer):
    return Blok(type="tool_use", id=id_, name=toolnaam, input=invoer)


def test_ronde_zoekt_en_zet_mail_klaar(opdracht, monkeypatch):
    stappen = iter([
        _antwoord(Blok(type="text", text="Ik zoek partijen."),
                  _tool("t1", "partij_toevoegen", naam="Dakkapel Direct", email="info@dakkapeldirect.nl",
                        website="https://dakkapeldirect.nl", plaats="Rotterdam")),
        _antwoord(_tool("t2", "mail_sturen", partij_id=1, onderwerp="Offerteaanvraag prefab dakkapel",
                        tekst="Goedendag,\n\nIk zoek ...\n\nMet vriendelijke groet,"),
                  _tool("t3", "mail_sturen", partij_id=99, onderwerp="x", tekst="y"),
                  _tool("t4", "notities_bijwerken", notities="Plan: 10 partijen benaderen.")),
        _antwoord(Blok(type="text", text="Eén partij gevonden en een aanvraag klaargezet."), stop="end_turn"),
    ])
    gezien = []

    def nep_vraag(system, messages, tools, max_tokens=16000):
        gezien.append(messages[-1])
        return next(stappen)

    meldingen = []
    monkeypatch.setattr(claude, "vraag", nep_vraag)
    monkeypatch.setattr(agent.mailer, "meld_eigenaar", lambda o, t: meldingen.append((o, t)))

    samenvatting = agent.draai_ronde(opdracht, "test")
    assert samenvatting == "Eén partij gevonden en een aanvraag klaargezet."

    # tweede aanroep kreeg het tool-resultaat, met een fout voor de onbekende partij
    resultaten = {r["tool_use_id"]: r for r in gezien[2]["content"]}
    assert "wacht op goedkeuring" in resultaten["t2"]["content"]
    assert resultaten["t3"].get("is_error")

    with db.get_db() as c:
        a = c.execute("SELECT * FROM acties").fetchall()
        o = c.execute("SELECT * FROM opdrachten WHERE id = ?", (opdracht,)).fetchone()
        log = c.execute("SELECT tekst FROM logboek WHERE soort = 'ronde'").fetchone()
    assert len(a) == 1 and a[0]["status"] == "wacht" and a[0]["aan"] == "info@dakkapeldirect.nl"
    assert o["notities"] == "Plan: 10 partijen benaderen." and o["ronde_bezig"] == 0
    assert o["zoekopdrachten"] == 6 and o["kosten_usd"] > 0
    assert log["tekst"] == samenvatting
    assert meldingen and "Actie nodig" in meldingen[0][0] and f"[OPD-{opdracht}]" in meldingen[0][0]


def test_eerste_goedkeuren_daarna_zelf(opdracht, monkeypatch):
    with db.get_db() as c:
        c.execute("UPDATE opdrachten SET autonomie = 'eerste_goedkeuren' WHERE id = ?", (opdracht,))
        c.execute("INSERT INTO partijen (opdracht_id, naam, email) VALUES (?, 'A', 'a@a.nl')", (opdracht,))
    r = agent.Ronde(opdracht)
    assert "goedkeuring" in r.t_mail_sturen(1, "x", "y")
    with db.get_db() as c:
        c.execute("UPDATE acties SET status = 'verstuurd'")
    r2 = agent.Ronde(opdracht)
    assert "wordt binnen kantoortijden verstuurd" in r2.t_mail_sturen(1, "Re: x", "z")


def test_eigenaar_kan_niet_als_partij(opdracht):
    with db.get_db() as c:
        c.execute("INSERT INTO partijen (opdracht_id, naam, email) VALUES (?, 'Ik', 'eigenaar@example.com')", (opdracht,))
    try:
        agent.Ronde(opdracht).t_mail_sturen(1, "x", "y")
        assert False
    except agent.ToolFout:
        pass


def test_context_bevat_nieuwe_mail_en_pdf(opdracht):
    with db.get_db() as c:
        c.execute("INSERT INTO partijen (opdracht_id, naam, email) VALUES (?, 'A', 'a@a.nl')", (opdracht,))
    from app import config
    config.BIJLAGEN_DIR.mkdir(exist_ok=True)
    (config.BIJLAGEN_DIR / "x_offerte.pdf").write_bytes(b"%PDF-1.4")
    inbox.verwerk_mail({"van": "a@a.nl", "aan": "", "onderwerp": "Offerte", "tekst": "Zie bijlage",
                        "message_id": "<q@a.nl>", "bijlagen": [{"naam": "offerte.pdf", "type": "application/pdf",
                                                               "bestand": "x_offerte.pdf", "grootte": 8}]})
    blokken, nieuw = agent.bouw_context(opdracht, "test")
    assert blokken[0]["type"] == "document"
    assert "[NIEUW] [bericht 1] IN" in blokken[-1]["text"]
    assert nieuw == [1]


def test_fallback_blokken_gefilterd():
    content = [Blok(type="thinking"), Blok(type="text", text="a"), Blok(type="tool_use", id="x"),
               Blok(type="fallback"), Blok(type="text", text="b")]
    uit = claude.inhoud_voor_geschiedenis(content)
    assert [b.type for b in uit] == ["text", "text"]
