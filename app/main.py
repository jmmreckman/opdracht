import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import markdown as md
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import config, db, inbox, intake, scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("opdracht")

@asynccontextmanager
async def levensduur(_app):
    db.init_db()
    await scheduler.start()
    yield


app = FastAPI(title="Opdracht", lifespan=levensduur)
app.mount("/assets", StaticFiles(directory=config.APP_DIR / "static"), name="assets")
templates = Jinja2Templates(directory=config.APP_DIR / "templates")


def _md(tekst: str) -> str:
    return md.markdown(tekst or "", extensions=["tables", "sane_lists", "nl2br"])


def _lokaal(utc_str) -> str:
    if not utc_str:
        return ""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    return dt.astimezone(config.TZ).strftime("%d-%m %H:%M")


templates.env.filters["md"] = _md
templates.env.filters["lokaal"] = _lokaal
templates.env.globals["site_email"] = config.opdracht_email

STATUS_LABELS = {
    "concept": "concept", "actief": "actief", "gepauzeerd": "gepauzeerd",
    "afgerond": "afgerond", "geannuleerd": "geannuleerd",
}
AUTONOMIE_LABELS = {
    "alles_goedkeuren": "Alles eerst goedkeuren",
    "eerste_goedkeuren": "Eerste contact goedkeuren",
    "zelfstandig": "Zelfstandig",
}
templates.env.globals["AUTONOMIE_LABELS"] = AUTONOMIE_LABELS


# ------------------------------------------------------------------ login

PUBLIEK = {"/login", "/health"}


def _token() -> str:
    return hashlib.sha256(f"opdracht:{config.site_password()}".encode()).hexdigest()


@app.middleware("http")
async def vereist_login(request: Request, call_next):
    pad = request.url.path
    if pad in PUBLIEK or pad.startswith("/assets/") or pad.startswith("/api/extern/"):
        return await call_next(request)
    if not hmac.compare_digest(request.cookies.get("sessie", ""), _token()):
        if pad.startswith("/api/"):
            return JSONResponse({"fout": "niet ingelogd"}, status_code=401)
        return RedirectResponse("/login")
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login")
def login_pagina(request: Request, fout: int = 0):
    return templates.TemplateResponse(request, "login.html", {"fout": fout})


@app.post("/login")
def login(wachtwoord: str = Form(...)):
    if hmac.compare_digest(wachtwoord, config.site_password()):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("sessie", _token(), max_age=60 * 60 * 24 * 365, httponly=True,
                        samesite="lax", secure=config.site_url().startswith("https"))
        return resp
    return RedirectResponse("/login?fout=1", status_code=303)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("sessie")
    return resp


# ------------------------------------------------------------------ overzicht

@app.get("/")
def overzicht(request: Request):
    with db.get_db() as conn:
        opdrachten = conn.execute(
            "SELECT o.*, "
            "(SELECT COUNT(*) FROM acties a WHERE a.opdracht_id = o.id AND a.status IN ('wacht','handmatig')) AS actie_nodig, "
            "(SELECT COUNT(*) FROM partijen p WHERE p.opdracht_id = o.id) AS n_partijen, "
            "(SELECT COUNT(*) FROM partijen p WHERE p.opdracht_id = o.id AND p.offerte_bedrag != '') AS n_offertes "
            "FROM opdrachten o ORDER BY (o.status IN ('actief','concept')) DESC, o.updated_at DESC").fetchall()
        los = conn.execute(
            "SELECT * FROM berichten WHERE opdracht_id IS NULL AND richting = 'in' ORDER BY id DESC LIMIT 20"
        ).fetchall()
        waarschuwingen = conn.execute(
            "SELECT * FROM logboek WHERE opdracht_id IS NULL AND soort IN ('waarschuwing','fout') "
            "AND created_at >= datetime('now', '-3 day') ORDER BY id DESC LIMIT 5").fetchall()
    return templates.TemplateResponse(request, "overzicht.html", {
        "opdrachten": opdrachten, "los": los, "waarschuwingen": waarschuwingen,
        "alle_opdrachten": [o for o in opdrachten if o["status"] in ("actief", "concept", "gepauzeerd")],
        "mail_ok": bool(config.env("SMTP_HOST")), "imap_ok": bool(config.env("IMAP_HOST")),
        "imap_laatst": db.kv_get("imap_laatst"),
    })


