"""De werkronde: Claude krijgt de actuele stand van een opdracht en beslist
wat er moet gebeuren (partijen zoeken, mailen, formulieren, vragen aan jou,
rapporteren). Alles wat de deur uit gaat loopt via de tabel `acties`, zodat
goedkeuring en verzendtempo in de code geregeld zijn en niet in de prompt."""
import base64
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from . import browser, claude, config, db, mailer
from .prompts import WERK_SYSTEEM

logger = logging.getLogger("opdracht")

MAX_STAPPEN = 30
MAX_BERICHTEN_PER_PARTIJ_PER_RONDE = 2
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
PARTIJ_STATUSSEN = ["gevonden", "benaderd", "in_gesprek", "offerte_ontvangen",
                    "geen_reactie", "afgevallen", "favoriet"]
EMAIL_OK = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _tool(naam, beschrijving, eigenschappen, verplicht):
    return {
        "name": naam,
        "description": beschrijving,
        "input_schema": {
            "type": "object",
            "properties": eigenschappen,
            "required": verplicht,
            "additionalProperties": False,
        },
    }


S = {"type": "string"}
I = {"type": "integer"}

EIGEN_TOOLS = [
    _tool("website_bekijken",
          "Open een webpagina in een echte browser. Geeft de zichtbare tekst, gevonden "
          "e-mailadressen, telefoonnummers, links naar contact/offerte-pagina's en of er een "
          "formulier (en captcha) op staat. Gebruik dit om contactgegevens te vinden.",
          {"url": S}, ["url"]),
    _tool("formulier_bekijken",
          "Bekijk de invulvelden van een contact-/offerteformulier: selector, type, label, "
          "verplicht, keuzeopties, en de verstuurknop. Nodig vóór formulier_invullen.",
          {"url": S}, ["url"]),
    _tool("partij_toevoegen",
          "Leg een gevonden bedrijf/partij vast bij deze opdracht.",
          {"naam": S, "website": S, "email": S, "telefoon": S, "formulier_url": S,
           "plaats": S, "notitie": {**S, "description": "Waarom geschikt, reviews, bijzonderheden."}},
          ["naam"]),
    _tool("partij_bijwerken",
          "Werk gegevens of status van een partij bij, bijvoorbeeld een gevonden mailadres, "
          "een ontvangen offerte, of afvallen (met reden in notitie).",
          {"partij_id": I, "naam": S, "website": S, "email": S, "telefoon": S,
           "formulier_url": S, "plaats": S, "notitie": S,
           "status": {"type": "string", "enum": PARTIJ_STATUSSEN},
           "offerte_bedrag": {**S, "description": "Bijv. '€ 8.950 incl. btw'."},
           "offerte_samenvatting": {**S, "description": "Wat zit erin, planning, voorwaarden, geldigheid."}},
          ["partij_id"]),
    _tool("mail_sturen",
          "Stuur een mail aan een partij (naar het e-mailadres van die partij). Afhankelijk "
          "van de autonomie-instelling wacht de mail eerst op goedkeuring van de "
          "opdrachtgever; het resultaat zegt wat er gebeurt. Mails gaan alleen binnen "
          "kantoortijden de deur uit.",
          {"partij_id": I, "onderwerp": S,
           "tekst": {**S, "description": "Volledige mailtekst inclusief aanhef en ondertekening."},
           "antwoord_op_bericht_id": {**I, "description": "Id van het ontvangen bericht waarop je reageert (voor de mailthread)."}},
          ["partij_id", "onderwerp", "tekst"]),
    _tool("formulier_invullen",
          "Vul een contact-/offerteformulier van een partij in en verstuur het. Gebruik "
          "de selectors uit formulier_bekijken. Wacht eventueel eerst op goedkeuring.",
          {"partij_id": I, "url": S,
           "velden": {"type": "array", "items": {
               "type": "object",
               "properties": {"selector": S, "waarde": S, "label": S},
               "required": ["selector", "waarde"], "additionalProperties": False}},
           "verstuur_selector": S,
           "samenvatting": {**S, "description": "Kort: wat je in het formulier vraagt (voor de opdrachtgever)."}},
          ["partij_id", "url", "velden", "samenvatting"]),
    _tool("vraag_opdrachtgever",
          "Leg een vraag of beslissing voor aan de opdrachtgever. Die krijgt een mail en "
          "antwoordt via de site of per mail; daarna start een nieuwe ronde.",
          {"vraag": S}, ["vraag"]),
    _tool("bericht_koppelen",
          "Koppel een binnengekomen bericht dat nog niet aan een partij hangt aan de juiste "
          "partij (bijv. een reactie via een ander adres).",
          {"bericht_id": I, "partij_id": I}, ["bericht_id", "partij_id"]),
    _tool("notities_bijwerken",
          "Vervang je werkgeheugen voor deze opdracht (plan, stand van zaken, afspraken, "
          "wat je nog moet doen). Dit is het enige dat je onthoudt tussen rondes, naast "
          "partijen en berichten. Schrijf het volledig, niet alleen de wijziging.",
          {"notities": S}, ["notities"]),
    _tool("rapport_opslaan",
          "Sla het (tussen)rapport op in markdown. definitief=true betekent: opdracht klaar, "
          "rapport wordt naar de opdrachtgever gemaild.",
          {"rapport": S, "definitief": {"type": "boolean"}}, ["rapport", "definitief"]),
    _tool("volgende_ronde",
          "Plan wanneer je deze opdracht zelf weer wilt oppakken (naast de vaste "
          "dagelijkse rondes en nieuwe mail).",
          {"over_uren": {"type": "number"}, "reden": S}, ["over_uren"]),
]


