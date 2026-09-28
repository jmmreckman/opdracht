import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SCHEDULER", "0")
os.environ.setdefault("SITE_PASSWORD", "test")
os.environ.setdefault("OPDRACHT_EMAIL", "opdracht@steenhub.nl")
os.environ.setdefault("NOTIFY_EMAIL", "eigenaar@example.com")
os.environ.setdefault("API_TOKEN", "geheim")
os.environ.setdefault("SITE_URL", "http://testserver")
if Path("/opt/pw-browsers/chromium").exists():
    os.environ.setdefault("CHROMIUM_PATH", "/opt/pw-browsers/chromium")


@pytest.fixture(autouse=True)
def schone_db(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BIJLAGEN_DIR", tmp_path / "bijlagen")
    monkeypatch.setattr(config, "SCREENSHOTS_DIR", tmp_path / "screenshots")
    db.init_db()
    yield tmp_path


@pytest.fixture
def opdracht():
    from app import intake
    return intake.maak_opdracht({
        "titel": "Prefab dakkapel Rotterdam", "type": "uitbesteden",
        "brief": "Prefab dakkapel 2,5 m breed, plaatsing in 1 dag.",
        "criteria": "Plaatsing over 5 tot 8 weken (hard).", "deelbaar": "Rotterdam-Noord, tussenwoning",
        "deadline": "5-8 weken", "autonomie": "alles_goedkeuren"}, "actief")
