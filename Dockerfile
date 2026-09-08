# syntax=docker/dockerfile:1

# OmniVoice als selbst gehosteter Container.
#
# Standard: CPU-Build (läuft auf jedem Laptop, auch ohne GPU).
# Für NVIDIA-GPUs beim Bauen einen anderen PyTorch-Index setzen, z. B.:
#   --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
# (macht `docker compose --profile gpu` automatisch)

ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.8.0
ARG TORCHAUDIO_VERSION=2.8.0
# Version des Upstream-Pakets (https://pypi.org/project/omnivoice/).
ARG OMNIVOICE_VERSION=0.2.1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Modell-Cache liegt im Volume, damit Gewichte nur einmal geladen werden.
    HF_HOME=/models \
    TORCH_HOME=/models/torch \
    OMNIVOICE_HOST=0.0.0.0 \
    OMNIVOICE_PORT_INTERNAL=7860

# ffmpeg + libsndfile: Dekodieren von mp3/m4a/ogg-Referenzaudio.
# curl: HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# PyTorch zuerst (eigene Layer, größter Download) und mit explizitem Index,
# damit reproduzierbar die CPU- bzw. CUDA-Räder gezogen werden.
RUN pip install --no-cache-dir \
        "torch==${TORCH_VERSION}" \
        "torchaudio==${TORCHAUDIO_VERSION}" \
        --index-url "${TORCH_INDEX_URL}" \
        --extra-index-url https://pypi.org/simple

RUN pip install --no-cache-dir \
        "omnivoice==${OMNIVOICE_VERSION}" \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "python-multipart>=0.0.9"

RUN useradd --create-home --uid 1000 omnivoice \
    && mkdir -p /models /output \
    && chown -R omnivoice:omnivoice /models /output

WORKDIR /app
COPY --chown=omnivoice:omnivoice omnivoice_server/ /app/omnivoice_server/
COPY --chown=omnivoice:omnivoice scripts/ /app/scripts/
COPY --chown=omnivoice:omnivoice docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER omnivoice
VOLUME ["/models"]
EXPOSE 7860

# Beim ersten Start werden mehrere GB Modellgewichte geladen; solange läuft
# noch die Startphase und ein fehlschlagender Healthcheck ist erwartbar.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30m --retries=5 \
    CMD curl -fsS http://127.0.0.1:${OMNIVOICE_PORT_INTERNAL}/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
