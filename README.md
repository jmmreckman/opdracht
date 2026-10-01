# Opdracht (opdracht.steenhub.nl)

Een persoonlijke assistent die opdrachten zelfstandig uitvoert, over meerdere dagen:
partijen zoeken, offertes aanvragen vanuit **opdracht@steenhub.nl** (per mail of via
het formulier op hun website), reacties lezen, verhelderende vragen beantwoorden, en
je aan het eind een vergelijking met advies mailen. Pure onderzoeksvragen ("beste e-bike
tot €1000") kan hij ook, maar daarvoor is de Claude-app zelf meestal net zo handig.

## Hoe het werkt

- **Nieuwe opdracht**: via het intakegesprek op de site, door een mail van jouw eigen
  adres naar opdracht@steenhub.nl, of vanuit Claude Code via de API (zie onderaan).
- **Rondes**: de assistent (Claude) krijgt telkens de hele stand van een opdracht
  en beslist wat er moet gebeuren. Een ronde draait:
  - direct bij de start,
  - elke dag om 09:15 en 15:45 (`RONDE_TIJDEN`),
  - ongeveer 10 minuten nadat er een reactie van een partij binnenkomt,
  - meteen als jij iets stuurt (een antwoord, een bericht of een mail met `[OPD-nr]` in het onderwerp),
  - op een moment dat de assistent zelf plant (bijvoorbeeld "over 3 werkdagen herinneringen sturen").
- **Zelfstandigheid** per opdracht:
  - *Alles eerst goedkeuren*: elke mail en elk formulier staat eerst op de site
    (en je krijgt een mail). Je kunt de tekst aanpassen voordat je op goedkeuren drukt.
  - *Eerste contact goedkeuren*: de eerste aanvraag per partij keur je goed,
    vervolgmails gaan vanzelf.
  - *Zelfstandig*: alles gaat vanzelf.
- **Wat hij nooit zelf doet** (dit staat in de instructies van de assistent): iets
  toezeggen, een opdracht gunnen, tekenen of betalen, een afspraak met datum vastleggen,
  of jouw naam, adres of telefoon delen. Daarvoor krijg je een vraag. Delen gebeurt
  alleen met wat onder *Mag gedeeld worden* staat.
- **Versturen** gebeurt alleen binnen kantoortijden (ma–za 8–19 uur), met minstens
  2 minuten tussen berichten en maximaal 30 per opdracht per dag. Dat oogt menselijk
  en voorkomt dat je in de spam belandt.
- **Formulieren** worden ingevuld met een echte browser (Chromium). Bij een captcha
  krijg je een mail met de in te vullen tekst en klik je daarna op *Zelf verstuurd*.
- **Offertes als PDF** in een reactie worden direct door de assistent gelezen.
- **Bestanden per opdracht** (tekening, vergunning, foto's) zet je onder *Bestanden* op
  de opdrachtpagina. De assistent leest ze (PDF, afbeelding, tekst) en haalt er
  bijvoorbeeld maten uit. Als bijlage meesturen in mails kan alleen bij bestanden met
  het vinkje *mag mee in mails*; dat zet je per bestand aan of uit.
- **Mails naar jou**: vragen en goedkeuringen direct, een dagelijkse samenvatting om
  18:30, en het eindrapport. Antwoord gewoon op zo'n mail: het `[OPD-nr]` in het
  onderwerp koppelt je antwoord aan de opdracht.

## Installatie op de VPS

Snelste weg: log in op de VPS en draai
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jmmreckman/opdracht/main/install.sh)
```
Dat haalt de code op, vraagt vier instellingen, start alles, voegt het Caddy-blok toe en
controleert Claude, SMTP en IMAP (met een testmail). Nog een keer draaien = bijwerken.
Hieronder dezelfde stappen met de hand.

Zelfde opzet als rommel.steenhub.nl: Docker, en Caddy regelt HTTPS.

### 1. DNS
Maak bij je domeinregistrar een A-record (of CNAME) voor `opdracht` naar dezelfde
server als steenhub.nl.

### 2. Mailbox controleren
Zorg dat opdracht@steenhub.nl werkt bij Strato. Stuur daarna een testmail vanaf dat
adres naar [mail-tester.com](https://www.mail-tester.com) en controleer dat SPF en DKIM
goed staan (bij Strato: *Domeinen → DNS → SPF/DKIM*). Staan die niet goed, dan belanden
je aanvragen bij bedrijven in de spam.

### 3. Claude API-sleutel
Maak op [console.anthropic.com](https://console.anthropic.com) een API-sleutel aan en
zet onder *Limits* een maandlimiet (bijvoorbeeld $30). Reken op €2–10 per opdracht van
een paar weken. De site laat per opdracht een kosteninschatting zien.

### 4. Code en instellingen
```bash
sudo mkdir -p /opt/opdracht && sudo chown $USER /opt/opdracht
git clone -b main https://github.com/jmmreckman/opdracht.git /opt/opdracht
cd /opt/opdracht
cp .env.example .env
nano .env        # vul in: SITE_PASSWORD, API_TOKEN, ANTHROPIC_API_KEY, SMTP_PASSWORD, AFZENDER_NAAM
docker compose up -d --build
curl http://127.0.0.1:8124/health     # moet {"ok":true} geven
```

### 5. Caddy
Voeg het blok uit `caddy-snippet.example` toe aan `/opt/kamerverhuur-scanner/deploy/Caddyfile`
en herlaad Caddy:
```bash
docker exec deploy-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```
(De automatische deploy hieronder doet dit ook, als het blok er nog niet staat.)

### 6. Automatisch deployen (optioneel)
Bij elke push draait GitHub de tests en zet de nieuwe versie op de VPS. Voeg daarvoor in
deze repo onder *Settings → Secrets and variables → Actions* dezelfde drie secrets toe
als in de kamerverhuur-scanner-repo: `DEPLOY_SSH_KEY`, `DEPLOY_HOST` en `DEPLOY_USER`.
Zonder die secrets draaien alleen de tests, en werk je bij met:
```bash
cd /opt/opdracht && git pull && docker compose up -d --build
```

### Eerste test
1. Log in op https://opdracht.steenhub.nl.
2. Het overzicht zegt of SMTP en IMAP goed zijn ingesteld en wanneer de mail voor
   het laatst is opgehaald.
3. Maak een opdracht en laat de eerste ronde lopen. Met *Alles eerst goedkeuren*
   gaat er niets de deur uit zonder jouw klik.

Logs bekijken: `docker compose logs -f`.

## Opdrachten vanuit Claude Code

Met `API_TOKEN` uit `.env`:
```bash
# nieuwe opdracht (start meteen; "start": false maakt een concept)
curl -X POST https://opdracht.steenhub.nl/api/extern/opdrachten \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"titel": "...", "type": "uitbesteden", "brief": "...", "criteria": "...",
       "deelbaar": "...", "deadline": "...", "autonomie": "alles_goedkeuren"}'

curl https://opdracht.steenhub.nl/api/extern/opdrachten/1 -H "Authorization: Bearer $API_TOKEN"   # stand + rapport
curl -X POST https://opdracht.steenhub.nl/api/extern/opdrachten/1/bericht \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" -d '{"tekst": "..."}'
```

## Lokaal ontwikkelen

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
SCHEDULER=0 SITE_PASSWORD=test SITE_URL=http://localhost:8000 uvicorn app.main:app --reload
```

## Opbouw

| Bestand | Wat |
|---|---|
| `app/agent.py` | De werkronde: context opbouwen, Claude met tools, goedkeuringsregels |
| `app/prompts.py` | Instructies voor de assistent (schrijfstijl, harde regels) en de intake |
| `app/inbox.py` | Binnenkomende mail koppelen aan opdracht en partij, en de verzendwachtrij |
| `app/mailer.py` | SMTP versturen, IMAP ophalen, bijlagen |
| `app/browser.py` | Websites bekijken en formulieren invullen (Playwright) |
| `app/scheduler.py` | Achtergrondlus: mail ophalen, versturen, rondes, dagelijkse samenvatting |
| `app/intake.py` | Het intakegesprek en opdrachten per mail |
| `app/main.py` | Website en API |
