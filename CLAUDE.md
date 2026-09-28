# Opdracht (opdracht.steenhub.nl)

Persoonlijke assistent van Jurian die opdrachten uitvoert vanuit opdracht@steenhub.nl.
Zie README.md voor de werking; code in `app/`, tests in `tests/` (`python -m pytest -q`).
Een push naar `claude/opdracht-steenhub-platform-kam5l2` wordt na geslaagde tests automatisch
naar de VPS gedeployed (`.github/workflows/deploy.yml`).

## Vragen over lopende opdrachten beantwoorden

Als Jurian vraagt naar resultaten, offertes of de stand van een opdracht: haal de data
op via de API met het alleen-lezen token uit de omgevingsvariabele `OPDRACHT_LEES_TOKEN`.

```bash
H="Authorization: Bearer $OPDRACHT_LEES_TOKEN"
curl -s -H "$H" https://opdracht.steenhub.nl/api/extern/opdrachten              # lijst
curl -s -H "$H" "https://opdracht.steenhub.nl/api/extern/opdrachten/1?alles=1"  # alles: partijen,
#   offertes, volledige mails, acties, logboek, rapport; bijlagen hebben een "url"
curl -s -H "$H" -o offerte.pdf "<bijlage-url>"                                  # PDF ophalen en lezen
```

Het token kan niets versturen of wijzigen. Wil Jurian dat de assistent iets doet, dan
geeft hij dat zelf door via "Bericht aan de assistent" op de site.
