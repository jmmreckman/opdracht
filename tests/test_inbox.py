from app import db, inbox


def _mail(**kw):
    basis = {"van": "info@dakdekker.nl", "aan": "opdracht@steenhub.nl", "onderwerp": "Re: offerte",
             "tekst": "Wij kunnen in week 46.", "message_id": "<m1@dakdekker.nl>", "in_reply_to": "",
             "references": "", "auth_results": "", "automatisch": False, "bijlagen": [], "datum": ""}
    basis.update(kw)
    return basis


def _partij(oid, **kw):
    with db.get_db() as c:
        cur = c.execute("INSERT INTO partijen (opdracht_id, naam, email, website) VALUES (?,?,?,?)",
                        (oid, kw.get("naam", "Dakdekker BV"), kw.get("email", ""), kw.get("website", "")))
        return cur.lastrowid


def test_koppel_via_in_reply_to(opdracht):
    pid = _partij(opdracht, email="andere@adres.nl")
    with db.get_db() as c:
        c.execute("INSERT INTO berichten (opdracht_id, partij_id, richting, message_id) VALUES (?,?,'uit','<ons@steenhub.nl>')",
                  (opdracht, pid))
    u = inbox.verwerk_mail(_mail(van="iemand@gmail.com", in_reply_to="<ons@steenhub.nl>"))
    assert (u["opdracht_id"], u["partij_id"]) == (opdracht, pid)


def test_koppel_via_adres_en_domein(opdracht):
    pid = _partij(opdracht, email="info@dakdekker.nl", website="https://www.dakdekker.nl")
    assert inbox.verwerk_mail(_mail())["partij_id"] == pid
    u = inbox.verwerk_mail(_mail(van="piet@dakdekker.nl", message_id="<m2@x>"))
    assert u["partij_id"] == pid
    with db.get_db() as c:
        assert c.execute("SELECT status FROM partijen WHERE id = ?", (pid,)).fetchone()[0] == "in_gesprek"


def test_dubbele_mail_genegeerd(opdracht):
    inbox.verwerk_mail(_mail())
    assert inbox.verwerk_mail(_mail())["soort"] == "dubbel"


def test_onbekend_bij_enige_actieve_opdracht(opdracht):
    u = inbox.verwerk_mail(_mail(van="x@onbekend.nl"))
    assert u["opdracht_id"] == opdracht and u["partij_id"] is None


def test_eigenaar_met_tag_moet_geverifieerd_zijn(opdracht):
    vals = inbox.verwerk_mail(_mail(van="eigenaar@example.com", onderwerp=f"Re: [OPD-{opdracht}] Actie nodig",
                                    message_id="<e1@x>"))
    assert vals["soort"] == "genegeerd"
    echt = inbox.verwerk_mail(_mail(van="eigenaar@example.com", onderwerp=f"Re: [OPD-{opdracht}] Actie nodig",
                                    message_id="<e2@x>", auth_results="mx.strato.de; dkim=pass header.d=example.com",
                                    tekst="Budget mag tot 12k.\n\nOp 1 okt schreef X:\n> oud"))
    assert echt == {"opdracht_id": opdracht, "partij_id": None, "soort": "opdrachtgever"}
    with db.get_db() as c:
        b = c.execute("SELECT * FROM berichten WHERE soort = 'opdrachtgever'").fetchone()
    assert b["tekst"] == "Budget mag tot 12k."


def test_verzendvenster():
    from datetime import datetime
    from app import config
    assert inbox.binnen_verzendvenster(datetime(2026, 9, 28, 10, 0, tzinfo=config.TZ))      # maandag
    assert not inbox.binnen_verzendvenster(datetime(2026, 9, 28, 22, 0, tzinfo=config.TZ))
    assert not inbox.binnen_verzendvenster(datetime(2026, 9, 27, 10, 0, tzinfo=config.TZ))  # zondag
