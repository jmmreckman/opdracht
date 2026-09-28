"""Formulieren invullen in een echte (headless) Chromium tegen een lokaal testsite'je."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import browser

FORM = """<html><body>
<div id="cookie"><button onclick="this.parentNode.remove()">Alles accepteren</button></div>
<h1>Contact</h1><p>Mail ons: info@dakbedrijf.nl of bel <a href="tel:0101234567">010-1234567</a></p>
<a href="/offerte">Offerte aanvragen</a>
<form method="post" action="/verstuurd">
  <label for="naam">Naam *</label><input id="naam" name="naam" required>
  <label for="mail">E-mail *</label><input id="mail" name="email" type="email" required>
  <label>Soort klus <select name="soort"><option>Kies</option><option>Dakkapel</option></select></label>
  <label for="msg">Bericht</label><textarea id="msg" name="bericht"></textarea>
  <label><input type="checkbox" name="privacy" required> Ik ga akkoord met de privacyverklaring</label>
  <button type="submit">Verstuur aanvraag</button>
</form>%s</body></html>"""


class H(BaseHTTPRequestHandler):
    ontvangen = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        extra = '<div class="g-recaptcha" data-sitekey="x"></div>' if self.path == "/captcha" else ""
        self._stuur(FORM % extra)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        H.ontvangen = dict(p.split("=", 1) for p in self.rfile.read(n).decode().split("&"))
        self._stuur("<html><body><h1>Bedankt voor uw aanvraag!</h1></body></html>")

    def _stuur(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())


@pytest.fixture(scope="module")
def site():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_website_bekijken(site):
    info = browser.website_bekijken(site + "/contact")
    assert "info@dakbedrijf.nl" in info["emails"]
    assert info["telefoon"] == ["0101234567"]
    assert info["heeft_formulier"] and not info["captcha"]
    assert any("offerte" in l["url"] for l in info["relevante_links"])


def test_formulier_bekijken_en_invullen(site):
    info = browser.formulier_bekijken(site + "/contact")
    sels = {v["label"].split(" *")[0]: v for v in info["velden"]}
    assert sels["Naam"]["selector"] == "#naam" and sels["Naam"]["verplicht"]
    assert "Dakkapel" in next(v for v in info["velden"] if v["type"] == "select")["opties"]
    assert info["knoppen"][0]["tekst"] == "Verstuur aanvraag"

    velden = [{"selector": "#naam", "waarde": "M. Jansen"},
              {"selector": "#mail", "waarde": "opdracht@steenhub.nl"},
              {"selector": 'select[name="soort"]', "waarde": "Dakkapel"},
              {"selector": "#msg", "waarde": "Graag een offerte voor een prefab dakkapel."},
              {"selector": 'input[name="privacy"]', "waarde": "ja"}]
    uit = browser.formulier_invullen(site + "/contact", velden)
    assert uit["status"] == "verstuurd", uit
    assert H.ontvangen["email"] == "opdracht%40steenhub.nl" and H.ontvangen["soort"] == "Dakkapel"


def test_captcha_niet_versturen(site):
    uit = browser.formulier_invullen(site + "/captcha", [{"selector": "#naam", "waarde": "x"}])
    assert uit["status"] == "captcha"
