# Opdracht (opdracht.steenhub.nl)

Persoonlijke assistent van Jurian die opdrachten uitvoert vanuit opdracht@steenhub.nl.
Zie README.md voor de werking; code in `app/`, tests in `tests/` (`python -m pytest -q`).

## Code aanpassen en deployen

Een push naar `main` wordt na geslaagde tests automatisch naar de VPS gedeployed
(`.github/workflows/deploy.yml`, draait `docker compose up -d --build` in /opt/opdracht;
`.env` en `data/` blijven staan). Jurian wil dat wijzigingen live gaan: werk op je
sessie-branch, draai `python -m pytest -q`, en zet het daarna ook op `main`
(`git push origin HEAD:main`, na `git fetch origin main` en rebasen als main verder is).
Die toestemming geeft hij hierbij expliciet. Controleer na de push of de workflow groen is
en bekijk daarna de logs (zie hieronder) om te zien of het ook echt werkt.

## Vragen over lopende opdrachten beantwoorden

Als Jurian vraagt naar resultaten, offertes of de stand van een opdracht: haal de data
op via de API met het alleen-lezen token uit de omgevingsvariabele `OPDRACHT_LEES_TOKEN`.

```bash
H="Authorization: Bearer $OPDRACHT_LEES_TOKEN"
curl -s -H "$H" https://opdracht.steenhub.nl/api/extern/opdrachten              # lijst
curl -s -H "$H" "https://opdracht.steenhub.nl/api/extern/opdrachten/1?alles=1"  # alles: partijen,
#   offertes, volledige mails, acties, logboek, rapport; bijlagen hebben een "url"
curl -s -H "$H" -o offerte.pdf "<bijlage-url>"                                  # PDF ophalen en lezen
curl -s -H "$H" "https://opdracht.steenhub.nl/api/extern/logs?regels=300"       # applicatielog + logboek
```

Het token kan niets versturen of wijzigen. Wil Jurian dat de assistent iets doet, dan
geeft hij dat zelf door via "Bericht aan de assistent" op de site.
