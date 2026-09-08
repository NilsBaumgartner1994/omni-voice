#!/usr/bin/env bash
set -euo pipefail

# Docker Compose setzt `VAR: ${VAR:-}` auch dann im Container, wenn die
# Variable auf dem Host gar nicht definiert ist -- dann eben als leerer String.
# huggingface_hub liest `HF_ENDPOINT` aber mit `os.getenv("HF_ENDPOINT",
# "https://huggingface.co")`: ein gesetzter leerer Wert gewinnt gegen den
# Standard, alle Hub-URLs verlieren ihr Schema und der Modell-Download stirbt mit
#   UnsupportedProtocol: Request URL is missing an 'http://' or 'https://' protocol.
# Leere Optionen daher entfernen, statt sie als "" weiterzureichen.
for _var in HF_ENDPOINT HF_HUB_OFFLINE HF_TOKEN HUGGING_FACE_HUB_TOKEN \
            OMP_NUM_THREADS OMNIVOICE_DEVICE OMNIVOICE_DTYPE; do
  if [[ -z "${!_var:-}" ]]; then
    unset "${_var}"
  fi
done
unset _var

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
