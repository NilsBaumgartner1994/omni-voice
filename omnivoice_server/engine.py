"""TTS engines behind the web server.

``OmniVoiceEngine`` wraps the upstream :class:`omnivoice.OmniVoice` model.
``DummyEngine`` produces a short synthetic tone instead and needs neither
torch nor model weights -- it exists so the container plumbing (ports,
volumes, reverse proxy, UI) can be smoke tested in seconds.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import Settings

logger = logging.getLogger(__name__)

# Fallback language list for the dummy engine (the real one exposes 600+).
_FALLBACK_LANGUAGES = [
    "German",
    "English",
    "French",
    "Spanish",
    "Italian",
    "Chinese",
    "Japanese",
]


class SynthesisError(RuntimeError):
    """Raised for user-fixable problems (bad input, missing ASR model)."""


@dataclass
class SynthesisRequest:
    text: str
    mode: str = "auto"  # auto | clone | design
    language: str | None = None
    instruct: str | None = None
    ref_audio_path: str | None = None
    ref_text: str | None = None
    num_step: int = 32
    guidance_scale: float = 2.0
    speed: float = 1.0
    duration: float | None = None
    denoise: bool = True
    normalize_text: bool = False


@dataclass
class EngineStatus:
    state: str = "idle"  # idle | loading | ready | error
    detail: str = ""
    device: str | None = None
    dtype: str | None = None
    model: str | None = None
    asr: bool = False
    load_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "device": self.device,
            "dtype": self.dtype,
            "model": self.model,
            "asr": self.asr,
            "load_seconds": self.load_seconds,
        }


class BaseEngine:
    """Common load-state handling shared by both engines."""

    name = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.sampling_rate = 24000
        self.status = EngineStatus(model=settings.model)
        self._lock = threading.Lock()  # serialises generation
        self._load_lock = threading.Lock()

    # -- loading ---------------------------------------------------------
    def load(self) -> None:
        """Load the model. Safe to call multiple times."""
        with self._load_lock:
            if self.status.state == "ready":
                return
            self.status.state = "loading"
            self.status.detail = "Modell wird geladen ..."
            started = time.time()
            try:
                self._load()
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                self.status.state = "error"
                self.status.detail = f"{type(exc).__name__}: {exc}"
                logger.exception("Loading the model failed")
                return
            self.status.load_seconds = round(time.time() - started, 1)
            self.status.state = "ready"
            self.status.detail = "Modell geladen."

    def load_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.load, name="model-loader", daemon=True)
        thread.start()
        return thread

    def _load(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # -- generation ------------------------------------------------------
    def synthesize(self, request: SynthesisRequest) -> Sequence[float]:
        if self.status.state != "ready":
            raise SynthesisError(
                "Das Modell ist noch nicht geladen (Status: "
                f"{self.status.state}). Bitte warten und erneut versuchen."
            )
        self._validate(request)
        with self._lock:
            return self._synthesize(request)

    def _validate(self, request: SynthesisRequest) -> None:
        """Checks that do not depend on the concrete engine."""
        if not request.text.strip():
            raise SynthesisError("Bitte einen Text angeben.")
        if request.mode == "clone":
            if not request.ref_audio_path:
                raise SynthesisError(
                    "Für das Klonen einer Stimme wird eine Referenz-Audiodatei "
                    "benötigt."
                )
            if not request.ref_text and not self.settings.load_asr:
                raise SynthesisError(
                    "Ohne Referenztext wird ein Whisper-ASR-Modell benötigt, das "
                    "aktuell deaktiviert ist. Bitte den Referenztext eintragen "
                    "oder den Container mit OMNIVOICE_LOAD_ASR=true starten."
                )
        if request.mode == "design" and not request.instruct:
            raise SynthesisError(
                "Für 'Stimme entwerfen' wird mindestens eine Eigenschaft "
                "(instruct) benötigt."
            )

    def _synthesize(
        self, request: SynthesisRequest
    ) -> Sequence[float]:  # pragma: no cover
        raise NotImplementedError

    def languages(self) -> list[str]:
        return list(_FALLBACK_LANGUAGES)


class DummyEngine(BaseEngine):
    """Generates a short tone. No torch, no weights, no downloads."""

    name = "dummy"

    def _load(self) -> None:
        self.status.device = "cpu"
        self.status.dtype = "float32"
        self.status.model = "dummy"

    def _synthesize(self, request: SynthesisRequest) -> Sequence[float]:
        # Roughly mimic a speaking rate so the UI shows a plausible clip.
        seconds = request.duration or max(
            0.6, min(20.0, len(request.text) / 15.0 / max(request.speed, 0.1))
        )
        total = int(seconds * self.sampling_rate)
        base = 220.0 if request.mode != "design" else 330.0
        samples = []
        for i in range(total):
            t = i / self.sampling_rate
            envelope = min(1.0, t * 8.0, max(0.0, (seconds - t) * 8.0))
            value = math.sin(2 * math.pi * base * t) * 0.25 * envelope
            value += math.sin(2 * math.pi * base * 2 * t) * 0.08 * envelope
            samples.append(value)
        return samples


class OmniVoiceEngine(BaseEngine):
    """The real thing: upstream OmniVoice loaded from the HuggingFace hub."""

    name = "omnivoice"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.model = None
        self._languages: list[str] = []

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _resolve_device(configured: str | None) -> str:
        if configured:
            return configured
        from omnivoice.utils.common import get_best_device

        return get_best_device()

    @staticmethod
    def _resolve_dtype(configured: str | None, device: str):
        import torch

        aliases = {
            "float16": torch.float16,
            "fp16": torch.float16,
            "half": torch.float16,
            "bfloat16": torch.bfloat16,
            "bf16": torch.bfloat16,
            "float32": torch.float32,
            "fp32": torch.float32,
            "float": torch.float32,
        }
        if configured and configured.lower() != "auto":
            key = configured.lower()
            if key not in aliases:
                raise ValueError(
                    f"Unbekannter dtype {configured!r}. Erlaubt: "
                    + ", ".join(sorted(aliases))
                )
            return aliases[key]
        # float16 on CPU is either unimplemented or extremely slow for many
        # ops, so the CPU default (the common case on a laptop) is float32.
        if device.startswith(("cuda", "xpu", "mps")):
            return torch.float16
        return torch.float32

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        import torch  # noqa: F401  (imported for its side effects/checks)
        from omnivoice import OmniVoice

        device = self._resolve_device(self.settings.device)
        dtype = self._resolve_dtype(self.settings.dtype, device)

        self.status.device = device
        self.status.dtype = str(dtype).replace("torch.", "")
        self.status.model = self.settings.model
        self.status.asr = self.settings.load_asr
        self.status.detail = (
            f"Lade {self.settings.model} auf {device} ({self.status.dtype}). "
            "Beim ersten Start werden die Gewichte heruntergeladen."
        )
        logger.info(self.status.detail)

        self.model = OmniVoice.from_pretrained(
            self.settings.model,
            device_map=device,
            dtype=dtype,
            load_asr=self.settings.load_asr,
            asr_model_name=self.settings.asr_model,
        )
        self.sampling_rate = int(self.model.sampling_rate or 24000)

        try:
            from omnivoice.utils.lang_map import LANG_NAMES, lang_display_name

            self._languages = sorted(lang_display_name(n) for n in LANG_NAMES)
        except Exception:  # noqa: BLE001 - the language list is cosmetic
            logger.warning("Could not read the language list", exc_info=True)
            self._languages = list(_FALLBACK_LANGUAGES)

    def languages(self) -> list[str]:
        return self._languages or list(_FALLBACK_LANGUAGES)

    # -- generation ------------------------------------------------------
    def _synthesize(self, request: SynthesisRequest) -> Sequence[float]:
        from omnivoice import OmniVoiceGenerationConfig

        gen_config = OmniVoiceGenerationConfig(
            num_step=int(request.num_step),
            guidance_scale=float(request.guidance_scale),
            denoise=bool(request.denoise),
        )

        kwargs: dict[str, Any] = {
            "text": request.text,
            "language": request.language,
            "generation_config": gen_config,
            "normalize_text": bool(request.normalize_text),
        }

        if request.duration and request.duration > 0:
            kwargs["duration"] = float(request.duration)
        elif request.speed and float(request.speed) != 1.0:
            kwargs["speed"] = float(request.speed)

        if request.mode == "clone":
            kwargs["voice_clone_prompt"] = self.model.create_voice_clone_prompt(
                ref_audio=request.ref_audio_path,
                ref_text=request.ref_text or None,
            )

        if request.instruct:
            kwargs["instruct"] = request.instruct

        audio = self.model.generate(**kwargs)
        # np.ndarray of shape (T,) -- encode_wav has a numpy fast path.
        return audio[0]


def build_engine(settings: Settings) -> BaseEngine:
    if settings.engine == "dummy":
        logger.warning(
            "OMNIVOICE_ENGINE=dummy: es wird nur ein Testton erzeugt, kein echtes TTS."
        )
        return DummyEngine(settings)
    return OmniVoiceEngine(settings)
