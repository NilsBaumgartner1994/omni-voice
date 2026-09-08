#!/usr/bin/env bash
set -euo pipefail

command="${1:-serve}"
shift || true

case "${command}" in
  serve)
    # Kleine eigene Weboberfläche + REST-API (Standard).
    exec python -m omnivoice_server "$@"
    ;;
  gradio)
    # Die originale Gradio-Oberfläche von OmniVoice, aber mit passendem
    # Gerät/dtype (Upstream erzwingt float16, das ist auf CPU unbrauchbar).
    exec python -m omnivoice_server.gradio_app "$@"
    ;;
  prefetch)
    # Modellgewichte vorab in das Volume laden.
    exec python /app/scripts/prefetch_models.py "$@"
    ;;
  infer)
    exec omnivoice-infer "$@"
    ;;
  shell)
    exec bash "$@"
    ;;
  help | --help | -h)
    cat <<'USAGE'
Verfügbare Kommandos:
  serve      Weboberfläche + REST-API starten (Standard)
  gradio     Originale Gradio-Oberfläche starten
  prefetch   Modellgewichte herunterladen (kein HF-Token nötig)
  infer      omnivoice-infer CLI (Einzel-Synthese in eine Datei)
  shell      Bash im Container
Alles andere wird direkt als Befehl ausgeführt, z. B.:
  docker compose run --rm omnivoice python -c "import torch; print(torch.__version__)"
USAGE
    ;;
  *)
    exec "${command}" "$@"
    ;;
esac
