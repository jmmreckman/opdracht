"""Een echte browser (Chromium via Playwright) om websites te bekijken en
contact-/offerteformulieren in te vullen. Synchrone API: wordt altijd vanuit
een worker-thread aangeroepen, nooit vanuit de asyncio-loop zelf."""
import logging
import re
import uuid
from contextlib import contextmanager

from . import config

logger = logging.getLogger("opdracht")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CAPTCHA_SELECTOR = (
    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
    "iframe[src*='captcha'], .g-recaptcha, .h-captcha, .cf-turnstile, [data-sitekey]"
)
COOKIE_KNOPPEN = [
    "Alles accepteren", "Alle cookies accepteren", "Accepteer alle cookies", "Accepteren",
    "Akkoord", "Accepteer", "Ik ga akkoord", "Toestaan", "Alles toestaan", "Accept all",
    "Accept", "OK",
]
SUCCES_RE = re.compile(
    r"bedankt|dank je|dank u|dankjewel|is verzonden|succesvol|ontvangen|"
    r"we nemen .{0,30}contact|nemen wij .{0,30}contact|thank you|has been sent|message sent",
    re.IGNORECASE,
)


class BrowserFout(Exception):
    pass


@contextmanager
def _pagina():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        opties = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        pad = config.env("CHROMIUM_PATH")
        if pad:
            opties["executable_path"] = pad
        browser = pw.chromium.launch(**opties)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT, locale="nl-NL", timezone_id="Europe/Amsterdam",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.set_default_timeout(20000)
            yield page
        finally:
            browser.close()
    finally:
        pw.stop()


def _open(page, url: str) -> None:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa: BLE001
        raise BrowserFout(f"Kon {url} niet openen: {e}") from e
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 - sommige sites worden nooit 'idle'
        pass
    _cookies_wegklikken(page)


def _cookies_wegklikken(page) -> None:
    for tekst in COOKIE_KNOPPEN:
        try:
            knop = page.get_by_role("button", name=tekst, exact=True)
            if knop.count() and knop.first.is_visible():
                knop.first.click(timeout=2000)
                page.wait_for_timeout(500)
                return
        except Exception:  # noqa: BLE001
            continue


# Draait in de pagina: beschrijft alle invulvelden met een bruikbare selector.
_VELDEN_JS = r"""
() => {
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/([^a-zA-Z0-9_-])/g, '\\$1');
  const selectorVoor = (el) => {
    if (el.id) return '#' + esc(el.id);
    const tag = el.tagName.toLowerCase();
    if (el.name) {
      const sel = `${tag}[name="${el.name.replace(/"/g, '\\"')}"]`;
      if (el.type === 'radio') return sel + `[value="${(el.value||'').replace(/"/g, '\\"')}"]`;
      if (document.querySelectorAll(sel).length === 1) return sel;
    }
    const alle = Array.from(document.querySelectorAll(tag));
    return `${tag} >> nth=${alle.indexOf(el)}`;
  };
  const labelVoor = (el) => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) t = l.innerText; }
    if (!t && el.closest('label')) t = el.closest('label').innerText;
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) {
      const wrap = el.closest('div,p,li,fieldset');
      if (wrap) { const l = wrap.querySelector('label,legend,span'); if (l && l !== el) t = l.innerText; }
    }
    return (t || '').trim().replace(/\s+/g, ' ').slice(0, 120);
  };
  const zichtbaar = (el) => {
    const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const velden = [];
  document.querySelectorAll('input, textarea, select').forEach((el) => {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','reset','image','search'].includes(type)) return;
    if (!zichtbaar(el) && !['checkbox','radio'].includes(type)) return;
    const v = {
      selector: selectorVoor(el), type, label: labelVoor(el),
      naam: el.name || '', placeholder: el.placeholder || '',
      verplicht: el.required || el.getAttribute('aria-required') === 'true',
      formulier: el.form ? Array.from(document.forms).indexOf(el.form) : -1,
    };
    if (type === 'select') v.opties = Array.from(el.options).map(o => o.text.trim()).filter(Boolean).slice(0, 40);
    if (type === 'radio' || type === 'checkbox') v.waarde = el.value;
    velden.push(v);
  });
  const knoppen = [];
  document.querySelectorAll('button, input[type=submit]').forEach((el) => {
    if (!zichtbaar(el)) return;
    const t = (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
    const type = (el.getAttribute('type') || (el.form ? 'submit' : 'button')).toLowerCase();
    if (type === 'submit' || /verstu|verzend|aanvra|send|submit|offerte/i.test(t))
      knoppen.push({selector: selectorVoor(el), tekst: t.slice(0, 60)});
  });
  return {velden, knoppen};
}
"""