# ------------------------------------------------------------------ intake

@app.get("/nieuw")
def nieuw():
    return RedirectResponse(f"/intake/{intake.nieuwe_intake()}", status_code=303)


@app.get("/intake/{intake_id}")
def intake_pagina(request: Request, intake_id: int):
    it = intake.laad(intake_id)
    if it is None:
        raise HTTPException(404)
    if it["opdracht_id"]:
        return RedirectResponse(f"/opdracht/{it['opdracht_id']}")
    return templates.TemplateResponse(request, "intake.html", {"it": it})


class IntakeBericht(BaseModel):
    tekst: str


@app.post("/api/intake/{intake_id}")
async def intake_bericht(intake_id: int, body: IntakeBericht):
    if intake.laad(intake_id) is None:
        raise HTTPException(404)
    try:
        it = await asyncio.to_thread(intake.praat, intake_id, body.tekst.strip())
    except Exception as e:  # noqa: BLE001
        logger.exception("Intake mislukt")
        return JSONResponse({"fout": f"Er ging iets mis: {e}"}, status_code=500)
    return {"weergave": it["weergave"], "voorstel": it["voorstel"]}


@app.post("/intake/{intake_id}/start")
def intake_start(intake_id: int, titel: str = Form(...), type: str = Form("uitbesteden"),
                 brief: str = Form(""), criteria: str = Form(""), deelbaar: str = Form(""),
                 deadline: str = Form(""), autonomie: str = Form("alles_goedkeuren")):
    oid = intake.start_uit_intake(intake_id, {
        "titel": titel, "type": type, "brief": brief, "criteria": criteria,
        "deelbaar": deelbaar, "deadline": deadline, "autonomie": autonomie})
    return RedirectResponse(f"/opdracht/{oid}", status_code=303)


# ------------------------------------------------------------------ opdracht

def _laad_opdracht(opdracht_id: int):
    o = db.opdracht(opdracht_id)
    if o is None:
        raise HTTPException(404)
    return o


@app.get("/opdracht/{opdracht_id}")
def opdracht_pagina(request: Request, opdracht_id: int):
    o = _laad_opdracht(opdracht_id)
    with db.get_db() as conn:
        partijen = conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM berichten b WHERE b.partij_id = p.id) AS n_berichten, "
            "(SELECT MAX(datum) FROM berichten b WHERE b.partij_id = p.id) AS laatste "
            "FROM partijen p WHERE p.opdracht_id = ? ORDER BY "
            "CASE p.status WHEN 'favoriet' THEN 0 WHEN 'offerte_ontvangen' THEN 1 WHEN 'in_gesprek' THEN 2 "
            "WHEN 'benaderd' THEN 3 WHEN 'gevonden' THEN 4 WHEN 'geen_reactie' THEN 5 ELSE 6 END, p.id",
            (opdracht_id,)).fetchall()
        acties = conn.execute(
            "SELECT a.*, p.naam AS partij_naam FROM acties a LEFT JOIN partijen p ON p.id = a.partij_id "
            "WHERE a.opdracht_id = ? ORDER BY a.id DESC", (opdracht_id,)).fetchall()
        berichten = conn.execute(
            "SELECT b.*, p.naam AS partij_naam FROM berichten b LEFT JOIN partijen p ON p.id = b.partij_id "
            "WHERE b.opdracht_id = ? ORDER BY b.datum DESC, b.id DESC LIMIT 60", (opdracht_id,)).fetchall()
        logboek = conn.execute(
            "SELECT * FROM logboek WHERE opdracht_id = ? ORDER BY id DESC LIMIT 40", (opdracht_id,)).fetchall()
    return templates.TemplateResponse(request, "opdracht.html", {
        "o": o, "partijen": partijen, "berichten": berichten, "logboek": logboek,
        "wacht": [a for a in acties if a["status"] == "wacht" and a["soort"] != "vraag"],
        "vragen": [a for a in acties if a["soort"] == "vraag" and a["status"] == "wacht"],
        "handmatig": [a for a in acties if a["status"] in ("handmatig", "mislukt")],
        "wachtrij": [a for a in acties if a["status"] == "goedgekeurd"],
        "verzendvenster": inbox.binnen_verzendvenster(),
        "bijlagen": db.bijlagen,
    })