# ------------------------------------------------------------------ context

def _lokaal(utc_str: str | None) -> str:
    if not utc_str:
        return "?"
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    return dt.astimezone(config.TZ).strftime("%d-%m %H:%M")


def _knip(tekst: str, max_tekens: int) -> str:
    tekst = (tekst or "").strip()
    return tekst if len(tekst) <= max_tekens else tekst[:max_tekens] + "\n[... ingekort]"


def _bericht_regel(b, nieuw: bool) -> str:
    kop = "UIT" if b["richting"] == "uit" else "IN"
    soort = " (formulier)" if b["soort"] == "formulier" else ""
    auto = " (automatisch antwoord)" if b["automatisch"] else ""
    label = "[NIEUW] " if nieuw else ""
    tekst = mailer.zonder_citaat(b["tekst"]) if b["richting"] == "in" else b["tekst"]
    tekst = _knip(tekst, 6000 if nieuw else 1500)
    bijl = db.bijlagen(b)
    bijl_txt = ("\nBijlagen: " + ", ".join(x["naam"] for x in bijl)) if bijl else ""
    return (f"{label}[bericht {b['id']}] {kop}{soort}{auto} {_lokaal(b['datum'])} "
            f"van {b['van'] or '-'} aan {b['aan'] or '-'}\nOnderwerp: {b['onderwerp']}\n"
            f"{tekst}{bijl_txt}\n")