def website_bekijken(url: str, max_tekens: int = 12000) -> dict:
    """Tekst, e-mailadressen, telefoonnummers, contactlinks en formulieren van een pagina."""
    with _pagina() as page:
        _open(page, url)
        tekst = page.inner_text("body") if page.locator("body").count() else ""
        html = page.content()
        mailtos = page.eval_on_selector_all(
            "a[href^='mailto:']", "els => els.map(e => e.getAttribute('href'))")
        tels = page.eval_on_selector_all(
            "a[href^='tel:']", "els => els.map(e => e.getAttribute('href'))")
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [e.innerText.trim().slice(0,60), e.href])",
        )
        velden = page.evaluate(_VELDEN_JS)
        captcha = page.locator(CAPTCHA_SELECTOR).count() > 0
        eind_url = page.url

    emails = set()
    for m in mailtos:
        emails.add(m.split(":", 1)[1].split("?")[0].strip().lower())
    for m in EMAIL_RE.findall(tekst + " " + html):
        if not m.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            emails.add(m.lower())
    relevante_links = [
        {"tekst": t, "url": h} for t, h in links
        if re.search(r"contact|offerte|aanvra|prijs|over-ons|over ons|about|dakkapel",
                     (t + " " + h), re.IGNORECASE)
    ][:25]
    return {
        "url": eind_url,
        "emails": sorted(emails)[:20],
        "telefoon": sorted({t.split(":", 1)[1] for t in tels})[:5],
        "relevante_links": relevante_links,
        "heeft_formulier": bool(velden["velden"]),
        "aantal_formuliervelden": len(velden["velden"]),
        "captcha": captcha,
        "tekst": tekst[:max_tekens],
    }


def formulier_bekijken(url: str) -> dict:
    with _pagina() as page:
        _open(page, url)
        info = page.evaluate(_VELDEN_JS)
        captcha = page.locator(CAPTCHA_SELECTOR).count() > 0
        info["url"] = page.url
        info["captcha"] = captcha
        return info


def formulier_invullen(url: str, velden: list[dict], verstuur_selector: str = "",
                       echt_versturen: bool = True) -> dict:
    """Vult het formulier in en verstuurt het. Geeft status terug:
    'verstuurd', 'onzeker' (geen bevestiging gezien), 'captcha' of 'fout'."""
    config.SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    naam = uuid.uuid4().hex[:10]
    with _pagina() as page:
        _open(page, url)
        fouten = []
        for veld in velden:
            sel, waarde = veld.get("selector", ""), str(veld.get("waarde", ""))
            if not sel:
                continue
            try:
                el = page.locator(sel).first
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                type_ = (el.get_attribute("type") or "").lower()
                if tag == "select":
                    try:
                        el.select_option(label=waarde, timeout=5000)
                    except Exception:  # noqa: BLE001
                        el.select_option(value=waarde, timeout=5000)
                elif type_ in ("checkbox", "radio"):
                    if waarde.lower() in ("1", "true", "ja", "aan", "on", "yes", "x") or type_ == "radio":
                        el.check(timeout=5000, force=True)
                    else:
                        el.uncheck(timeout=5000, force=True)
                else:
                    el.fill(waarde, timeout=5000)
            except Exception as e:  # noqa: BLE001
                fouten.append(f"{sel}: {str(e).splitlines()[0][:150]}")
        voor = f"{naam}_voor.png"
        page.screenshot(path=str(config.SCREENSHOTS_DIR / voor), full_page=True)

        if page.locator(CAPTCHA_SELECTOR).count() > 0:
            return {"status": "captcha", "fouten": fouten, "screenshot": voor,
                    "melding": "Formulier heeft een captcha; moet handmatig verstuurd worden."}
        if fouten and len(fouten) >= max(1, len(velden) // 2):
            return {"status": "fout", "fouten": fouten, "screenshot": voor,
                    "melding": "Te veel velden konden niet ingevuld worden."}
        if not echt_versturen:
            return {"status": "proef", "fouten": fouten, "screenshot": voor, "melding": ""}

        url_voor = page.url
        tekst_voor = page.inner_text("body")
        try:
            if verstuur_selector:
                page.locator(verstuur_selector).first.click(timeout=8000)
            else:
                knop = page.locator(
                    "form button[type=submit], form input[type=submit], "
                    "form button:not([type])").first
                knop.click(timeout=8000)
        except Exception as e:  # noqa: BLE001
            return {"status": "fout", "fouten": fouten + [f"verstuurknop: {e}"],
                    "screenshot": voor, "melding": "Kon de verstuurknop niet vinden/klikken."}
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(2000)
        na = f"{naam}_na.png"
        page.screenshot(path=str(config.SCREENSHOTS_DIR / na), full_page=True)
        tekst_na = page.inner_text("body")
        nieuwe_tekst = tekst_na.replace(tekst_voor, "") if tekst_na != tekst_voor else ""
        gelukt = bool(SUCCES_RE.search(nieuwe_tekst or "")) or (
            page.url != url_voor and bool(SUCCES_RE.search(tekst_na)))
        return {
            "status": "verstuurd" if gelukt else "onzeker",
            "fouten": fouten,
            "screenshot": na,
            "melding": "" if gelukt else "Geen duidelijke bevestiging gezien na versturen.",
        }