@app.get("/api/opdracht/{opdracht_id}/status")
def opdracht_status(opdracht_id: int):
    o = _laad_opdracht(opdracht_id)
    return {"ronde_bezig": bool(o["ronde_bezig"]), "updated_at": o["updated_at"],
            "laatste_ronde_at": o["laatste_ronde_at"]}


@app.post("/opdracht/{opdracht_id}/bewerken")
def opdracht_bewerken(opdracht_id: int, titel: str = Form(...), brief: str = Form(""),
                      criteria: str = Form(""), deelbaar: str = Form(""), deadline: str = Form(""),
                      autonomie: str = Form("alles_goedkeuren"), type: str = Form("uitbesteden")):
    _laad_opdracht(opdracht_id)
    if autonomie not in AUTONOMIE_LABELS or type not in ("uitbesteden", "onderzoek"):
        raise HTTPException(400)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE opdrachten SET titel = ?, brief = ?, criteria = ?, deelbaar = ?, deadline = ?, "
            "autonomie = ?, type = ?, updated_at = datetime('now') WHERE id = ?",
            (titel, brief, criteria, deelbaar, deadline, autonomie, type, opdracht_id))
    db.log(opdracht_id, "Opdracht aangepast door opdrachtgever.")
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/opdracht/{opdracht_id}/status")
def opdracht_status_zetten(opdracht_id: int, status: str = Form(...)):
    o = _laad_opdracht(opdracht_id)
    if status not in STATUS_LABELS:
        raise HTTPException(400)
    with db.get_db() as conn:
        conn.execute("UPDATE opdrachten SET status = ?, updated_at = datetime('now') WHERE id = ?",
                     (status, opdracht_id))
    if status == "actief" and o["status"] != "actief":
        inbox.plan_ronde_nu(opdracht_id)
    db.log(opdracht_id, f"Status gezet op {status}.")
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/opdracht/{opdracht_id}/autonomie")
def opdracht_autonomie(opdracht_id: int, autonomie: str = Form(...)):
    _laad_opdracht(opdracht_id)
    if autonomie not in AUTONOMIE_LABELS:
        raise HTTPException(400)
    with db.get_db() as conn:
        conn.execute("UPDATE opdrachten SET autonomie = ? WHERE id = ?", (autonomie, opdracht_id))
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/opdracht/{opdracht_id}/ronde")
async def opdracht_ronde(opdracht_id: int):
    o = _laad_opdracht(opdracht_id)
    if o["status"] in ("actief", "concept", "gepauzeerd") and not o["ronde_bezig"]:
        asyncio.create_task(scheduler.draai(opdracht_id, "handmatig gestart door de opdrachtgever"))
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/opdracht/{opdracht_id}/bericht")
def opdracht_bericht(opdracht_id: int, tekst: str = Form(...)):
    _laad_opdracht(opdracht_id)
    if tekst.strip():
        inbox.bericht_van_eigenaar(opdracht_id, tekst.strip())
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/opdracht/{opdracht_id}/verwijderen")
def opdracht_verwijderen(opdracht_id: int, bevestig: str = Form("")):
    o = _laad_opdracht(opdracht_id)
    if bevestig.strip().lower() != "verwijder":
        return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)
    with db.get_db() as conn:
        conn.execute("DELETE FROM opdrachten WHERE id = ?", (o["id"],))
    return RedirectResponse("/", status_code=303)


