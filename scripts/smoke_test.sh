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

# Der eben gemessene Lauf muss in der Dauerprognose auftauchen.
curl -fsS "${BASE_URL}/api/estimate?text_chars=100" -o "${OUT}.estimate.json"

python3 - "${OUT}.estimate.json" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
assert data["estimate_seconds"] is not None, f"Keine Prognose gelernt: {data}"
assert data["history"]["count"] >= 1, f"Lauf nicht gespeichert: {data}"
where = "dauerhaft" if data["history"]["persistent"] else "nur im Speicher"
print(f"OK: Dauerprognose {data['estimate_seconds']}s ({where})")
PY

echo "Smoke-Test erfolgreich."