def bouw_context(opdracht_id: int, reden: str) -> tuple[list, list[int]]:
    """Het gebruikersbericht voor een ronde, plus de id's van de berichten
    die als [NIEUW] zijn meegegeven (die worden na de ronde 'verwerkt')."""
    with db.get_db() as conn:
        o = conn.execute("SELECT * FROM opdrachten WHERE id = ?", (opdracht_id,)).fetchone()
        partijen = conn.execute(
            "SELECT * FROM partijen WHERE opdracht_id = ? ORDER BY id", (opdracht_id,)).fetchall()
        berichten = conn.execute(
            "SELECT * FROM berichten WHERE opdracht_id = ? ORDER BY datum, id", (opdracht_id,)).fetchall()
        acties = conn.execute(
            "SELECT a.*, p.naam AS partij_naam FROM acties a LEFT JOIN partijen p ON p.id = a.partij_id "
            "WHERE a.opdracht_id = ? ORDER BY a.id", (opdracht_id,)).fetchall()
        logs = conn.execute(
            "SELECT * FROM logboek WHERE opdracht_id = ? AND soort = 'ronde' "
            "ORDER BY id DESC LIMIT 5", (opdracht_id,)).fetchall()

    nu = datetime.now(config.TZ)
    afzender = config.opdracht_email()
    naam = config.afzender_naam()
    delen = [
        "# Situatie",
        f"Nu: {DAGEN[nu.weekday()]} {nu.strftime('%d-%m-%Y %H:%M')} (Nederlandse tijd)",
        f"Reden van deze ronde: {reden}",
        f"Afzender (jouw mailbox): {afzender}",
        f"Afzendernaam voor ondertekening en formulieren: {naam or '(geen, alleen groet)'}",
        "",
        f"# Opdracht {o['id']}: {o['titel']}",
        f"Type: {o['type']} | Status: {o['status']} | Autonomie: {o['autonomie']}",
        "", "## Omschrijving", o["brief"] or "-",
        "", "## Harde eisen en criteria", o["criteria"] or "-",
        "", "## Mag gedeeld worden met partijen", o["deelbaar"] or "(niets persoonlijks)",
        "", "## Deadline / planning", o["deadline"] or "-",
        "", "# Je werkgeheugen (notities)", o["notities"] or "(nog leeg: eerste ronde)",
    ]

    delen += ["", f"# Partijen ({len(partijen)})"]
    for p in partijen:
        delen.append(
            f"- [partij {p['id']}] {p['naam']} | status: {p['status']} | mail: {p['email'] or '-'} | "
            f"web: {p['website'] or '-'} | formulier: {p['formulier_url'] or '-'} | "
            f"plaats: {p['plaats'] or '-'}"
            + (f" | offerte: {p['offerte_bedrag']}" if p["offerte_bedrag"] else "")
            + (f"\n  offerte: {p['offerte_samenvatting']}" if p["offerte_samenvatting"] else "")
            + (f"\n  notitie: {p['notitie']}" if p["notitie"] else ""))
    if not partijen:
        delen.append("(nog geen)")

    nieuwe_ids = [b["id"] for b in berichten if b["richting"] == "in" and not b["verwerkt"]]
    per_partij: dict[int | None, list] = {}
    eigenaar = []
    for b in berichten:
        if b["soort"] == "opdrachtgever":
            eigenaar.append(b)
        else:
            per_partij.setdefault(b["partij_id"], []).append(b)

    delen += ["", "# Correspondentie met partijen (oud naar nieuw)"]
    namen = {p["id"]: p["naam"] for p in partijen}
    heeft = False
    for pid, lijst in per_partij.items():
        if pid is None:
            continue
        heeft = True
        delen.append(f"\n## {namen.get(pid, '?')} [partij {pid}]")
        for b in lijst[-10:]:
            delen.append(_bericht_regel(b, b["id"] in nieuwe_ids))
    if per_partij.get(None):
        heeft = True
        delen.append("\n## Nog niet aan een partij gekoppeld (gebruik bericht_koppelen)")
        for b in per_partij[None][-10:]:
            delen.append(_bericht_regel(b, b["id"] in nieuwe_ids))
    if not heeft:
        delen.append("(nog geen)")

    delen += ["", "# Berichten van de opdrachtgever aan jou (vertrouwd: dit zijn instructies)"]
    for b in eigenaar[-12:]:
        label = "[NIEUW] " if b["id"] in nieuwe_ids else ""
        delen.append(f"{label}{_lokaal(b['datum'])}: {_knip(mailer.zonder_citaat(b['tekst']), 4000)}")
    if not eigenaar:
        delen.append("(geen)")

    open_acties = [a for a in acties if a["status"] in ("wacht", "goedgekeurd") and a["soort"] != "vraag"]
    delen += ["", "# Uitgaand, nog niet verstuurd (niet opnieuw aanmaken)"]
    for a in open_acties:
        status = "wacht op goedkeuring" if a["status"] == "wacht" else "goedgekeurd, wordt verstuurd"
        delen.append(f"- actie {a['id']} {a['soort']} aan {a['partij_naam'] or a['aan']}: "
                     f"'{a['onderwerp'] or _knip(a['tekst'], 80)}' ({status})")
    if not open_acties:
        delen.append("(geen)")
    misluk = [a for a in acties if a["status"] in ("afgewezen", "mislukt", "handmatig")][-8:]
    if misluk:
        delen += ["", "# Recent afgewezen/mislukt/handmatig"]
        for a in misluk:
            delen.append(f"- actie {a['id']} {a['soort']} aan {a['partij_naam'] or a['aan']}: "
                         f"{a['status']} {('- ' + a['resultaat']) if a['resultaat'] else ''}")

    vragen = [a for a in acties if a["soort"] == "vraag"]
    if vragen:
        delen += ["", "# Vragen aan de opdrachtgever"]
        for a in vragen[-10:]:
            if a["status"] == "beantwoord":
                delen.append(f"- Vraag: {a['tekst']}\n  Antwoord: {a['antwoord']}")
            elif a["status"] == "wacht":
                delen.append(f"- Vraag (nog geen antwoord): {a['tekst']}")

    if o["rapport"]:
        delen += ["", "# Huidig rapport" + (" (definitief)" if o["rapport_definitief"] else " (tussenstand)"),
                  _knip(o["rapport"], 6000)]
    if logs:
        delen += ["", "# Samenvattingen van eerdere rondes (nieuwste eerst)"]
        for lg in logs:
            delen.append(f"- {_lokaal(lg['created_at'])}: {lg['tekst']}")

    delen += ["", "Bepaal nu wat er in deze ronde moet gebeuren en voer het uit."]

    blokken = _bijlage_blokken([b for b in berichten if b["id"] in nieuwe_ids])
    blokken.append({"type": "text", "text": "\n".join(delen)})
    return blokken, nieuwe_ids


