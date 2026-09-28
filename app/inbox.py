"""Binnenkomende mail koppelen aan opdracht + partij, en uitgaande acties
(mails, formulieren) daadwerkelijk uitvoeren."""
import json
import logging
import re
from datetime import datetime

from . import browser, config, db, mailer

logger = logging.getLogger("opdracht")

ALGEMENE_DOMEINEN = {
    "gmail.com", "hotmail.com", "hotmail.nl", "outlook.com", "outlook.nl", "live.nl", "live.com",
    "yahoo.com", "yahoo.nl", "icloud.com", "me.com", "kpnmail.nl", "ziggo.nl", "planet.nl",
    "home.nl", "xs4all.nl", "hetnet.nl", "msn.com", "upcmail.nl", "casema.nl", "proton.me",
    "protonmail.com",
}
OPD_TAG = re.compile(r"\[OPD-(\d+)\]", re.IGNORECASE)


def _domein(adres_of_url: str) -> str:
    s = (adres_of_url or "").lower().strip()
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s


def _eigenaar_geverifieerd(mail: dict) -> bool:
    """Afzenders zijn makkelijk te vervalsen. Staat er een Authentication-Results
    header, dan moet DKIM of DMARC 'pass' zijn. OWNER_MAIL_STRICT=0 zet dit uit."""
    if not config.env_bool("OWNER_MAIL_STRICT", True):
        return True
    ar = (mail.get("auth_results") or "").lower()
    if not ar:
        return False
    return "dkim=pass" in ar or "dmarc=pass" in ar


def verwerk_mail(mail: dict) -> dict:
    """Slaat één binnengekomen mail op op de juiste plek. Geeft terug
    {'opdracht_id', 'partij_id', 'soort'} zodat de planner weet welke
    opdracht een nieuwe ronde nodig heeft."""
    van = mail["van"]
    with db.get_db() as conn:
        if mail.get("message_id"):
            dubbel = conn.execute("SELECT id FROM berichten WHERE message_id = ?",
                                  (mail["message_id"],)).fetchone()
            if dubbel:
                return {"opdracht_id": None, "partij_id": None, "soort": "dubbel"}

    if van in config.eigenaar_emails():
        return _verwerk_eigenaar(mail)

    opdracht_id, partij_id = _zoek_koppeling(mail)
    _sla_op(mail, opdracht_id, partij_id, "mail")
    if opdracht_id and partij_id:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE partijen SET status = 'in_gesprek', updated_at = datetime('now') "
                "WHERE id = ? AND status IN ('gevonden','benaderd','geen_reactie')", (partij_id,))
    return {"opdracht_id": opdracht_id, "partij_id": partij_id, "soort": "partij"}


def _zoek_koppeling(mail: dict) -> tuple[int | None, int | None]:
    ids = set((mail.get("in_reply_to", "") + " " + mail.get("references", "")).split())
    with db.get_db() as conn:
        # 1. Antwoord op een mail die wij stuurden
        for mid in ids:
            b = conn.execute("SELECT opdracht_id, partij_id FROM berichten WHERE message_id = ?",
                             (mid,)).fetchone()
            if b and b["opdracht_id"]:
                return b["opdracht_id"], b["partij_id"]
        # 2. Afzender is het bekende adres van een partij (actieve opdrachten eerst)
        rij = conn.execute(
            "SELECT p.id, p.opdracht_id FROM partijen p JOIN opdrachten o ON o.id = p.opdracht_id "
            "WHERE lower(p.email) = ? ORDER BY (o.status = 'actief') DESC, p.id DESC LIMIT 1",
            (mail["van"],)).fetchone()
        if rij:
            return rij["opdracht_id"], rij["id"]
        # 3. Zelfde bedrijfsdomein als het mailadres of de website van een partij
        dom = _domein(mail["van"])
        if dom and dom not in ALGEMENE_DOMEINEN:
            for p in conn.execute(
                    "SELECT p.id, p.opdracht_id, p.email, p.website FROM partijen p "
                    "JOIN opdrachten o ON o.id = p.opdracht_id "
                    "ORDER BY (o.status = 'actief') DESC, p.id DESC").fetchall():
                if dom in (_domein(p["email"]), _domein(p["website"])):
                    return p["opdracht_id"], p["id"]
        # 4. Maar één actieve uitbesteed-opdracht: daar hoort het vast bij
        actief = conn.execute(
            "SELECT id FROM opdrachten WHERE status = 'actief' AND type = 'uitbesteden'").fetchall()
        if len(actief) == 1:
            return actief[0]["id"], None
    return None, None