@app.get("/opdracht/{opdracht_id}/partij/{partij_id}")
def partij_pagina(request: Request, opdracht_id: int, partij_id: int):
    o = _laad_opdracht(opdracht_id)
    with db.get_db() as conn:
        p = conn.execute("SELECT * FROM partijen WHERE id = ? AND opdracht_id = ?",
                         (partij_id, opdracht_id)).fetchone()
        if p is None:
            raise HTTPException(404)
        berichten = conn.execute("SELECT * FROM berichten WHERE partij_id = ? ORDER BY datum, id",
                                 (partij_id,)).fetchall()
        acties = conn.execute("SELECT * FROM acties WHERE partij_id = ? AND status NOT IN ('verstuurd') "
                              "ORDER BY id", (partij_id,)).fetchall()
    return templates.TemplateResponse(request, "partij.html", {
        "o": o, "p": p, "berichten": berichten, "acties": acties, "bijlagen": db.bijlagen})


@app.post("/opdracht/{opdracht_id}/partij/{partij_id}/status")
def partij_status(opdracht_id: int, partij_id: int, status: str = Form(...)):
    with db.get_db() as conn:
        conn.execute("UPDATE partijen SET status = ?, updated_at = datetime('now') WHERE id = ? AND opdracht_id = ?",
                     (status, partij_id, opdracht_id))
    return RedirectResponse(f"/opdracht/{opdracht_id}/partij/{partij_id}", status_code=303)


# ------------------------------------------------------------------ acties

def _laad_actie(actie_id: int):
    with db.get_db() as conn:
        a = conn.execute("SELECT * FROM acties WHERE id = ?", (actie_id,)).fetchone()
    if a is None:
        raise HTTPException(404)
    return a


def _terug(a, request: Request):
    ref = request.headers.get("referer", "")
    if re.search(r"/opdracht/\d+", ref):
        return RedirectResponse(ref, status_code=303)
    return RedirectResponse(f"/opdracht/{a['opdracht_id']}", status_code=303)


@app.post("/actie/{actie_id}/goedkeuren")
def actie_goedkeuren(request: Request, actie_id: int, onderwerp: str = Form(""), tekst: str = Form("")):
    a = _laad_actie(actie_id)
    if a["status"] != "wacht" or a["soort"] == "vraag":
        return _terug(a, request)
    with db.get_db() as conn:
        if a["soort"] == "mail":
            conn.execute("UPDATE acties SET status = 'goedgekeurd', onderwerp = ?, tekst = ? WHERE id = ?",
                         (onderwerp or a["onderwerp"], tekst or a["tekst"], actie_id))
        else:
            conn.execute("UPDATE acties SET status = 'goedgekeurd' WHERE id = ?", (actie_id,))
    return _terug(a, request)


@app.post("/actie/goedkeuren-alles/{opdracht_id}")
def acties_alles_goedkeuren(opdracht_id: int):
    with db.get_db() as conn:
        conn.execute("UPDATE acties SET status = 'goedgekeurd' WHERE opdracht_id = ? AND status = 'wacht' "
                     "AND soort IN ('mail','formulier')", (opdracht_id,))
    return RedirectResponse(f"/opdracht/{opdracht_id}", status_code=303)