def _bijlage_blokken(berichten) -> list:
    """PDF's en afbeeldingen uit nieuwe mails (offertes!) direct meesturen."""
    blokken, aantal = [], 0
    for b in berichten:
        for bijl in db.bijlagen(b):
            if aantal >= 4:
                return blokken
            pad = config.BIJLAGEN_DIR / bijl["bestand"]
            if not pad.exists():
                continue
            data = pad.read_bytes()
            typ = bijl["type"].lower()
            if bijl["naam"].lower().endswith(".pdf"):
                typ = "application/pdf"
            if typ == "application/pdf" and len(data) < 15_000_000:
                blokken.append({"type": "document", "title": f"{bijl['naam']} (bij bericht {b['id']})",
                                "source": {"type": "base64", "media_type": "application/pdf",
                                           "data": base64.standard_b64encode(data).decode()}})
                aantal += 1
            elif typ in ("image/jpeg", "image/png", "image/gif", "image/webp") and len(data) < 4_000_000:
                blokken.append({"type": "image", "source": {
                    "type": "base64", "media_type": typ,
                    "data": base64.standard_b64encode(data).decode()}})
                blokken.append({"type": "text", "text": f"(afbeelding hierboven: {bijl['naam']} bij bericht {b['id']})"})
                aantal += 1
    return blokken


# ------------------------------------------------------------------ tools

