"""Minimal WAV encoding (stdlib only, no soundfile dependency)."""

from __future__ import annotations

import array
import io
import sys
import wave
from collections.abc import Sequence


def encode_wav(samples: Sequence[float], sampling_rate: int) -> bytes:
    """Encode float samples in [-1, 1] as a 16-bit mono WAV file.

    Accepts plain sequences as well as numpy arrays (fast path).
    """
    pcm_bytes = to_pcm16(samples)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sampling_rate))
        handle.writeframes(pcm_bytes)
    return buffer.getvalue()


def to_pcm16(samples: Sequence[float]) -> bytes:
    """Float-Samples in [-1, 1] als 16-Bit-PCM (little endian)."""
    numpy = sys.modules.get("numpy")
    if numpy is not None and isinstance(samples, numpy.ndarray):
        clipped = numpy.clip(samples, -1.0, 1.0) * 32767.0
        return clipped.astype("<i2").tobytes()

    pcm = array.array("h")
    for value in samples:
        if value > 1.0:
            value = 1.0
        elif value < -1.0:
            value = -1.0
        pcm.append(int(value * 32767.0))
    if sys.byteorder == "big":  # WAV is little-endian
        pcm.byteswap()
    return pcm.tobytes()