@app.post("/actie/{actie_id}/afwijzen")
def actie_afwijzen(request: Request, actie_id: int, reden: str = Form("")):
    a = _laad_actie(actie_id)
    with db.get_db() as conn:
        conn.execute("UPDATE acties SET status = 'afgewezen', resultaat = ? WHERE id = ?",
                     (reden, actie_id))
    if reden.strip():
        inbox.bericht_van_eigenaar(
            a["opdracht_id"], f"Ik heb je {a['soort']} '{a['onderwerp'] or a['aan']}' afgewezen: {reden.strip()}")
    return _terug(a, request)


@app.post("/actie/{actie_id}/beantwoorden")
def actie_beantwoorden(request: Request, actie_id: int, antwoord: str = Form(...)):
    a = _laad_actie(actie_id)
    with db.get_db() as conn:
        conn.execute("UPDATE acties SET status = 'beantwoord', antwoord = ?, uitgevoerd_at = datetime('now') "
                     "WHERE id = ?", (antwoord, actie_id))
    inbox.bericht_van_eigenaar(a["opdracht_id"], f"Antwoord op je vraag \"{a['tekst']}\":\n{antwoord}")
    return _terug(a, request)


@app.post("/actie/{actie_id}/zelf-verstuurd")
def actie_zelf_verstuurd(request: Request, actie_id: int):
    a = _laad_actie(actie_id)
    if a["soort"] == "formulier" and a["status"] in ("handmatig", "mislukt", "wacht"):
        inbox.registreer_formulier_verstuurd(a, "Handmatig verstuurd door opdrachtgever.")
    return _terug(a, request)


@app.post("/actie/{actie_id}/opnieuw")
def actie_opnieuw(request: Request, actie_id: int):
    a = _laad_actie(actie_id)
    if a["status"] in ("mislukt", "handmatig", "afgewezen"):
        with db.get_db() as conn:
            conn.execute("UPDATE acties SET status = 'goedgekeurd', resultaat = '' WHERE id = ?", (actie_id,))
    return _terug(a, request)


# ------------------------------------------------------------------ losse berichten

@app.post("/bericht/{bericht_id}/koppel")
def bericht_koppelen(bericht_id: int, opdracht_id: int = Form(...)):
    _laad_opdracht(opdracht_id)
    with db.get_db() as conn:
        conn.execute("UPDATE berichten SET opdracht_id = ?, verwerkt = 0 WHERE id = ?", (opdracht_id, bericht_id))
    inbox.plan_ronde_nu(opdracht_id)
    return RedirectResponse("/", status_code=303)


@app.post("/bericht/{bericht_id}/negeer")
def bericht_negeren(bericht_id: int):
    with db.get_db() as conn:
        conn.execute("DELETE FROM berichten WHERE id = ? AND opdracht_id IS NULL", (bericht_id,))
    return RedirectResponse("/", status_code=303)


@app.get("/bestand/{soort}/{naam}")
def bestand(soort: str, naam: str):
    basis = {"bijlage": config.BIJLAGEN_DIR, "screenshot": config.SCREENSHOTS_DIR}.get(soort)
    if basis is None or "/" in naam or ".." in naam:
        raise HTTPException(404)
    pad = basis / naam
    if not pad.exists():
        raise HTTPException(404)
    return FileResponse(pad, filename=naam.split("_", 1)[-1] if soort == "bijlage" else naam)


# ------------------------------------------------------------------ API voor Claude Code

def _api_check(request: Request, alleen_lezen: bool = False):
    """API_TOKEN mag alles; LEES_TOKEN alleen de GET-endpoints."""
    kop = request.headers.get("authorization", "")
    tokens = [config.api_token()] + ([config.lees_token()] if alleen_lezen else [])
    if not any(t and hmac.compare_digest(kop, f"Bearer {t}") for t in tokens):
        raise HTTPException(401, "Ongeldig of ontbrekend token")


class NieuweOpdracht(BaseModel):
    titel: str
    type: str = "uitbesteden"
    brief: str
    criteria: str = ""
    deelbaar: str = ""
    deadline: str = ""
    autonomie: str = "alles_goedkeuren"
    start: bool = True


