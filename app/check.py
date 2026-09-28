"""Controleert of alles goed staat: Claude API, SMTP en IMAP.
Draaien in de container:  docker compose exec opdracht python -m app.check
Met --testmail stuurt hij ook een testmail naar NOTIFY_EMAIL."""
import imaplib
import smtplib
import ssl
import sys

from . import config, mailer


def _ok(tekst):
    print(f"  OK    {tekst}")


def _fout(tekst):
    print(f"  FOUT  {tekst}")


def check_claude() -> bool:
    if not config.env("ANTHROPIC_API_KEY"):
        _fout("ANTHROPIC_API_KEY ontbreekt in .env")
        return False
    try:
        import anthropic
        model = anthropic.Anthropic().models.retrieve(config.claude_model())
        _ok(f"Claude API werkt ({model.id})")
        return True
    except Exception as e:  # noqa: BLE001
        _fout(f"Claude API: {e}")
        return False


def check_smtp() -> bool:
    host, port = config.env("SMTP_HOST"), config.env_int("SMTP_PORT", 587)
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls(context=ctx)
        server.login(config.env("SMTP_USER"), config.env("SMTP_PASSWORD"))
        server.quit()
        _ok(f"Uitgaande mail (SMTP {host}:{port}) inloggen gelukt")
        return True
    except Exception as e:  # noqa: BLE001
        _fout(f"SMTP {host}:{port}: {e}")
        return False


def check_imap() -> bool:
    try:
        imap = mailer._imap()
        typ, _ = imap.select(config.env("IMAP_FOLDER", "INBOX"), readonly=True)
        typ2, mappen = imap.list()
        imap.logout()
        if typ != "OK":
            _fout("IMAP: inloggen gelukt maar map INBOX niet gevonden")
            return False
        _ok(f"Inkomende mail (IMAP {config.env('IMAP_HOST')}) inloggen gelukt")
        namen = [m.decode(errors="replace").split(' "/" ')[-1] for m in (mappen or []) if m]
        print(f"        mappen: {', '.join(namen[:12])}")
        return True
    except (imaplib.IMAP4.error, Exception) as e:  # noqa: BLE001
        _fout(f"IMAP: {e}")
        return False


def main():
    print("Controle opdracht.steenhub.nl")
    goed = all([check_claude(), check_smtp(), check_imap()])
    if goed and "--testmail" in sys.argv and config.notify_email():
        try:
            mailer.verstuur(config.notify_email(), "Opdracht-assistent staat aan",
                            f"Het werkt! Ga naar {config.site_url()} om je eerste opdracht te maken.",
                            van_naam="Opdracht-assistent")
            _ok(f"Testmail verstuurd naar {config.notify_email()}")
        except Exception as e:  # noqa: BLE001
            _fout(f"Testmail: {e}")
            goed = False
    print("Alles in orde." if goed else "Er is iets niet in orde, zie hierboven.")
    sys.exit(0 if goed else 1)


if __name__ == "__main__":
    main()
