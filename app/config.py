"""Alle instellingen komen uit omgevingsvariabelen (.env op de VPS).
Hier op één plek, zodat de rest van de code nooit zelf os.environ leest."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR.parent / "data"))
BIJLAGEN_DIR = DATA_DIR / "bijlagen"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"

TZ = ZoneInfo(os.environ.get("TIJDZONE", "Europe/Amsterdam"))


def env(naam: str, standaard: str = "") -> str:
    return os.environ.get(naam, standaard).strip()


def env_int(naam: str, standaard: int) -> int:
    try:
        return int(env(naam, str(standaard)))
    except ValueError:
        return standaard


def env_bool(naam: str, standaard: bool) -> bool:
    waarde = env(naam, "1" if standaard else "0").lower()
    return waarde in ("1", "true", "ja", "yes", "aan")


def site_password() -> str:
    return env("SITE_PASSWORD", "verander-mij")


def site_url() -> str:
    return env("SITE_URL", "https://opdracht.steenhub.nl").rstrip("/")


def opdracht_email() -> str:
    """Het adres waar alle communicatie vandaan komt (en binnenkomt)."""
    return env("OPDRACHT_EMAIL") or env("SMTP_USER") or env("SMTP_FROM")


def afzender_naam() -> str:
    return env("AFZENDER_NAAM")


def eigenaar_emails() -> set[str]:
    """Adressen van de opdrachtgever zelf (jij). Mails hiervandaan zijn
    instructies/nieuwe opdrachten, geen reacties van partijen."""
    ruw = env("EIGENAAR_EMAILS") or env("NOTIFY_EMAIL")
    return {a.strip().lower() for a in ruw.split(",") if a.strip()}


def notify_email() -> str:
    return env("NOTIFY_EMAIL")


def claude_model() -> str:
    return env("CLAUDE_MODEL", "claude-opus-5")


def claude_effort() -> str:
    return env("CLAUDE_EFFORT", "high")


def api_token() -> str:
    return env("API_TOKEN")


def lees_token() -> str:
    """Token dat alleen mag lezen (voor Claude Code-sessies die vragen beantwoorden)."""
    return env("LEES_TOKEN")