class Ronde:
    def __init__(self, opdracht_id: int):
        self.opdracht_id = opdracht_id
        self.berichten_per_partij: dict[int, int] = {}
        self.nieuw_wacht = 0
        self.nieuwe_vragen: list[str] = []
        self.rapport_definitief = False
        self.volgende_ronde_gezet = False

    def opdracht(self):
        return db.opdracht(self.opdracht_id)

    def _partij(self, conn, partij_id):
        p = conn.execute("SELECT * FROM partijen WHERE id = ? AND opdracht_id = ?",
                         (partij_id, self.opdracht_id)).fetchone()
        if p is None:
            raise ToolFout(f"Partij {partij_id} hoort niet bij deze opdracht.")
        return p

    def voer_uit(self, naam: str, invoer: dict) -> str:
        functie = getattr(self, f"t_{naam}", None)
        if functie is None:
            raise ToolFout(f"Onbekende tool {naam}")
        return functie(**invoer)

    # -- browser
    def t_website_bekijken(self, url):
        return json.dumps(browser.website_bekijken(url), ensure_ascii=False)

    def t_formulier_bekijken(self, url):
        info = browser.formulier_bekijken(url)
        return json.dumps(info, ensure_ascii=False)[:15000]

    # -- partijen
    def t_partij_toevoegen(self, naam, website="", email="", telefoon="", formulier_url="",
                           plaats="", notitie=""):
        email = email.strip().lower()
        with db.get_db() as conn:
            dubbel = conn.execute(
                "SELECT id FROM partijen WHERE opdracht_id = ? AND (lower(naam) = lower(?) "
                "OR (? != '' AND lower(email) = ?) OR (? != '' AND website = ?))",
                (self.opdracht_id, naam, email, email, website, website)).fetchone()
            if dubbel:
                return f"Bestaat al als partij {dubbel['id']}; gebruik partij_bijwerken."
            cur = conn.execute(
                "INSERT INTO partijen (opdracht_id, naam, website, email, telefoon, formulier_url, "
                "plaats, notitie) VALUES (?,?,?,?,?,?,?,?)",
                (self.opdracht_id, naam, website, email, telefoon, formulier_url, plaats, notitie))
            return f"Partij {cur.lastrowid} toegevoegd."

    def t_partij_bijwerken(self, partij_id, **velden):
        toegestaan = {"naam", "website", "email", "telefoon", "formulier_url", "plaats", "notitie",
                      "status", "offerte_bedrag", "offerte_samenvatting"}
        velden = {k: (v.strip().lower() if k == "email" else v) for k, v in velden.items()
                  if k in toegestaan and v is not None}
        if not velden:
            return "Niets bij te werken."
        with db.get_db() as conn:
            self._partij(conn, partij_id)
            sets = ", ".join(f"{k} = ?" for k in velden)
            conn.execute(f"UPDATE partijen SET {sets}, updated_at = datetime('now') WHERE id = ?",
                         (*velden.values(), partij_id))
        return f"Partij {partij_id} bijgewerkt."

    # -- uitgaand
    def _moet_goedkeuren(self, conn, partij_id) -> bool:
        autonomie = self.opdracht()["autonomie"]
        if autonomie == "alles_goedkeuren":
            return True
        if autonomie == "zelfstandig":
            return False
        eerder = conn.execute(
            "SELECT 1 FROM acties WHERE partij_id = ? AND soort IN ('mail','formulier') "
            "AND status IN ('verstuurd','goedgekeurd') LIMIT 1", (partij_id,)).fetchone()
        return eerder is None

    def _check_limieten(self, conn, partij_id):
        n = self.berichten_per_partij.get(partij_id, 0)
        if n >= MAX_BERICHTEN_PER_PARTIJ_PER_RONDE:
            raise ToolFout("Je hebt deze partij deze ronde al genoeg berichten gestuurd.")
        max_dag = config.env_int("MAX_UITGAAND_PER_DAG", 30)
        vandaag = conn.execute(
            "SELECT COUNT(*) FROM acties WHERE opdracht_id = ? AND soort IN ('mail','formulier') "
            "AND created_at >= datetime('now', '-1 day')", (self.opdracht_id,)).fetchone()[0]
        if vandaag >= max_dag:
            raise ToolFout(f"Daglimiet van {max_dag} uitgaande berichten voor deze opdracht bereikt; "
                           "wacht tot een volgende ronde.")

    def t_mail_sturen(self, partij_id, onderwerp, tekst, antwoord_op_bericht_id=None):
        with db.get_db() as conn:
            p = self._partij(conn, partij_id)
            aan = (p["email"] or "").strip().lower()
            if not EMAIL_OK.match(aan):
                raise ToolFout("Deze partij heeft geen geldig e-mailadres; zoek het op en gebruik "
                               "partij_bijwerken, of gebruik het formulier.")
            if aan in config.eigenaar_emails() or aan == config.opdracht_email().lower():
                raise ToolFout("Dit adres mag niet als partij gemaild worden.")
            self._check_limieten(conn, partij_id)
            if antwoord_op_bericht_id:
                b = conn.execute("SELECT id FROM berichten WHERE id = ? AND opdracht_id = ? AND richting = 'in'",
                                 (antwoord_op_bericht_id, self.opdracht_id)).fetchone()
                if b is None:
                    antwoord_op_bericht_id = None
            wacht = self._moet_goedkeuren(conn, partij_id)
            conn.execute(
                "INSERT INTO acties (opdracht_id, partij_id, soort, status, aan, onderwerp, tekst, antwoord_op) "
                "VALUES (?, ?, 'mail', ?, ?, ?, ?, ?)",
                (self.opdracht_id, partij_id, "wacht" if wacht else "goedgekeurd", aan, onderwerp,
                 tekst, antwoord_op_bericht_id))
        self.berichten_per_partij[partij_id] = self.berichten_per_partij.get(partij_id, 0) + 1
        if wacht:
            self.nieuw_wacht += 1
            return "Mail klaargezet; wacht op goedkeuring van de opdrachtgever."
        return "Mail goedgekeurd; wordt binnen kantoortijden verstuurd."

    def t_formulier_invullen(self, partij_id, url, velden, samenvatting, verstuur_selector=""):
        with db.get_db() as conn:
            p = self._partij(conn, partij_id)
            self._check_limieten(conn, partij_id)
            wacht = self._moet_goedkeuren(conn, partij_id)
            leesbaar = "\n".join(f"{v.get('label') or v['selector']}: {v['waarde']}" for v in velden)
            conn.execute(
                "INSERT INTO acties (opdracht_id, partij_id, soort, status, aan, onderwerp, tekst, formulier) "
                "VALUES (?, ?, 'formulier', ?, ?, ?, ?, ?)",
                (self.opdracht_id, partij_id, "wacht" if wacht else "goedgekeurd", url,
                 f"Formulier {p['naam']}: {samenvatting}"[:200], leesbaar,
                 json.dumps({"url": url, "velden": velden, "verstuur_selector": verstuur_selector},
                            ensure_ascii=False)))
            if not p["formulier_url"]:
                conn.execute("UPDATE partijen SET formulier_url = ? WHERE id = ?", (url, partij_id))
        self.berichten_per_partij[partij_id] = self.berichten_per_partij.get(partij_id, 0) + 1
        if wacht:
            self.nieuw_wacht += 1
            return "Formulier klaargezet; wacht op goedkeuring van de opdrachtgever."
        return "Formulier goedgekeurd; wordt binnen kantoortijden ingevuld en verstuurd."

    def t_vraag_opdrachtgever(self, vraag):
        with db.get_db() as conn:
            conn.execute("INSERT INTO acties (opdracht_id, soort, status, tekst) VALUES (?, 'vraag', 'wacht', ?)",
                         (self.opdracht_id, vraag))
        self.nieuwe_vragen.append(vraag)
        return "Vraag is uitgezet bij de opdrachtgever. Ga door met wat zonder antwoord kan."

    def t_bericht_koppelen(self, bericht_id, partij_id):
        with db.get_db() as conn:
            self._partij(conn, partij_id)
            cur = conn.execute("UPDATE berichten SET partij_id = ? WHERE id = ? AND opdracht_id = ?",
                               (partij_id, bericht_id, self.opdracht_id))
            if not cur.rowcount:
                raise ToolFout("Bericht niet gevonden in deze opdracht.")
            p = conn.execute("SELECT email FROM partijen WHERE id = ?", (partij_id,)).fetchone()
            b = conn.execute("SELECT van FROM berichten WHERE id = ?", (bericht_id,)).fetchone()
            if not p["email"] and b["van"]:
                conn.execute("UPDATE partijen SET email = ? WHERE id = ?", (b["van"], partij_id))
        return "Gekoppeld."

    # -- geheugen en resultaat
    def t_notities_bijwerken(self, notities):
        with db.get_db() as conn:
            conn.execute("UPDATE opdrachten SET notities = ?, updated_at = datetime('now') WHERE id = ?",
                         (notities, self.opdracht_id))
        return "Werkgeheugen opgeslagen."

    def t_rapport_opslaan(self, rapport, definitief):
        with db.get_db() as conn:
            conn.execute("UPDATE opdrachten SET rapport = ?, rapport_definitief = ?, updated_at = datetime('now') "
                         "WHERE id = ?", (rapport, 1 if definitief else 0, self.opdracht_id))
        if definitief:
            self.rapport_definitief = True
            return "Definitief rapport opgeslagen; wordt na deze ronde naar de opdrachtgever gemaild."
        return "Tussenrapport opgeslagen."

    def t_volgende_ronde(self, over_uren, reden=""):
        uren = max(0.5, min(float(over_uren), 24 * 14))
        moment = (datetime.utcnow() + timedelta(hours=uren)).strftime("%Y-%m-%d %H:%M:%S")
        with db.get_db() as conn:
            conn.execute("UPDATE opdrachten SET volgende_ronde_at = ? WHERE id = ?", (moment, self.opdracht_id))
        self.volgende_ronde_gezet = True
        return f"Volgende ronde gepland over {uren:g} uur."


