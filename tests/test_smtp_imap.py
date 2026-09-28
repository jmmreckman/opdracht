"""SMTP echt end-to-end (STARTTLS op 587-stijl én implicit TLS op 465) tegen een
lokale aiosmtpd-server; IMAP met een nagebootste server."""
import ssl
import subprocess

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.handlers import Message

from app import mailer


class Opvang(Message):
    def __init__(self):
        super().__init__()
        self.berichten = []

    def handle_message(self, message):
        self.berichten.append(message)


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", d / "k.pem",
                    "-out", d / "c.pem", "-days", "1", "-subj", "/CN=localhost"],
                   check=True, capture_output=True)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    return ctx, d / "c.pem"


def _vrije_poort():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("implicit", [False, True])
def test_smtp_verstuur(cert, monkeypatch, implicit):
    ctx, _ = cert
    opvang = Opvang()
    poort = _vrije_poort()
    kw = {"server_hostname": "localhost", "ssl_context": ctx} if implicit else {"tls_context": ctx, "require_starttls": True}
    ctl = Controller(opvang, hostname="127.0.0.1", port=poort, **kw)
    ctl.start()
    try:
        monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SMTP_PORT", "465" if implicit else str(poort))
        if implicit:
            # Mailer kiest SMTP_SSL alleen bij poort 465: stuur die door naar de testpoort.
            import smtplib
            echt = smtplib.SMTP_SSL
            monkeypatch.setattr(smtplib, "SMTP_SSL", lambda h, p, **k: echt(h, poort, **k))
        # Zelfondertekend testcertificaat accepteren.
        monkeypatch.setattr(mailer.ssl, "create_default_context", lambda *a, **k: ssl._create_unverified_context())
        mid = mailer.verstuur("info@bedrijf.nl", "Offerteaanvraag", "Goedendag,\n\nTest.")
    finally:
        ctl.stop()
    assert mid and opvang.berichten[0]["Message-ID"] == mid
    assert opvang.berichten[0]["To"] == "info@bedrijf.nl"


class NepIMAP:
    def __init__(self, mails):
        self.mails = mails

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def select(self, map_, readonly=True):
        return "OK", [b"1"]

    def status(self, map_, wat):
        return "OK", [b'INBOX (UIDVALIDITY 42)']

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            crit = args[-1]
            if crit == "ALL":
                return "OK", [" ".join(str(u) for u in self.mails).encode()]
            start = int(crit.split()[1].split(":")[0])
            uids = [u for u in self.mails if u >= start] or [max(self.mails)]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        return "OK", [(b"x", self.mails[uid])]


def test_imap_eerste_keer_en_nieuw(monkeypatch):
    ruw = b"From: a@b.nl\r\nSubject: hoi\r\nMessage-ID: <1@b.nl>\r\n\r\nTekst\r\n"
    mails = {5: ruw, 6: ruw.replace(b"<1@", b"<2@")}
    monkeypatch.setattr(mailer, "_imap", lambda: NepIMAP(mails))
    lijst, staat = mailer.haal_nieuwe_mails("")
    assert lijst == [] and staat == "42:6"          # oude mail overslaan
    mails[7] = ruw.replace(b"<1@", b"<3@")
    lijst, staat = mailer.haal_nieuwe_mails(staat)
    assert [m["message_id"] for m in lijst] == ["<3@b.nl>"] and staat == "42:7"
    assert mailer.haal_nieuwe_mails(staat) == ([], "42:7")  # "n:*" geeft de laatste terug: niet dubbel
