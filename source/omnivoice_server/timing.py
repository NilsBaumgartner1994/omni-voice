"""Duration forecast for TTS jobs, learned from previous runs.

The web UI wants to answer "how long will this take?" before the generation
starts. There is no progress signal inside the model, so the estimate comes
from measured runs on *this* machine: every finished generation is stored as
a ``(work, seconds)`` pair and the next estimate is ``work * rate``, where
``rate`` is the median seconds-per-work-unit of the recent history.

``work`` is what actually scales the runtime of a diffusion TTS model:

    work = num_step * audio_seconds * cfg_factor

* ``num_step`` -- each diffusion step is one pass through the network,
* ``audio_seconds`` -- how much audio is produced (known from a fixed
  duration, otherwise approximated from the text length and the speed),
* ``cfg_factor`` -- classifier-free guidance needs a second forward pass per
  step, so ``guidance_scale > 0`` roughly doubles the work.

With that normalisation a single previous run is enough for a usable
estimate of a *different* input: 60 s for a 10-second clip at 32 steps
predicts ~30 s for a 5-second clip at the same settings.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Rough speaking rate used to turn a text length into an audio length. Only
# the ratio between two requests matters here, and a systematic offset is
# absorbed by the learned rate.
CHARS_PER_SECOND = 15.0

# Samples that go into one estimate. Short enough to follow changes on the
# machine (thermal throttling, other load), long enough for a stable median.
ESTIMATE_WINDOW = 12

# Below this, per-mode history is too thin and all modes are pooled.
MIN_MODE_SAMPLES = 2

HISTORY_VERSION = 1


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class RequestFeatures:
    """Everything about a request that influences its runtime."""

    text_chars: int = 0
    num_step: int = 32
    guidance_scale: float = 2.0
    speed: float = 1.0
    duration: float | None = None
    mode: str = "auto"

    @classmethod
    def from_request(cls, request: Any) -> RequestFeatures:
        """Build features from a :class:`~omnivoice_server.engine.SynthesisRequest`."""
        return cls(
            text_chars=len(getattr(request, "text", "") or ""),
            num_step=int(getattr(request, "num_step", 32) or 32),
            guidance_scale=float(getattr(request, "guidance_scale", 0.0) or 0.0),
            speed=float(getattr(request, "speed", 1.0) or 1.0),
            duration=getattr(request, "duration", None),
            mode=str(getattr(request, "mode", "auto") or "auto"),
        )

    @property
    def audio_seconds(self) -> float:
        """Length of the audio this request is expected to produce."""
        if self.duration and self.duration > 0:
            return float(self.duration)
        speed = _clamp(float(self.speed or 1.0), 0.1, 10.0)
        return _clamp(self.text_chars / CHARS_PER_SECOND / speed, 0.3, 3600.0)

    @property
    def work(self) -> float:
        """Relative amount of compute this request needs."""
        num_step = _clamp(float(self.num_step or 1), 1.0, 1000.0)
        cfg_factor = 2.0 if self.guidance_scale and self.guidance_scale > 0 else 1.0
        return max(num_step * self.audio_seconds * cfg_factor, 1e-3)


@dataclass(frozen=True)
class Sample:
    """One finished generation."""

    env: str
    mode: str
    work: float
    seconds: float
    at: float

    @property
    def rate(self) -> float:
        return self.seconds / self.work


@dataclass(frozen=True)
class Estimate:
    seconds: float
    low_seconds: float
    high_seconds: float
    samples: int
    based_on: str  # "mode" | "all"
    audio_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "estimate_seconds": round(self.seconds, 2),
            "low_seconds": round(self.low_seconds, 2),
            "high_seconds": round(self.high_seconds, 2),
            "samples": self.samples,
            "based_on": self.based_on,
            "audio_seconds": round(self.audio_seconds, 1),
        }


def _quantile(values: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted, non-empty list."""
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


