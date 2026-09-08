"""Runtime configuration for the OmniVoice container.

Everything is driven by environment variables so the container can be
configured entirely from ``docker-compose.yml`` / ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str | None) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def default_library_dir() -> str:
    """Wo die Stimm-Bibliothek liegt.

    Bewusst *nicht* im Modell-Volume: Personen, Bilder und Referenzaufnahmen
    sollen einen Modellwechsel unbeschadet überstehen. Im Container ist das
    ``/data`` (eigenes Volume), außerhalb ein ``data/`` neben dem Projekt.
    """
    base = _env_str("OMNIVOICE_DATA_DIR", None)
    if not base:
        base = "/data" if os.path.isdir("/data") else os.path.join(os.getcwd(), "data")
    return os.path.join(base, "voices")


def _default_timing_history_path() -> str:
    """Inside the container this lands in the ``/models`` volume, so the
    forecast survives ``docker compose up -d`` and image rebuilds."""
    base = _env_str("HF_HOME", None) or "/models"
    return os.path.join(base, "generation-timings.json")


@dataclass
class Settings:
    """Configuration of the web server and the TTS engine."""

    host: str = "0.0.0.0"
    port: int = 7860

    # "omnivoice" runs the real model, "dummy" serves a synthetic beep and is
    # used by the test suite and by `make smoke` to verify the plumbing
    # (ports, volumes, reverse proxies) without downloading model weights.
    engine: str = "omnivoice"

    model: str = "k2-fsa/OmniVoice"
    device: str | None = None  # None -> auto detect
    dtype: str | None = None  # None -> fp16 on GPU, fp32 on CPU

    # Whisper is only needed to auto-transcribe the reference audio of a
    # voice clone. It is a large extra download, so it is off by default.
    load_asr: bool = False
    asr_model: str = "openai/whisper-large-v3-turbo"

    # Upper bounds so a single browser tab cannot lock up a laptop.
    max_text_chars: int = 2000
    max_ref_audio_bytes: int = 25 * 1024 * 1024
    max_image_bytes: int = 5 * 1024 * 1024

    # Verzeichnis der Stimm-Bibliothek (leer -> default_library_dir()).
    library_dir: str = ""
    # Wie viele berechnete Stimmen gleichzeitig im Arbeitsspeicher bleiben.
    voice_cache_size: int = 8

    # Number of concurrent generations. The model is not thread safe, and a
    # laptop has no spare compute anyway, so keep this at 1.
    max_concurrency: int = 1

    # Measured generation times, used to forecast how long the next job will
    # take. None keeps the history in memory only (default outside Docker).
    timing_history_path: str | None = None
    timing_history_size: int = 200

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            host=_env_str("OMNIVOICE_HOST", "0.0.0.0"),
            port=_env_int("OMNIVOICE_PORT_INTERNAL", 7860),
            engine=_env_str("OMNIVOICE_ENGINE", "omnivoice").lower(),
            model=_env_str("OMNIVOICE_MODEL", "k2-fsa/OmniVoice"),
            device=_env_str("OMNIVOICE_DEVICE", None),
            dtype=_env_str("OMNIVOICE_DTYPE", None),
            load_asr=_env_bool("OMNIVOICE_LOAD_ASR", False),
            asr_model=_env_str("OMNIVOICE_ASR_MODEL", "openai/whisper-large-v3-turbo"),
            max_text_chars=_env_int("OMNIVOICE_MAX_TEXT_CHARS", 2000),
            max_ref_audio_bytes=_env_int(
                "OMNIVOICE_MAX_REF_AUDIO_BYTES", 25 * 1024 * 1024
            ),
            max_image_bytes=_env_int("OMNIVOICE_MAX_IMAGE_BYTES", 5 * 1024 * 1024),
            library_dir=_env_str("OMNIVOICE_LIBRARY_DIR", default_library_dir()),
            voice_cache_size=_env_int("OMNIVOICE_VOICE_CACHE_SIZE", 8),
            timing_history_path=_env_str(
                "OMNIVOICE_TIMING_HISTORY", _default_timing_history_path()
            ),
            timing_history_size=_env_int("OMNIVOICE_TIMING_HISTORY_SIZE", 200),
        )