class Bericht(BaseModel):
    tekst: str


@app.post("/api/extern/opdrachten")
def api_nieuwe_opdracht(request: Request, body: NieuweOpdracht):
    _api_check(request)
    oid = intake.maak_opdracht(body.model_dump(), "actief" if body.start else "concept")
    return {"id": oid, "url": f"{config.site_url()}/opdracht/{oid}"}


@app.get("/api/extern/opdrachten")
def api_opdrachten(request: Request):
    _api_check(request, alleen_lezen=True)
    with db.get_db() as conn:
        rows = conn.execute("SELECT id, titel, type, status, updated_at FROM opdrachten ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/extern/opdrachten/{opdracht_id}")
def api_opdracht(request: Request, opdracht_id: int, alles: bool = False):
    """Stand van een opdracht. Met ?alles=1 ook alle berichten (volledige tekst),
    acties en bijlagen, zodat een Claude Code-sessie er vragen over kan beantwoorden."""
    _api_check(request, alleen_lezen=True)
    o = _laad_opdracht(opdracht_id)
    with db.get_db() as conn:
        partijen = conn.execute(
            "SELECT id, naam, status, email, website, telefoon, formulier_url, plaats, offerte_bedrag, "
            "offerte_samenvatting, notitie FROM partijen WHERE opdracht_id = ?", (opdracht_id,)).fetchall()
        logboek = conn.execute("SELECT created_at, soort, tekst FROM logboek WHERE opdracht_id = ? "
                               "ORDER BY id DESC LIMIT ?", (opdracht_id, 200 if alles else 15)).fetchall()
        open_ = conn.execute("SELECT id, soort, onderwerp, tekst FROM acties WHERE opdracht_id = ? "
                             "AND status IN ('wacht','handmatig')", (opdracht_id,)).fetchall()
        berichten = conn.execute(
            "SELECT id, partij_id, richting, soort, van, aan, onderwerp, tekst, automatisch, bijlagen, datum "
            "FROM berichten WHERE opdracht_id = ? ORDER BY datum, id", (opdracht_id,)).fetchall() if alles else []
        acties = conn.execute(
            "SELECT id, partij_id, soort, status, aan, onderwerp, tekst, antwoord, resultaat, created_at, "
            "uitgevoerd_at FROM acties WHERE opdracht_id = ? ORDER BY id", (opdracht_id,)).fetchall() if alles else []
    uit = {k: o[k] for k in ("id", "titel", "type", "status", "brief", "criteria", "deelbaar", "deadline",
                              "autonomie", "notities", "rapport", "rapport_definitief", "kosten_usd")}
    uit.update({"partijen": [dict(p) for p in partijen], "logboek": [dict(lg) for lg in logboek],
                "wacht_op_jou": [dict(a) for a in open_]})
    if alles:
        uit["berichten"] = []
        for b in berichten:
            d = dict(b)
            d["bijlagen"] = [{**x, "url": f"{config.site_url()}/api/extern/bijlage/{x['bestand']}"}
                             for x in db.bijlagen(b)]
            uit["berichten"].append(d)
        uit["acties"] = [dict(a) for a in acties]
    return uit


@app.get("/api/extern/bijlage/{naam}")
def api_bijlage(request: Request, naam: str):
    _api_check(request, alleen_lezen=True)
    if "/" in naam or ".." in naam or not (config.BIJLAGEN_DIR / naam).exists():
        raise HTTPException(404)
    return FileResponse(config.BIJLAGEN_DIR / naam, filename=naam.split("_", 1)[-1])


@app.post("/api/extern/opdrachten/{opdracht_id}/bericht")
def api_bericht(request: Request, opdracht_id: int, body: Bericht):
    _api_check(request)
    _laad_opdracht(opdracht_id)
    inbox.bericht_van_eigenaar(opdracht_id, body.tekst)
    return {"ok": True}


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


templates.env.filters["json"] = _json
