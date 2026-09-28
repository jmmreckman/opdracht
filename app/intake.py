"""Het intakegesprek: samen met Claude een opdracht scherp krijgen voordat
de assistent aan de slag gaat."""
import json
import logging

from . import claude, config, db, mailer
from .prompts import INTAKE_SYSTEEM

logger = logging.getLogger("opdracht")

VOORSTEL_TOOL = {
    "name": "opdracht_voorstellen",
    "description": "Leg de uitgewerkte opdracht voor aan de opdrachtgever, die hem met één klik start.",
    "input_schema": {
        "type": "object",
        "properties": {
            "titel": {"type": "string", "description": "Korte titel, bijv. 'Prefab dakkapel Rotterdam'."},
            "type": {"type": "string", "enum": ["uitbesteden", "onderzoek"]},
            "brief": {"type": "string", "description": "Volledige opdrachtomschrijving voor de assistent."},
            "criteria": {"type": "string", "description": "Harde eisen en wensen, met wat het zwaarst weegt."},
            "deelbaar": {"type": "string", "description": "Wat met bedrijven gedeeld mag worden."},
            "deadline": {"type": "string", "description": "Planning/deadlines, bijv. plaatsing over 5 tot 8 weken."},
            "autonomie": {"type": "string", "enum": ["alles_goedkeuren", "eerste_goedkeuren", "zelfstandig"]},
        },
        "required": ["titel", "type", "brief", "criteria", "deelbaar", "deadline", "autonomie"],
        "additionalProperties": False,
    },
}


def _dump(content) -> list:
    return [b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else b
            for b in content]


def nieuwe_intake() -> int:
    with db.get_db() as conn:
        cur = conn.execute("INSERT INTO intakes (gesprek) VALUES ('{\"api\": [], \"weergave\": []}')")
        return cur.lastrowid


def laad(intake_id: int) -> dict | None:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM intakes WHERE id = ?", (intake_id,)).fetchone()
    if row is None:
        return None
    gesprek = json.loads(row["gesprek"])
    return {
        "id": row["id"],
        "api": gesprek.get("api", []),
        "weergave": gesprek.get("weergave", []),
        "voorstel": json.loads(row["voorstel"]) if row["voorstel"] else None,
        "opdracht_id": row["opdracht_id"],
    }


def _bewaar(intake_id: int, api: list, weergave: list, voorstel: dict | None) -> None:
    with db.get_db() as conn:
        conn.execute("UPDATE intakes SET gesprek = ?, voorstel = ? WHERE id = ?",
                     (json.dumps({"api": api, "weergave": weergave}, ensure_ascii=False),
                      json.dumps(voorstel, ensure_ascii=False) if voorstel else "", intake_id))


def praat(intake_id: int, tekst: str) -> dict:
    """Eén beurt in het intakegesprek. Geeft de bijgewerkte intake terug."""
    it = laad(intake_id)
    api, weergave, voorstel = it["api"], it["weergave"], it["voorstel"]
    api.append({"role": "user", "content": tekst})
    weergave.append({"rol": "jij", "tekst": tekst})

    antwoord_tekst = ""
    for _ in range(4):
        resp = claude.vraag(INTAKE_SYSTEEM, api, [VOORSTEL_TOOL], max_tokens=8000)
        inhoud = claude.inhoud_voor_geschiedenis(resp.content)
        api.append({"role": "assistant", "content": _dump(inhoud)})
        t = claude.tekst_van(inhoud)
        if t:
            antwoord_tekst = (antwoord_tekst + "\n\n" + t).strip()
        tool_uses = [b for b in inhoud if b.type == "tool_use"]
        if resp.stop_reason == "pause_turn":
            continue
        if not tool_uses:
            break
        resultaten = []
        for tu in tool_uses:
            voorstel = dict(tu.input)
            resultaten.append({"type": "tool_result", "tool_use_id": tu.id,
                               "content": "Voorstel staat klaar voor de opdrachtgever. Sluit af met "
                                          "één of twee zinnen; herhaal het voorstel niet."})
        api.append({"role": "user", "content": resultaten})

    weergave.append({"rol": "assistent", "tekst": antwoord_tekst or "(voorstel klaargezet)"})
    _bewaar(intake_id, api, weergave, voorstel)
    return laad(intake_id)


def maak_opdracht(voorstel: dict, status: str = "actief") -> int:
    autonomie = voorstel.get("autonomie") or "alles_goedkeuren"
    if autonomie not in ("alles_goedkeuren", "eerste_goedkeuren", "zelfstandig"):
        autonomie = "alles_goedkeuren"
    type_ = voorstel.get("type") if voorstel.get("type") in ("uitbesteden", "onderzoek") else "uitbesteden"
    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO opdrachten (titel, type, status, brief, criteria, deelbaar, deadline, autonomie, "
            "volgende_ronde_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'actief' THEN datetime('now') END)",
            (voorstel.get("titel") or "Nieuwe opdracht", type_, status, voorstel.get("brief", ""),
             voorstel.get("criteria", ""), voorstel.get("deelbaar", ""), voorstel.get("deadline", ""),
             autonomie, status))
        oid = cur.lastrowid
    db.log(oid, f"Opdracht aangemaakt ({status}).")
    return oid


def start_uit_intake(intake_id: int, voorstel: dict) -> int:
    oid = maak_opdracht(voorstel, "actief")
    with db.get_db() as conn:
        conn.execute("UPDATE intakes SET opdracht_id = ? WHERE id = ?", (oid, intake_id))
    return oid


def opdracht_uit_mail(onderwerp: str, tekst: str) -> int:
    """Nieuwe opdracht die je per mail stuurt: wordt een concept dat je op
    de site met één klik start (of eerst aanvult)."""
    vraag = (f"De opdrachtgever mailde deze nieuwe opdracht. Je kunt nu geen vragen stellen: roep "
             f"direct opdracht_voorstellen aan en noteer in de brief wat nog onduidelijk is.\n\n"
             f"Onderwerp: {onderwerp}\n\n{tekst}")
    voorstel = None
    try:
        resp = claude.vraag(INTAKE_SYSTEEM, [{"role": "user", "content": vraag}], [VOORSTEL_TOOL],
                            max_tokens=8000)
        for b in resp.content:
            if b.type == "tool_use" and b.name == "opdracht_voorstellen":
                voorstel = dict(b.input)
    except Exception:  # noqa: BLE001
        logger.exception("Intake uit mail mislukt")
    if voorstel is None:
        voorstel = {"titel": onderwerp or "Opdracht per mail", "type": "uitbesteden", "brief": tekst,
                    "criteria": "", "deelbaar": "", "deadline": "", "autonomie": "alles_goedkeuren"}
    oid = maak_opdracht(voorstel, "concept")
    mailer.meld_eigenaar(
        f"[OPD-{oid}] Concept klaar: {voorstel['titel']}",
        f"Ik heb je mail omgezet in een opdracht. Controleer en start hem hier:\n"
        f"{config.site_url()}/opdracht/{oid}\n\n{voorstel.get('brief', '')}\n\n"
        f"Eisen: {voorstel.get('criteria', '')}\nPlanning: {voorstel.get('deadline', '')}\n"
        f"Mag gedeeld worden: {voorstel.get('deelbaar', '')}")
    return oid
