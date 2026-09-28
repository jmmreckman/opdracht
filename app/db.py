import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS opdrachten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titel TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'uitbesteden' CHECK(type IN ('uitbesteden','onderzoek')),
    status TEXT NOT NULL DEFAULT 'concept'
        CHECK(status IN ('concept','actief','gepauzeerd','afgerond','geannuleerd')),
    brief TEXT NOT NULL DEFAULT '',
    criteria TEXT NOT NULL DEFAULT '',
    deelbaar TEXT NOT NULL DEFAULT '',
    deadline TEXT NOT NULL DEFAULT '',
    autonomie TEXT NOT NULL DEFAULT 'alles_goedkeuren'
        CHECK(autonomie IN ('alles_goedkeuren','eerste_goedkeuren','zelfstandig')),
    notities TEXT NOT NULL DEFAULT '',
    rapport TEXT NOT NULL DEFAULT '',
    rapport_definitief INTEGER NOT NULL DEFAULT 0,
    volgende_ronde_at TEXT,
    laatste_ronde_at TEXT,
    ronde_bezig INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_uit INTEGER NOT NULL DEFAULT 0,
    zoekopdrachten INTEGER NOT NULL DEFAULT 0,
    kosten_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS partijen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opdracht_id INTEGER NOT NULL REFERENCES opdrachten(id) ON DELETE CASCADE,
    naam TEXT NOT NULL,
    website TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    telefoon TEXT NOT NULL DEFAULT '',
    formulier_url TEXT NOT NULL DEFAULT '',
    plaats TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'gevonden'
        CHECK(status IN ('gevonden','benaderd','in_gesprek','offerte_ontvangen',
                         'geen_reactie','afgevallen','favoriet')),
    notitie TEXT NOT NULL DEFAULT '',
    offerte_bedrag TEXT NOT NULL DEFAULT '',
    offerte_samenvatting TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Alles wat binnenkomt of de deur uit gaat: mails, formulieren, berichten
-- van jou (de opdrachtgever) aan de assistent.
CREATE TABLE IF NOT EXISTS berichten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opdracht_id INTEGER REFERENCES opdrachten(id) ON DELETE CASCADE,
    partij_id INTEGER REFERENCES partijen(id) ON DELETE SET NULL,
    richting TEXT NOT NULL CHECK(richting IN ('in','uit')),
    soort TEXT NOT NULL DEFAULT 'mail' CHECK(soort IN ('mail','formulier','opdrachtgever')),
    van TEXT NOT NULL DEFAULT '',
    aan TEXT NOT NULL DEFAULT '',
    onderwerp TEXT NOT NULL DEFAULT '',
    tekst TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    in_reply_to TEXT NOT NULL DEFAULT '',
    references_hdr TEXT NOT NULL DEFAULT '',
    automatisch INTEGER NOT NULL DEFAULT 0,
    bijlagen TEXT NOT NULL DEFAULT '[]',
    verwerkt INTEGER NOT NULL DEFAULT 0,
    datum TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_berichten_msgid ON berichten(message_id);
CREATE INDEX IF NOT EXISTS idx_berichten_opdracht ON berichten(opdracht_id, verwerkt);

-- Wat de assistent wil doen en (afhankelijk van de autonomie) eerst door
-- jou goedgekeurd moet worden: een mail, een formulier, of een vraag aan jou.
CREATE TABLE IF NOT EXISTS acties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opdracht_id INTEGER NOT NULL REFERENCES opdrachten(id) ON DELETE CASCADE,
    partij_id INTEGER REFERENCES partijen(id) ON DELETE SET NULL,
    soort TEXT NOT NULL CHECK(soort IN ('mail','formulier','vraag')),
    status TEXT NOT NULL DEFAULT 'wacht'
        CHECK(status IN ('wacht','goedgekeurd','verstuurd','afgewezen','mislukt',
                         'handmatig','beantwoord')),
    aan TEXT NOT NULL DEFAULT '',
    onderwerp TEXT NOT NULL DEFAULT '',
    tekst TEXT NOT NULL DEFAULT '',
    antwoord_op INTEGER REFERENCES berichten(id) ON DELETE SET NULL,
    formulier TEXT NOT NULL DEFAULT '',
    antwoord TEXT NOT NULL DEFAULT '',
    resultaat TEXT NOT NULL DEFAULT '',
    screenshot TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    uitgevoerd_at TEXT
);

CREATE TABLE IF NOT EXISTS logboek (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opdracht_id INTEGER REFERENCES opdrachten(id) ON DELETE CASCADE,
    soort TEXT NOT NULL DEFAULT 'info',
    tekst TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gesprek TEXT NOT NULL DEFAULT '[]',
    voorstel TEXT NOT NULL DEFAULT '',
    opdracht_id INTEGER REFERENCES opdrachten(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    sleutel TEXT PRIMARY KEY,
    waarde TEXT NOT NULL
);
"""


def db_path():
    return config.DATA_DIR / "opdracht.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.BIJLAGEN_DIR.mkdir(parents=True, exist_ok=True)
    config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
        # Een ronde die bezig was toen de container herstartte, is nooit afgemaakt.
        db.execute("UPDATE opdrachten SET ronde_bezig = 0")


def nu() -> str:
    """UTC-tijdstempel in hetzelfde formaat als SQLite's datetime('now')."""
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def kv_get(sleutel: str, standaard: str = "") -> str:
    with get_db() as db:
        row = db.execute("SELECT waarde FROM kv WHERE sleutel = ?", (sleutel,)).fetchone()
    return row["waarde"] if row else standaard


def kv_set(sleutel: str, waarde: str) -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO kv (sleutel, waarde) VALUES (?, ?) "
            "ON CONFLICT(sleutel) DO UPDATE SET waarde = excluded.waarde",
            (sleutel, waarde),
        )


def log(opdracht_id: int | None, tekst: str, soort: str = "info") -> None:
    with get_db() as db:
        db.execute(
            "INSERT INTO logboek (opdracht_id, soort, tekst) VALUES (?, ?, ?)",
            (opdracht_id, soort, tekst),
        )


def opdracht(opdracht_id: int) -> sqlite3.Row | None:
    with get_db() as db:
        return db.execute("SELECT * FROM opdrachten WHERE id = ?", (opdracht_id,)).fetchone()


def bijlagen(row) -> list[dict]:
    try:
        return json.loads(row["bijlagen"] or "[]")
    except (ValueError, TypeError):
        return []