class ToolFout(Exception):
    pass


# ------------------------------------------------------------------ de ronde

def claim(opdracht_id: int) -> bool:
    with db.get_db() as conn:
        cur = conn.execute("UPDATE opdrachten SET ronde_bezig = 1 WHERE id = ? AND ronde_bezig = 0",
                           (opdracht_id,))
        return cur.rowcount == 1


def draai_ronde(opdracht_id: int, reden: str) -> str:
    """Voert één werkronde uit. Geeft de samenvatting terug."""
    if not claim(opdracht_id):
        return "Er loopt al een ronde voor deze opdracht."
    try:
        return _draai(opdracht_id, reden)
    except Exception as e:  # noqa: BLE001
        logger.exception("Ronde voor opdracht %s mislukt", opdracht_id)
        db.log(opdracht_id, f"Ronde mislukt: {e}", "fout")
        return f"Ronde mislukt: {e}"
    finally:
        with db.get_db() as conn:
            conn.execute("UPDATE opdrachten SET ronde_bezig = 0 WHERE id = ?", (opdracht_id,))


def _draai(opdracht_id: int, reden: str) -> str:
    ronde = Ronde(opdracht_id)
    blokken, nieuwe_ids = bouw_context(opdracht_id, reden)
    messages = [{"role": "user", "content": blokken}]
    totaal = {"tokens_in": 0, "tokens_uit": 0, "zoekopdrachten": 0, "usd": 0.0}
    samenvatting = ""
    stappen = 0
    for stappen in range(1, MAX_STAPPEN + 1):
        resp = claude.vraag(WERK_SYSTEEM, messages, claude.WEB_TOOLS + EIGEN_TOOLS)
        for k, v in claude.kosten(resp).items():
            totaal[k] += v
        if resp.stop_reason == "refusal":
            samenvatting = "De AI weigerde deze ronde uit te voeren (veiligheidsfilter)."
            break
        inhoud = claude.inhoud_voor_geschiedenis(resp.content)
        messages.append({"role": "assistant", "content": inhoud})
        tekst = claude.tekst_van(inhoud)
        if tekst:
            samenvatting = tekst
        if resp.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in inhoud if b.type == "tool_use"]
        if not tool_uses:
            break
        resultaten = []
        for tu in tool_uses:
            try:
                uitkomst = ronde.voer_uit(tu.name, dict(tu.input or {}))
                resultaten.append({"type": "tool_result", "tool_use_id": tu.id, "content": uitkomst})
            except (ToolFout, browser.BrowserFout, TypeError) as e:
                resultaten.append({"type": "tool_result", "tool_use_id": tu.id,
                                   "content": f"Fout: {e}", "is_error": True})
            except Exception as e:  # noqa: BLE001
                logger.exception("Tool %s faalde", tu.name)
                resultaten.append({"type": "tool_result", "tool_use_id": tu.id,
                                   "content": f"Onverwachte fout: {e}", "is_error": True})
        messages.append({"role": "user", "content": resultaten})
    else:
        samenvatting = (samenvatting + "\n" if samenvatting else "") + \
            f"(Ronde gestopt na {MAX_STAPPEN} stappen; wordt later voortgezet.)"

    _na_ronde(ronde, nieuwe_ids, samenvatting, totaal)
    return samenvatting


