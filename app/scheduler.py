"""Achtergrondlus in de app zelf: mail ophalen, goedgekeurde berichten
versturen, rondes draaien op vaste tijden en bij nieuwe mail, en een
dagelijkse samenvatting aan jou."""
import asyncio
import logging
from datetime import datetime

from . import agent, config, db, inbox, mailer

logger = logging.getLogger("opdracht")

_ronde_slot = asyncio.Semaphore(1)  # één ronde tegelijk: overzichtelijk en goedkoper


async def start():
    if not config.env_bool("SCHEDULER", True):
        logger.info("Planner staat uit (SCHEDULER=0)")
        return
    asyncio.create_task(_lus("mail ophalen", config.env_int("MAIL_POLL_MIN", 5) * 60, haal_mail))
    asyncio.create_task(_lus("verzenden", 60, verzend))
    asyncio.create_task(_lus("rondes", 60, rondes))
    asyncio.create_task(_lus("vaste tijden", 60, vaste_tijden))


async def _lus(naam, interval, functie):
    await asyncio.sleep(5)
    while True:
        try:
            await functie()
        except Exception:  # noqa: BLE001
            logger.exception("Fout in planner-taak '%s'", naam)
        await asyncio.sleep(interval)


async def haal_mail():
    if not config.env("IMAP_HOST"):
        return
    staat = db.kv_get("imap_staat")
    mails, nieuwe_staat = await asyncio.to_thread(mailer.haal_nieuwe_mails, staat)
    for mail in mails:
        uitkomst = await asyncio.to_thread(inbox.verwerk_mail, mail)
        oid = uitkomst.get("opdracht_id")
        if oid and uitkomst["soort"] == "opdrachtgever":
            inbox.plan_ronde_nu(oid)
        elif oid:
            # Even wachten: vaak komt er een tweede mail of bijlage achteraan.
            inbox.plan_ronde_nu(oid, over_minuten=config.env_int("RONDE_NA_MAIL_MIN", 10))
    db.kv_set("imap_staat", nieuwe_staat)
    db.kv_set("imap_laatst", db.nu())


async def verzend():
    if not inbox.binnen_verzendvenster():
        return
    actie = inbox.volgende_actie()
    if actie is not None:
        status = await asyncio.to_thread(inbox.voer_actie_uit, actie)
        logger.info("Actie %s: %s", actie["id"], status)


async def rondes():
    with db.get_db() as conn:
        klaar = conn.execute(
            "SELECT id FROM opdrachten WHERE status = 'actief' AND ronde_bezig = 0 "
            "AND volgende_ronde_at IS NOT NULL AND volgende_ronde_at <= datetime('now') "
            "ORDER BY volgende_ronde_at LIMIT 1").fetchall()
    for rij in klaar:
        await draai(rij["id"], "geplande ronde / nieuwe informatie")


async def draai(opdracht_id: int, reden: str) -> str:
    async with _ronde_slot:
        with db.get_db() as conn:
            conn.execute("UPDATE opdrachten SET volgende_ronde_at = NULL WHERE id = ? "
                         "AND volgende_ronde_at <= datetime('now')", (opdracht_id,))
        return await asyncio.to_thread(agent.draai_ronde, opdracht_id, reden)


def _tijden(naam: str, standaard: str) -> list[tuple[int, int]]:
    uit = []
    for t in config.env(naam, standaard).split(","):
        uur, _, minuut = t.strip().partition(":")
        if uur.isdigit():
            uit.append((int(uur), int(minuut or 0)))
    return uit


async def vaste_tijden():
    nu = datetime.now(config.TZ)
    vandaag = nu.strftime("%Y-%m-%d")
    for uur, minuut in _tijden("RONDE_TIJDEN", "09:15,15:45"):
        sleutel = f"ronde_{uur:02d}{minuut:02d}"
        if (nu.hour, nu.minute) >= (uur, minuut) and db.kv_get(sleutel) != vandaag:
            db.kv_set(sleutel, vandaag)
            with db.get_db() as conn:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM opdrachten WHERE status = 'actief'").fetchall()]
            for oid in ids:
                inbox.plan_ronde_nu(oid)
    for uur, minuut in _tijden("SAMENVATTING_TIJD", "18:30"):
        if (nu.hour, nu.minute) >= (uur, minuut) and db.kv_get("samenvatting") != vandaag:
            db.kv_set("samenvatting", vandaag)
            await asyncio.to_thread(stuur_samenvatting)


def stuur_samenvatting() -> None:
    """Eén mail per dag met wat er per actieve opdracht gebeurd is."""
    with db.get_db() as conn:
        opdrachten = conn.execute("SELECT * FROM opdrachten WHERE status = 'actief' ORDER BY id").fetchall()
        blokken = []
        for o in opdrachten:
            logs = conn.execute(
                "SELECT tekst FROM logboek WHERE opdracht_id = ? AND created_at >= datetime('now', '-1 day') "
                "AND soort IN ('ronde','fout') ORDER BY id", (o["id"],)).fetchall()
            wacht = conn.execute(
                "SELECT COUNT(*) FROM acties WHERE opdracht_id = ? AND status IN ('wacht','handmatig')",
                (o["id"],)).fetchone()[0]
            nieuw_in = conn.execute(
                "SELECT COUNT(*) FROM berichten WHERE opdracht_id = ? AND richting = 'in' "
                "AND soort != 'opdrachtgever' AND datum >= datetime('now', '-1 day')", (o["id"],)).fetchone()[0]
            uit = conn.execute(
                "SELECT COUNT(*) FROM berichten WHERE opdracht_id = ? AND richting = 'uit' "
                "AND datum >= datetime('now', '-1 day')", (o["id"],)).fetchone()[0]
            if not logs and not wacht and not nieuw_in:
                continue
            regels = [f"[OPD-{o['id']}] {o['titel']}",
                      f"Vandaag: {uit} verstuurd, {nieuw_in} reacties binnen, {wacht} wacht(en) op jou."]
            regels += [f"- {lg['tekst']}" for lg in logs[-3:]]
            regels.append(f"{config.site_url()}/opdracht/{o['id']}")
            blokken.append("\n".join(regels))
    if blokken:
        mailer.meld_eigenaar("Opdrachten: stand van vandaag", "\n\n".join(blokken))
