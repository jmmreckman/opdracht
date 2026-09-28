"""Mail versturen (SMTP) en uitlezen (IMAP) voor opdracht@steenhub.nl.

Zelfde aanpak als rommel.steenhub.nl: gewone SMTP/IMAP met de inloggegevens
van het hosting-mailaccount (Strato). Poort 465 = SMTP_SSL, 587 = STARTTLS."""
import email
import imaplib
import json
import logging
import re
import smtplib
import ssl
import time
import uuid
from datetime import timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime

from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger("opdracht")


class MailFout(Exception):
    pass


# ---------------------------------------------------------------- versturen

def _smtp_verstuur(msg: EmailMessage) -> None:
    host = config.env("SMTP_HOST")
    if not host:
        raise MailFout("SMTP_HOST is niet ingesteld")
    port = config.env_int("SMTP_PORT", 587)
    gebruiker = config.env("SMTP_USER")
    wachtwoord = config.env("SMTP_PASSWORD")
    context = ssl.create_default_context()
    if port == 465:
        # Poort 465: direct versleuteld vanaf de eerste byte (SMTPS).
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
            if gebruiker and wachtwoord:
                server.login(gebruiker, wachtwoord)
            server.send_message(msg)
    else:
        # Poort 587 (of 25): eerst plain, dan opwaarderen met STARTTLS.
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.starttls(context=context)
            if gebruiker and wachtwoord:
                server.login(gebruiker, wachtwoord)
            server.send_message(msg)


def _domein(adres: str) -> str:
    return adres.rsplit("@", 1)[-1].lower() if "@" in adres else "localhost"


def bouw_mail(aan: str, onderwerp: str, tekst: str, in_reply_to: str = "",
              references: str = "", van_naam: str | None = None) -> EmailMessage:
    afzender = config.opdracht_email()
    if not afzender:
        raise MailFout("OPDRACHT_EMAIL/SMTP_USER is niet ingesteld")
    naam = config.afzender_naam() if van_naam is None else van_naam
    msg = EmailMessage()
    msg["Subject"] = onderwerp
    msg["From"] = formataddr((naam, afzender)) if naam else afzender
    msg["To"] = aan
    msg["Message-ID"] = make_msgid(domain=_domein(afzender))
    msg["Date"] = email.utils.formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    msg.set_content(tekst)
    return msg


def verstuur(aan: str, onderwerp: str, tekst: str, in_reply_to: str = "",
             references: str = "", van_naam: str | None = None) -> str:
    """Verstuurt een mail en geeft het Message-ID terug. Gooit MailFout bij
    problemen - de aanroeper beslist wat er dan moet gebeuren."""
    msg = bouw_mail(aan, onderwerp, tekst, in_reply_to, references, van_naam)
    try:
        _smtp_verstuur(msg)
    except MailFout:
        raise
    except Exception as e:  # noqa: BLE001 - alle SMTP/netwerkfouten
        raise MailFout(f"Versturen mislukt: {e}") from e
    _bewaar_in_verzonden(msg)
    return msg["Message-ID"]


def meld_eigenaar(onderwerp: str, tekst: str) -> None:
    """Mail aan jou (NOTIFY_EMAIL). Mag de rest van de app nooit breken."""
    ontvanger = config.notify_email()
    if not ontvanger or not config.env("SMTP_HOST"):
        return
    try:
        verstuur(ontvanger, onderwerp, tekst, van_naam="Opdracht-assistent")
    except Exception:  # noqa: BLE001
        logger.exception("Kon melding aan eigenaar niet versturen")


def _bewaar_in_verzonden(msg: EmailMessage) -> None:
    """Optioneel: kopie in de map Verzonden, zodat je het ook in webmail ziet."""
    map_ = config.env("IMAP_SENT_FOLDER")
    if not map_ or not config.env("IMAP_HOST"):
        return
    try:
        with _imap() as imap:
            imap.append(map_, "\\Seen", imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes())
    except Exception:  # noqa: BLE001
        logger.warning("Kon verzonden mail niet in '%s' bewaren", map_, exc_info=True)


# ---------------------------------------------------------------- uitlezen

def _imap() -> imaplib.IMAP4:
    host = config.env("IMAP_HOST")
    if not host:
        raise MailFout("IMAP_HOST is niet ingesteld")
    port = config.env_int("IMAP_PORT", 993)
    gebruiker = config.env("IMAP_USER") or config.env("SMTP_USER")
    wachtwoord = config.env("IMAP_PASSWORD") or config.env("SMTP_PASSWORD")
    if port == 993:
        imap = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(),
                                 timeout=60)
    else:
        imap = imaplib.IMAP4(host, port, timeout=60)
        imap.starttls(ssl_context=ssl.create_default_context())
    imap.login(gebruiker, wachtwoord)
    return imap


