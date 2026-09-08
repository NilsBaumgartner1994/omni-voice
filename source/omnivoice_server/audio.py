"""Auslieferungsformate der erzeugten Sprache: WAV (stdlib) und MP3 (ffmpeg).

Erzeugt wird immer WAV -- verlustfrei, ohne Fremdprogramm und in jedem
Browser abspielbar. MP3 ist rund zehnmal kleiner und deshalb der Standard
zum Herunterladen; kodiert wird es von ffmpeg, das im Image ohnehin für die
Referenzaufnahmen mitkommt. Fehlt ffmpeg (Entwicklung ohne Docker), bleibt
nur WAV übrig -- ``available_formats()`` sagt das der Oberfläche.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import wave
from collections.abc import Sequence
from dataclasses import dataclass

from .wav import encode_wav, to_pcm16

# Kodieren ist Sekundenarbeit; hängt ffmpeg trotzdem, soll die Anfrage nicht
# ewig blockieren.
FFMPEG_TIMEOUT_SECONDS = 120

_BITRATE_RE = re.compile(r"^\d{1,4}k?$", re.IGNORECASE)


class AudioEncodeError(RuntimeError):
    """Das Audio konnte nicht in das gewünschte Format gebracht werden."""


@dataclass(frozen=True)
class AudioFormat:
    key: str
    label: str
    media_type: str
    suffix: str

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "label": self.label,
            "media_type": self.media_type,
            "suffix": self.suffix,
        }


FORMATS: dict[str, AudioFormat] = {
    "mp3": AudioFormat("mp3", "MP3", "audio/mpeg", ".mp3"),
    "wav": AudioFormat("wav", "WAV", "audio/wav", ".wav"),
}

# Was `/api/tts` ohne `format` liefert: WAV, wie bisher.
API_DEFAULT_FORMAT = "wav"


def ffmpeg_binary() -> str | None:
    """Pfad zu ffmpeg -- oder ``None``, wenn es hier keins gibt."""
    override = os.environ.get("OMNIVOICE_FFMPEG", "").strip()
    return shutil.which(override or "ffmpeg")


def mp3_supported() -> bool:
    return ffmpeg_binary() is not None


def available_formats() -> list[AudioFormat]:
    """Formate, die dieser Server ausliefern kann (MP3 nur mit ffmpeg)."""
    return [fmt for key, fmt in FORMATS.items() if key != "mp3" or mp3_supported()]


def default_download_format() -> str:
    """Vorauswahl im Download-Menü der Oberfläche."""
    return "mp3" if mp3_supported() else "wav"


def normalize_format(value: str | None, default: str = API_DEFAULT_FORMAT) -> str:
    """Formatnamen prüfen (``None`` und Leerstring ergeben den Standard)."""
    key = (value or "").strip().lower().lstrip(".") or default
    if key in ("wave", "x-wav"):
        key = "wav"
    if key not in FORMATS:
        raise AudioEncodeError(
            f"Unbekanntes Audioformat: {value!r} "
            f"(erlaubt: {', '.join(sorted(FORMATS))})"
        )
    return key


def encode(
    samples: Sequence[float],
    sampling_rate: int,
    audio_format: str = API_DEFAULT_FORMAT,
    *,
    bitrate: str = "192k",
) -> bytes:
    """Rohsamples in das gewünschte Format kodieren."""
    key = normalize_format(audio_format)
    if key == "wav":
        return encode_wav(samples, sampling_rate)
    return _encode_mp3(to_pcm16(samples), sampling_rate, bitrate=bitrate)


def wav_to_mp3(payload: bytes, *, bitrate: str = "192k") -> bytes:
    """Fertige WAV-Bytes in MP3 wandeln, ohne den Umweg über Fließkomma.

    Gelesen wird mit der Standardbibliothek: so landet nur reines PCM bei
    ffmpeg und kein fremder Container, den es erst parsen müsste.
    """
    try:
        with wave.open(io.BytesIO(payload)) as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError, ValueError) as exc:
        raise AudioEncodeError(f"Keine lesbare WAV-Datei: {exc}") from exc
    if width != 2:
        raise AudioEncodeError(
            f"Nur 16-Bit-WAV wird unterstützt (hier: {width * 8} Bit)."
        )
    if not frames:
        raise AudioEncodeError("Die WAV-Datei enthält keine Audiodaten.")
    return _encode_mp3(frames, rate, bitrate=bitrate, channels=channels)


def clip_to_mp3(
    path: str, *, start: float, seconds: float, bitrate: str = "192k"
) -> bytes:
    """Einen Ausschnitt einer vorhandenen Audiodatei als MP3 herausschneiden.

    Gebraucht wird das für Referenzaufnahmen aus einem längeren Mitschnitt
    (etwa der Tonspur eines YouTube-Videos): ffmpeg dekodiert nur den
    gewünschten Bereich und kodiert ihn einkanalig neu.
    """
    if seconds <= 0:
        raise AudioEncodeError("Der Ausschnitt hat keine Länge.")
    if not _BITRATE_RE.match(bitrate or ""):
        raise AudioEncodeError(f"Ungültige MP3-Bitrate: {bitrate!r}")
    binary = ffmpeg_binary()
    if binary is None:
        raise AudioEncodeError(
            "Zum Schneiden wird ffmpeg gebraucht, das hier nicht gefunden wurde."
        )
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        # -ss vor -i: ffmpeg springt, statt alles davor zu dekodieren.
        "-ss",
        f"{max(0.0, float(start)):.3f}",
        "-t",
        f"{float(seconds):.3f}",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-f",
        "mp3",
        "-b:a",
        bitrate,
        "pipe:1",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioEncodeError("ffmpeg hat zu lange gebraucht.") from exc
    except OSError as exc:
        raise AudioEncodeError(f"ffmpeg ließ sich nicht starten: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else f"Rückgabewert {result.returncode}"
        raise AudioEncodeError(
            f"ffmpeg konnte den Ausschnitt nicht schneiden: {reason}"
        )
    return result.stdout


def _encode_mp3(
    pcm: bytes, sampling_rate: int, *, bitrate: str, channels: int = 1
) -> bytes:
    if not _BITRATE_RE.match(bitrate or ""):
        raise AudioEncodeError(f"Ungültige MP3-Bitrate: {bitrate!r}")
    binary = ffmpeg_binary()
    if binary is None:
        raise AudioEncodeError(
            "MP3 braucht ffmpeg, das hier nicht gefunden wurde. "
            "Im Container ist es enthalten; ohne Docker bitte ffmpeg "
            "installieren oder WAV verwenden."
        )
    command = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "s16le",
        "-ar",
        str(int(sampling_rate)),
        "-ac",
        str(int(channels)),
        "-i",
        "pipe:0",
        "-f",
        "mp3",
        "-b:a",
        bitrate,
        "pipe:1",
    ]
    try:
        # Festes Kommando, keine Shell -- nichts davon kommt vom Nutzer.
        result = subprocess.run(
            command,
            input=pcm,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioEncodeError("ffmpeg hat zu lange gebraucht.") from exc
    except OSError as exc:
        raise AudioEncodeError(f"ffmpeg ließ sich nicht starten: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else f"Rückgabewert {result.returncode}"
        raise AudioEncodeError(f"ffmpeg konnte kein MP3 erzeugen: {reason}")
    return result.stdout