def _na_ronde(ronde: Ronde, nieuwe_ids: list[int], samenvatting: str, totaal: dict):
    oid = ronde.opdracht_id
    with db.get_db() as conn:
        if nieuwe_ids:
            conn.execute(f"UPDATE berichten SET verwerkt = 1 WHERE id IN ({','.join('?' * len(nieuwe_ids))})",
                         nieuwe_ids)
        conn.execute(
            "UPDATE opdrachten SET laatste_ronde_at = datetime('now'), tokens_in = tokens_in + ?, "
            "tokens_uit = tokens_uit + ?, zoekopdrachten = zoekopdrachten + ?, kosten_usd = kosten_usd + ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (totaal["tokens_in"], totaal["tokens_uit"], totaal["zoekopdrachten"], totaal["usd"], oid))
        if not ronde.volgende_ronde_gezet:
            conn.execute("UPDATE opdrachten SET volgende_ronde_at = NULL WHERE id = ? "
                         "AND volgende_ronde_at <= datetime('now')", (oid,))
        if ronde.rapport_definitief:
            conn.execute("UPDATE opdrachten SET status = 'afgerond' WHERE id = ?", (oid,))
    db.log(oid, samenvatting or "(geen samenvatting)", "ronde")
    _meld(ronde, samenvatting)


def _meld(ronde: Ronde, samenvatting: str):
    o = ronde.opdracht()
    link = f"{config.site_url()}/opdracht/{o['id']}"
    tag = f"[OPD-{o['id']}]"
    if ronde.rapport_definitief:
        mailer.meld_eigenaar(
            f"{tag} Eindrapport: {o['titel']}",
            f"{o['rapport']}\n\n---\nBekijk alles op {link}\n"
            "Antwoord op deze mail om de assistent verder te laten gaan met deze opdracht.")
        return
    if not ronde.nieuw_wacht and not ronde.nieuwe_vragen:
        return
    regels = [samenvatting, ""]
    if ronde.nieuwe_vragen:
        regels.append("Vragen aan jou (antwoord op deze mail of via de site):")
        regels += [f"- {v}" for v in ronde.nieuwe_vragen]
        regels.append("")
    if ronde.nieuw_wacht:
        regels.append(f"{ronde.nieuw_wacht} bericht(en) wachten op je goedkeuring: {link}")
    regels.append(f"\nOpdracht: {link}")
    mailer.meld_eigenaar(f"{tag} Actie nodig: {o['titel']}", "\n".join(regels))