def haal_nieuwe_mails(staat: str) -> tuple[list[dict], str]:
    """Haalt nieuwe mails op uit INBOX (zonder ze als gelezen te markeren).
    `staat` is "<uidvalidity>:<laatste uid>" van de vorige keer. Bij de
    allereerste keer (of als de server de UID's heeft omgenummerd) wordt
    alleen de huidige hoogste UID onthouden: oude mail in de bus negeren we."""
    map_ = config.env("IMAP_FOLDER", "INBOX")
    with _imap() as imap:
        typ, data = imap.select(map_, readonly=True)
        if typ != "OK":
            raise MailFout(f"Kan map {map_} niet openen")
        uidvalidity = _uidvalidity(imap, map_)
        vorige_validity, _, vorige_uid = (staat or "").partition(":")
        laatste_uid = int(vorige_uid) if vorige_uid.isdigit() and vorige_validity == uidvalidity else None
        start = (laatste_uid or 0) + 1
        typ, data = imap.uid("SEARCH", None, f"UID {start}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split())
        # "n:*" geeft altijd minstens de laatste mail terug, ook als die oud is.
        uids = [u for u in uids if u >= start]
        if laatste_uid is None:
            typ, data = imap.uid("SEARCH", None, "ALL")
            alle = [int(u) for u in (data[0] or b"").split()]
            return [], f"{uidvalidity}:{max(alle) if alle else 0}"
        mails = []
        hoogste = laatste_uid
        for uid in uids:
            typ, msgdata = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
                continue
            try:
                geparsed = parse_mail(msgdata[0][1])
                geparsed["uid"] = uid
                mails.append(geparsed)
            except Exception:  # noqa: BLE001
                logger.exception("Kon mail met UID %s niet lezen", uid)
            hoogste = max(hoogste, uid)
        return mails, f"{uidvalidity}:{hoogste}"


def _uidvalidity(imap, map_) -> str:
    try:
        typ, data = imap.status(map_, "(UIDVALIDITY)")
        m = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"")
        return m.group(1).decode() if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _decode(waarde) -> str:
    if not waarde:
        return ""
    try:
        return str(make_header(decode_header(str(waarde))))
    except Exception:  # noqa: BLE001
        return str(waarde)


def html_naar_tekst(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    tekst = soup.get_text("\n")
    tekst = re.sub(r"[ \t\xa0]+", " ", tekst)
    tekst = re.sub(r"\n\s*\n\s*\n+", "\n\n", tekst)
    return tekst.strip()


def parse_mail(ruw: bytes) -> dict:
    msg = email.message_from_bytes(ruw)
    van_lijst = getaddresses([msg.get("From", "")])
    van_naam, van_adres = van_lijst[0] if van_lijst else ("", "")
    tekst_plain, tekst_html = "", ""
    bijlagen = []
    for deel in msg.walk():
        if deel.is_multipart():
            continue
        ctype = deel.get_content_type()
        disp = (deel.get("Content-Disposition") or "").lower()
        bestandsnaam = _decode(deel.get_filename() or "")
        if bestandsnaam or "attachment" in disp:
            inhoud = deel.get_payload(decode=True) or b""
            if inhoud:
                bijlagen.append(_bewaar_bijlage(bestandsnaam or "bijlage", ctype, inhoud))
            continue
        if ctype == "text/plain" and not tekst_plain:
            tekst_plain = _payload_tekst(deel)
        elif ctype == "text/html" and not tekst_html:
            tekst_html = _payload_tekst(deel)
    tekst = tekst_plain.strip() or html_naar_tekst(tekst_html)
    try:
        datum = parsedate_to_datetime(msg.get("Date"))
        if datum.tzinfo is None:
            datum = datum.replace(tzinfo=timezone.utc)
        datum_str = datum.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        datum_str = ""
    auto = bool(
        (msg.get("Auto-Submitted", "no").lower() not in ("", "no"))
        or msg.get("X-Autoreply") or msg.get("X-Autorespond")
        or (msg.get("Precedence", "").lower() in ("bulk", "auto_reply", "junk"))
        or van_adres.lower().startswith(("mailer-daemon", "postmaster"))
    )
    return {
        "van": van_adres.lower(),
        "van_naam": _decode(van_naam),
        "aan": msg.get("To", ""),
        "onderwerp": _decode(msg.get("Subject", "")),
        "tekst": tekst,
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": " ".join((msg.get("References") or "").split()),
        "auth_results": " ".join(str(h) for h in msg.get_all("Authentication-Results", [])),
        "automatisch": auto,
        "bijlagen": bijlagen,
        "datum": datum_str,
    }


def _payload_tekst(deel) -> str:
    inhoud = deel.get_payload(decode=True) or b""
    charset = deel.get_content_charset() or "utf-8"
    try:
        return inhoud.decode(charset, errors="replace")
    except LookupError:
        return inhoud.decode("utf-8", errors="replace")


def _bewaar_bijlage(naam: str, ctype: str, inhoud: bytes) -> dict:
    veilig = re.sub(r"[^A-Za-z0-9._-]+", "_", naam)[-80:] or "bijlage"
    bestand = f"{uuid.uuid4().hex[:12]}_{veilig}"
    config.BIJLAGEN_DIR.mkdir(parents=True, exist_ok=True)
    (config.BIJLAGEN_DIR / bestand).write_bytes(inhoud)
    return {"naam": naam, "type": ctype, "bestand": bestand, "grootte": len(inhoud)}


# ---------------------------------------------------------------- hulpjes

CITAAT_START = re.compile(
    r"^(Op .{5,200}(schreef|wrote).*:|On .{5,200}wrote:|-{2,} ?Oorspronkelijk bericht|"
    r"-{2,} ?Original Message|Van: .+|From: .+)\s*$",
    re.IGNORECASE,
)


def zonder_citaat(tekst: str) -> str:
    """Knipt het geciteerde vorige bericht onder een reactie weg."""
    regels = []
    for regel in tekst.splitlines():
        if CITAAT_START.match(regel.strip()):
            break
        if regel.startswith(">"):
            continue
        regels.append(regel)
    kort = "\n".join(regels).strip()
    return kort or tekst.strip()


def adressen_in(header: str) -> list[str]:
    return [a.lower() for _, a in getaddresses([header]) if a]


def dump_bijlagen(bijlagen: list[dict]) -> str:
    return json.dumps(bijlagen, ensure_ascii=False)
