from email.message import EmailMessage

from app import mailer


def _mail_met_pdf():
    msg = EmailMessage()
    msg["From"] = "Dakkapel Bedrijf <Info@Dakkapelbedrijf.nl>"
    msg["To"] = "opdracht@steenhub.nl"
    msg["Subject"] = "=?utf-8?q?Re=3A_Offerteaanvraag_dakkapel_=E2=82=AC?="
    msg["Message-ID"] = "<abc@dakkapelbedrijf.nl>"
    msg["In-Reply-To"] = "<ons1@steenhub.nl>"
    msg["Date"] = "Mon, 28 Sep 2026 10:00:00 +0200"
    msg.set_content("Beste,\n\nIn de bijlage onze offerte.\n\nOp 27 sep. 2026 schreef opdracht@steenhub.nl:\n> oude tekst")
    msg.add_alternative("<p>Beste,</p><p>In de bijlage <b>onze</b> offerte.</p>", subtype="html")
    msg.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="offerte.pdf")
    return msg.as_bytes()


def test_parse_mail_met_bijlage():
    m = mailer.parse_mail(_mail_met_pdf())
    assert m["van"] == "info@dakkapelbedrijf.nl"
    assert m["onderwerp"] == "Re: Offerteaanvraag dakkapel €"
    assert m["in_reply_to"] == "<ons1@steenhub.nl>"
    assert m["datum"] == "2026-09-28 08:00:00"
    assert "offerte" in m["tekst"]
    assert m["bijlagen"][0]["naam"] == "offerte.pdf"
    assert not m["automatisch"]


def test_zonder_citaat():
    tekst = "Prima, woensdag kan.\n\nOp 27 sep. 2026 om 10:00 schreef Jan <jan@x.nl>:\n> vraag"
    assert mailer.zonder_citaat(tekst) == "Prima, woensdag kan."


def test_html_only():
    msg = EmailMessage()
    msg["From"] = "a@b.nl"
    msg["Subject"] = "x"
    msg.set_content("<html><body><p>Hallo</p><br>daar<script>x()</script></body></html>", subtype="html")
    assert mailer.parse_mail(msg.as_bytes())["tekst"] == "Hallo\n\ndaar"


def test_bouw_mail_threading(monkeypatch):
    monkeypatch.setenv("AFZENDER_NAAM", "M. Jansen")
    msg = mailer.bouw_mail("info@x.nl", "Re: offerte", "tekst", "<a@x.nl>", "<0@steenhub.nl>")
    assert msg["From"] == "\"M. Jansen\" <opdracht@steenhub.nl>"
    assert msg["In-Reply-To"] == "<a@x.nl>"
    assert msg["References"] == "<0@steenhub.nl> <a@x.nl>"
    assert msg["Message-ID"].endswith("@steenhub.nl>")