def _sla_op(mail: dict, opdracht_id, partij_id, soort: str) -> int:
    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO berichten (opdracht_id, partij_id, richting, soort, van, aan, onderwerp, tekst, "
            "message_id, in_reply_to, references_hdr, automatisch, bijlagen, datum) "
            "VALUES (?, ?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), datetime('now')))",
            (opdracht_id, partij_id, soort, mail["van"], mail.get("aan", ""), mail["onderwerp"],
             mail["tekst"], mail.get("message_id") or None, mail.get("in_reply_to", ""),
             mail.get("references", ""), 1 if mail.get("automatisch") else 0,
             mailer.dump_bijlagen(mail.get("bijlagen", [])), mail.get("datum", "")))
        return cur.lastrowid


def _verwerk_eigenaar(mail: dict) -> dict:
    if not _eigenaar_geverifieerd(mail):
        db.log(None, f"Mail van {mail['van']} genegeerd: afzender niet geverifieerd (DKIM/DMARC). "
                     "Zet OWNER_MAIL_STRICT=0 als dit onterecht is.", "waarschuwing")
        return {"opdracht_id": None, "partij_id": None, "soort": "genegeerd"}
    m = OPD_TAG.search(mail["onderwerp"])
    if m and db.opdracht(int(m.group(1))):
        oid = int(m.group(1))
        mail = {**mail, "tekst": mailer.zonder_citaat(mail["tekst"])}
        _sla_op(mail, oid, None, "opdrachtgever")
        heropen(oid)
        return {"opdracht_id": oid, "partij_id": None, "soort": "opdrachtgever"}
    if re.match(r"^\s*(re|antw|fw|fwd|doorst)\s*:", mail["onderwerp"], re.IGNORECASE):
        # Antwoord op een mail zonder opdrachtnummer (bijv. de dagelijkse
        # samenvatting): geen nieuwe opdracht van maken.
        db.log(None, f"Je mail '{mail['onderwerp']}' kon niet aan een opdracht gekoppeld worden. "
                     "Antwoord op een mail met [OPD-nummer] in het onderwerp, of gebruik de site.",
               "waarschuwing")
        return {"opdracht_id": None, "partij_id": None, "soort": "genegeerd"}
    # Geen opdrachtnummer: dit is een nieuwe opdracht per mail.
    from . import intake
    intake.opdracht_uit_mail(mail["onderwerp"], mailer.zonder_citaat(mail["tekst"]))
    return {"opdracht_id": None, "partij_id": None, "soort": "nieuwe_opdracht"}


def heropen(opdracht_id: int) -> None:
    """Een bericht van jou op een afgeronde opdracht zet hem weer aan."""
    with db.get_db() as conn:
        conn.execute("UPDATE opdrachten SET status = 'actief', rapport_definitief = 0 "
                     "WHERE id = ? AND status = 'afgerond'", (opdracht_id,))


def bericht_van_eigenaar(opdracht_id: int, tekst: str) -> None:
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO berichten (opdracht_id, richting, soort, van, tekst) "
            "VALUES (?, 'in', 'opdrachtgever', 'site', ?)", (opdracht_id, tekst))
    heropen(opdracht_id)
    plan_ronde_nu(opdracht_id)


def plan_ronde_nu(opdracht_id: int, over_minuten: int = 0) -> None:
    with db.get_db() as conn:
        conn.execute(
            "UPDATE opdrachten SET volgende_ronde_at = datetime('now', ?) "
            "WHERE id = ? AND (volgende_ronde_at IS NULL OR volgende_ronde_at > datetime('now', ?))",
            (f"+{over_minuten} minutes", opdracht_id, f"+{over_minuten} minutes"))


# ------------------------------------------------------------ verzenden

def binnen_verzendvenster(nu: datetime | None = None) -> bool:
    nu = nu or datetime.now(config.TZ)
    dagen = {int(d) for d in config.env("VERZEND_DAGEN", "0,1,2,3,4,5").split(",") if d.strip().isdigit()}
    start = config.env_int("VERZEND_VAN_UUR", 8)
    eind = config.env_int("VERZEND_TOT_UUR", 19)
    return nu.weekday() in dagen and start <= nu.hour < eind


