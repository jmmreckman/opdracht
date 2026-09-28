#!/usr/bin/env bash
# Installeert of werkt opdracht.steenhub.nl bij op de VPS. Gebruik:
#   bash <(curl -fsSL https://raw.githubusercontent.com/jmmreckman/opdracht/claude/opdracht-steenhub-platform-kam5l2/install.sh)
# Nog een keer draaien = bijwerken (je instellingen blijven bewaard).
# Met "opnieuw" als argument vraagt hij de instellingen opnieuw.
set -euo pipefail

BRANCH="claude/opdracht-steenhub-platform-kam5l2"
REPO="https://github.com/jmmreckman/opdracht.git"
DIR=/opt/opdracht
CADDYFILE=/opt/kamerverhuur-scanner/deploy/Caddyfile
CADDY_CONTAINER=deploy-caddy-1

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

stap() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
vraag() {  # vraag <variabele> <tekst> [geheim]
  local antwoord
  if [ "${3:-}" = "geheim" ]; then
    read -rsp "$2: " antwoord < /dev/tty; echo
  else
    read -rp "$2: " antwoord < /dev/tty
  fi
  printf -v "$1" '%s' "$antwoord"
}

stap "Code ophalen naar $DIR"
if [ ! -d "$DIR/.git" ]; then
  $SUDO mkdir -p "$DIR"
  $SUDO chown "$(id -u):$(id -g)" "$DIR"
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
else
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
fi
cd "$DIR"
echo "Versie: $(git log -1 --format='%h %s')"

if [ ! -f .env ] || [ "${1:-}" = "opnieuw" ]; then
  stap "Instellingen (worden alleen op deze server bewaard, in $DIR/.env)"
  echo "Wat je typt bij wachtwoorden en sleutels is onzichtbaar; dat is normaal."
  vraag ANTHROPIC_KEY "1/4 Claude API-sleutel (begint met sk-ant-)" geheim
  vraag MAIL_WW "2/4 Wachtwoord van opdracht@steenhub.nl" geheim
  vraag SITE_WW "3/4 Kies een wachtwoord om in te loggen op opdracht.steenhub.nl" geheim
  vraag NAAM "4/4 Naam onder mails en in formulieren (Enter = geen naam)"
  API_TOKEN=$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  cp .env.example .env
  zet() {  # vervang KEY=... in .env zonder dat speciale tekens stuk gaan
    python3 - "$1" "$2" <<'PY'
import sys, re
sleutel, waarde = sys.argv[1], sys.argv[2]
# Docker Compose leest '...' letterlijk (geen $-vervanging); alleen als er zelf
# een ' in zit, wordt het "..." met escapes.
if "'" not in waarde:
    waarde = f"'{waarde}'"
else:
    waarde = '"' + waarde.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'
tekst = open(".env").read()
tekst = re.sub(rf"^{sleutel}=.*$", lambda m: f"{sleutel}={waarde}", tekst, flags=re.M)
open(".env", "w").write(tekst)
PY
  }
  zet ANTHROPIC_API_KEY "$ANTHROPIC_KEY"
  zet SMTP_PASSWORD "$MAIL_WW"
  zet SITE_PASSWORD "$SITE_WW"
  zet AFZENDER_NAAM "$NAAM"
  zet API_TOKEN "$API_TOKEN"
  chmod 600 .env
  echo "Opgeslagen."
else
  echo "Bestaande instellingen in $DIR/.env blijven staan (draai met 'opnieuw' om ze te wijzigen)."
fi

stap "Starten (eerste keer duurt een paar minuten: browser wordt gedownload)"
mkdir -p data
$SUDO docker compose up -d --build
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8124/health >/dev/null 2>&1; then echo "App draait."; break; fi
  sleep 2
  [ "$i" = 30 ] && { echo "App start niet. Logs:"; $SUDO docker compose logs --tail 40; exit 1; }
done

stap "Caddy (https://opdracht.steenhub.nl)"
if [ -f "$CADDYFILE" ]; then
  if grep -q "opdracht.steenhub.nl" "$CADDYFILE"; then
    echo "Staat er al in."
  else
    printf '\nopdracht.steenhub.nl {\n    reverse_proxy opdracht:8000\n}\n' | $SUDO tee -a "$CADDYFILE" >/dev/null
    $SUDO docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    echo "Toegevoegd en herladen."
  fi
else
  echo "LET OP: $CADDYFILE niet gevonden. Voeg dit blok zelf toe aan je Caddyfile:"
  cat caddy-snippet.example
fi

stap "Controle: Claude, uitgaande en inkomende mail"
if $SUDO docker compose exec -T opdracht python -m app.check --testmail; then
  printf '\n\033[1mKlaar!\033[0m Ga naar https://opdracht.steenhub.nl en check je mail (%s).\n' \
    "$(grep '^NOTIFY_EMAIL=' .env | cut -d= -f2)"
else
  printf '\nDe app draait, maar de controle vond een probleem (zie hierboven).\n'
  printf 'Stuur de uitvoer naar Claude. Instellingen opnieuw invullen: draai dit script met "opnieuw".\n'
fi