class DurationForecaster:
    """Records generation times and predicts the next one.

    Thread safe: generations run in a worker thread while the estimate is
    served from the event loop.
    """

    def __init__(self, path: str | None = None, max_samples: int = 200):
        self.path = path
        self.max_samples = max(1, int(max_samples))
        self._lock = threading.Lock()
        self._samples: list[Sample] = []
        self._persist = bool(path)
        if path:
            self._load()

    # -- history ---------------------------------------------------------
    @property
    def count(self) -> int:
        with self._lock:
            return len(self._samples)

    def record(self, features: RequestFeatures, seconds: float, env: str = "") -> None:
        """Store a finished generation. Never raises."""
        try:
            if not seconds or seconds <= 0:
                return
            sample = Sample(
                env=env,
                mode=features.mode,
                work=features.work,
                seconds=float(seconds),
                at=time.time(),
            )
            with self._lock:
                self._samples.append(sample)
                del self._samples[: max(0, len(self._samples) - self.max_samples)]
                snapshot = list(self._samples)
            self._save(snapshot)
        except Exception:  # noqa: BLE001 - a forecast must never break a job
            logger.warning("Laufzeit konnte nicht gespeichert werden", exc_info=True)

    def history(self, env: str = "") -> dict[str, Any]:
        """Summary of the recorded runs, for the UI footer and the API."""
        with self._lock:
            matching = [s for s in self._samples if s.env == env]
            total = len(self._samples)
        last = matching[-1] if matching else None
        return {
            "count": len(matching),
            "total": total,
            "persistent": self._persist,
            "last_seconds": round(last.seconds, 1) if last else None,
        }

    # -- prediction ------------------------------------------------------
    def estimate(self, features: RequestFeatures, env: str = "") -> Estimate | None:
        """Predicted wall-clock seconds, or ``None`` without usable history."""
        with self._lock:
            candidates = [s for s in self._samples if s.env == env]

        based_on = "mode"
        samples = [s for s in candidates if s.mode == features.mode]
        if len(samples) < MIN_MODE_SAMPLES:
            # A fresh mode borrows the rate of the other modes rather than
            # showing nothing; the per-mode overhead is small next to the
            # diffusion steps.
            based_on = "all"
            samples = candidates
        if not samples:
            return None

        rates = sorted(s.rate for s in samples[-ESTIMATE_WINDOW:])
        work = features.work
        median = _quantile(rates, 0.5)
        if len(rates) >= 3:
            low, high = _quantile(rates, 0.25), _quantile(rates, 0.75)
        else:
            # Too few runs for a spread: fall back to a generic +-25 %.
            low, high = median * 0.75, median * 1.25
        return Estimate(
            seconds=median * work,
            low_seconds=low * work,
            high_seconds=high * work,
            samples=len(samples),
            based_on=based_on,
            audio_seconds=features.audio_seconds,
        )

    # -- persistence -----------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 - a broken file starts a new history
            logger.warning(
                "Laufzeit-Historie %s ist unlesbar", self.path, exc_info=True
            )
            return
        if not isinstance(payload, dict) or payload.get("version") != HISTORY_VERSION:
            return
        samples = []
        for raw in payload.get("samples") or []:
            try:
                samples.append(
                    Sample(
                        env=str(raw["env"]),
                        mode=str(raw["mode"]),
                        work=float(raw["work"]),
                        seconds=float(raw["seconds"]),
                        at=float(raw["at"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._samples = samples[-self.max_samples :]
        logger.info(
            "%d frühere Laufzeiten aus %s geladen", len(self._samples), self.path
        )

    def _save(self, samples: list[Sample]) -> None:
        if not self._persist or not self.path:
            return
        payload = {
            "version": HISTORY_VERSION,
            "samples": [asdict(s) for s in samples],
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            # Atomic replace so a crash mid-write cannot truncate the file.
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                os.replace(tmp_path, self.path)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except OSError:
            # Read-only volume or missing directory: keep forecasting from
            # memory instead of logging on every single generation.
            self._persist = False
            logger.warning(
                "Laufzeit-Historie %s ist nicht schreibbar – Prognose gilt nur "
                "bis zum Neustart.",
                self.path,
                exc_info=True,
            )