def volgende_actie():
    """De oudste goedgekeurde actie, als het verzendtempo het toelaat."""
    interval = config.env_int("VERZEND_INTERVAL_SEC", 120)
    with db.get_db() as conn:
        laatste = conn.execute(
            "SELECT MAX(uitgevoerd_at) FROM acties WHERE soort IN ('mail','formulier') "
            "AND uitgevoerd_at IS NOT NULL").fetchone()[0]
        if laatste:
            verschil = (datetime.utcnow() - datetime.strptime(laatste, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if verschil < interval:
                return None
        return conn.execute(
            "SELECT a.* FROM acties a JOIN opdrachten o ON o.id = a.opdracht_id "
            "WHERE a.status = 'goedgekeurd' AND a.soort IN ('mail','formulier') "
            "AND o.status = 'actief' ORDER BY a.id LIMIT 1").fetchone()


def voer_actie_uit(actie) -> str:
    """Verstuurt een goedgekeurde mail of formulier. Geeft de nieuwe status terug."""
    if actie["soort"] == "mail":
        return _verstuur_mail(actie)
    return _verstuur_formulier(actie)


def _verstuur_mail(a) -> str:
    in_reply_to, references = "", ""
    if a["antwoord_op"]:
        with db.get_db() as conn:
            b = conn.execute("SELECT message_id, references_hdr FROM berichten WHERE id = ?",
                             (a["antwoord_op"],)).fetchone()
        if b and b["message_id"]:
            in_reply_to, references = b["message_id"], b["references_hdr"]
    try:
        message_id = mailer.verstuur(a["aan"], a["onderwerp"], a["tekst"], in_reply_to, references)
    except mailer.MailFout as e:
        _zet(a["id"], "mislukt", str(e))
        db.log(a["opdracht_id"], f"Mail aan {a['aan']} mislukt: {e}", "fout")
        return "mislukt"
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO berichten (opdracht_id, partij_id, richting, soort, van, aan, onderwerp, tekst, "
            "message_id, in_reply_to, references_hdr, verwerkt) VALUES (?, ?, 'uit', 'mail', ?, ?, ?, ?, ?, ?, ?, 1)",
            (a["opdracht_id"], a["partij_id"], config.opdracht_email(), a["aan"], a["onderwerp"],
             a["tekst"], message_id, in_reply_to, references))
        conn.execute("UPDATE partijen SET status = 'benaderd', updated_at = datetime('now') "
                     "WHERE id = ? AND status = 'gevonden'", (a["partij_id"],))
    _zet(a["id"], "verstuurd", "")
    return "verstuurd"


def _verstuur_formulier(a) -> str:
    try:
        f = json.loads(a["formulier"] or "{}")
        uitkomst = browser.formulier_invullen(f["url"], f.get("velden", []), f.get("verstuur_selector", ""))
    except Exception as e:  # noqa: BLE001
        _zet(a["id"], "mislukt", f"Browserfout: {e}")
        db.log(a["opdracht_id"], f"Formulier {a['aan']} mislukt: {e}", "fout")
        return "mislukt"
    status = uitkomst["status"]
    melding = "; ".join(x for x in [uitkomst.get("melding", ""), *uitkomst.get("fouten", [])] if x)
    if status == "captcha":
        _zet(a["id"], "handmatig", melding, uitkomst.get("screenshot", ""))
        mailer.meld_eigenaar(
            f"[OPD-{a['opdracht_id']}] Formulier handmatig versturen",
            f"Het formulier op {a['aan']} heeft een captcha. Wil je het zelf even invullen?\n\n"
            f"In te vullen:\n{a['tekst']}\n\nDaarna op de site op 'Zelf verstuurd' klikken: "
            f"{config.site_url()}/opdracht/{a['opdracht_id']}")
        return "handmatig"
    if status == "fout":
        _zet(a["id"], "mislukt", melding, uitkomst.get("screenshot", ""))
        return "mislukt"
    if status == "onzeker":
        melding = "Verstuurd, maar geen bevestiging gezien - controleer de schermafbeelding. " + melding
    registreer_formulier_verstuurd(a, melding, uitkomst.get("screenshot", ""))
    return "verstuurd"


def registreer_formulier_verstuurd(a, melding: str = "", screenshot: str = "") -> None:
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO berichten (opdracht_id, partij_id, richting, soort, van, aan, onderwerp, tekst, verwerkt) "
            "VALUES (?, ?, 'uit', 'formulier', ?, ?, ?, ?, 1)",
            (a["opdracht_id"], a["partij_id"], config.opdracht_email(), a["aan"], a["onderwerp"], a["tekst"]))
        conn.execute("UPDATE partijen SET status = 'benaderd', updated_at = datetime('now') "
                     "WHERE id = ? AND status = 'gevonden'", (a["partij_id"],))
    _zet(a["id"], "verstuurd", melding, screenshot)


def _zet(actie_id: int, status: str, resultaat: str, screenshot: str = "") -> None:
    with db.get_db() as conn:
        conn.execute(
            "UPDATE acties SET status = ?, resultaat = ?, screenshot = COALESCE(NULLIF(?, ''), screenshot), "
            "uitgevoerd_at = datetime('now') WHERE id = ?", (status, resultaat, screenshot, actie_id))
