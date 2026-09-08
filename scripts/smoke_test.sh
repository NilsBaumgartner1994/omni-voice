#!/usr/bin/env bash
# Prüft den laufenden Container end-to-end: Health, UI, Synthese.
# Erwartet einen erreichbaren Server (Standard: http://localhost:7860).
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:${OMNIVOICE_PORT:-7860}}"
TIMEOUT="${TIMEOUT:-120}"
OUT="${OUT:-$(mktemp -d)/smoke.wav}"

echo "Warte auf ${BASE_URL} (max. ${TIMEOUT}s) ..."
deadline=$((SECONDS + TIMEOUT))
until curl -fsS "${BASE_URL}/api/health" >/dev/null 2>&1; do
  if ((SECONDS >= deadline)); then
    echo "FEHLER: Server wurde nicht bereit." >&2
    curl -sS "${BASE_URL}/api/health" || true
    exit 1
  fi
  sleep 2
done
echo "OK: /api/health"

curl -fsS "${BASE_URL}/" >/dev/null && echo "OK: Weboberfläche"
curl -fsS "${BASE_URL}/api/info" >/dev/null && echo "OK: /api/info"

curl -fsS -X POST "${BASE_URL}/api/tts" \
  -H 'content-type: application/json' \
  -d '{"text":"Hallo aus dem Container."}' \
  -o "${OUT}"

python3 - "${OUT}" <<'PY'
import sys, wave

with wave.open(sys.argv[1]) as handle:
    seconds = handle.getnframes() / handle.getframerate()
assert seconds > 0.2, f"Audio ist zu kurz: {seconds}s"
print(f"OK: {seconds:.2f}s Audio erzeugt -> {sys.argv[1]}")
PY

echo "Smoke-Test erfolgreich."
