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
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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

# Medientypen der Referenzaufnahmen (die Bibliothek kennt zusätzlich Bilder).
AUDIO_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
}


# Was `/api/tts` ohne `format` liefert: WAV, wie bisher.
API_DEFAULT_FORMAT = "wav"


def ffmpeg_binary() -> str | None:
    """Pfad zu ffmpeg -- oder ``None``, wenn es hier keins gibt."""
    override = os.environ.get("OMNIVOICE_FFMPEG", "").strip()
    return shutil.which(override or "ffmpeg")


def ffprobe_binary() -> str | None:
    """Pfad zu ffprobe (liegt neben ffmpeg) -- oder ``None``."""
    override = os.environ.get("OMNIVOICE_FFPROBE", "").strip()
    if override:
        return shutil.which(override)
    ffmpeg = ffmpeg_binary()
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.access(sibling, os.X_OK):
            return sibling
    return shutil.which("ffprobe")


def probe_duration(path: str) -> float | None:
    """Länge einer Audiodatei in Sekunden -- ``None``, wenn nicht feststellbar.

    WAV liest die Standardbibliothek, alles andere fragt ffprobe. Ohne
    ffprobe (Entwicklung ohne Docker) bleibt die Länge von mp3/m4a/ogg
    unbekannt; der Aufrufer prüft dann eben nicht.
    """
    try:
        with wave.open(path) as handle:
            rate = handle.getframerate()
            if rate > 0:
                return handle.getnframes() / rate
    except (wave.Error, EOFError, ValueError, OSError):
        pass
    binary = ffprobe_binary()
    if binary is None:
        return None
    command = [
        binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        seconds = float(result.stdout.decode("ascii", "replace").strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def reference_too_long(seconds: float, limit: int) -> str:
    """Meldung, wenn eine Referenzaufnahme länger ist als erlaubt.

    Lange Referenzen bringen dem Klonen nichts, brauchen aber ein Vielfaches
    an Arbeitsspeicher -- auf einer CPU reicht eine halbe Minute, um den
    Container zu sprengen (Exit 137). Deshalb wird vor dem Modell abgebrochen.
    """
    return (
        f"Das Referenz-Audio ist {seconds:.1f} Sekunden lang, erlaubt sind "
        f"höchstens {limit} Sekunden. Bitte einen kürzeren Ausschnitt "
        "wählen (3–10 Sekunden reichen zum Klonen)."
    )


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


def clip_audio(
    path: str,
    *,
    start: float,
    seconds: float,
    audio_format: str = "wav",
    bitrate: str = "192k",
) -> bytes:
    """Einen Ausschnitt einer vorhandenen Audiodatei herausschneiden.

    Gebraucht wird das für Referenzaufnahmen aus einem längeren Mitschnitt
    (Tonspur eines YouTube-Videos, zu lange hochgeladene Datei): ffmpeg
    dekodiert nur den gewünschten Bereich und schreibt ihn einkanalig neu --
    als WAV (verlustfrei, Standard) oder MP3 (klein, für YouTube-Tonspuren).
    """
    if seconds <= 0:
        raise AudioEncodeError("Der Ausschnitt hat keine Länge.")
    key = normalize_format(audio_format)
    if key == "mp3" and not _BITRATE_RE.match(bitrate or ""):
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
    ]
    # MP3 kann durch die Pipe; WAV nicht: den RIFF-Header mit den Längen
    # schreibt ffmpeg erst zum Schluss und dafür muss es zurückspringen.
    target = "pipe:1"
    tmp_out = None
    if key == "mp3":
        command += ["-f", "mp3", "-b:a", bitrate]
    else:
        fd, tmp_out = tempfile.mkstemp(suffix=".wav", prefix="omnivoice-cut-")
        os.close(fd)
        target = tmp_out
        command += ["-f", "wav", "-c:a", "pcm_s16le", "-y"]
    command.append(target)
    try:
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioEncodeError("ffmpeg hat zu lange gebraucht.") from exc
        except OSError as exc:
            raise AudioEncodeError(f"ffmpeg ließ sich nicht starten: {exc}") from exc
        payload = result.stdout
        if result.returncode == 0 and tmp_out is not None:
            with open(tmp_out, "rb") as handle:
                payload = handle.read()
        if result.returncode != 0 or not payload:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            reason = detail[-1] if detail else f"Rückgabewert {result.returncode}"
            raise AudioEncodeError(
                f"ffmpeg konnte den Ausschnitt nicht schneiden: {reason}"
            )
    finally:
        if tmp_out is not None:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass
    return payload


def clip_to_mp3(
    path: str, *, start: float, seconds: float, bitrate: str = "192k"
) -> bytes:
    """Ausschnitt als MP3 (siehe :func:`clip_audio`)."""
    return clip_audio(
        path, start=start, seconds=seconds, audio_format="mp3", bitrate=bitrate
    )


@dataclass(frozen=True)
class ReferenceCut:
    """Ergebnis von :func:`fit_reference`: die Aufnahme, wie sie ins Modell geht."""

    data: bytes
    suffix: str
    media_type: str
    # Länge der Aufnahme vorher/nachher (None: nicht feststellbar).
    original_seconds: float | None
    seconds: float | None
    # Der herausgeschnittene Bereich der Originaldatei.
    start: float
    end: float | None
    # Wurde überhaupt geschnitten -- und falls ja, nur wegen der Obergrenze
    # (statt auf Wunsch)?
    cut: bool
    auto_trimmed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_seconds": (
                round(self.original_seconds, 2)
                if self.original_seconds is not None
                else None
            ),
            "seconds": round(self.seconds, 2) if self.seconds is not None else None,
            "start": round(self.start, 2),
            "end": round(self.end, 2) if self.end is not None else None,
            "cut": self.cut,
            "auto_trimmed": self.auto_trimmed,
        }


def fit_reference(
    data: bytes,
    suffix: str,
    *,
    start: float = 0.0,
    end: float | None = None,
    max_seconds: int = 0,
) -> ReferenceCut:
    """Eine hochgeladene Referenzaufnahme zurechtschneiden.

    Gewünscht ist der Bereich ``start``..``end`` (Sekunden; ohne ``end`` bis
    zum Schluss). Ist er -- oder ohne Wunsch die ganze Datei -- länger als
    ``max_seconds``, wird auf die Obergrenze gekürzt statt abgewiesen: lange
    Referenzen bringen dem Klonen nichts, sprengen aber auf einer CPU den
    Speicher. Geschnitten wird als WAV, sonst bleibt die Datei unangetastet.
    """
    start = max(0.0, float(start or 0.0))
    end_value = float(end) if end is not None else None
    if end_value is not None and end_value <= start:
        raise AudioEncodeError("Das Ende des Ausschnitts muss hinter dem Start liegen.")

    fd, tmp = tempfile.mkstemp(suffix=suffix or ".bin", prefix="omnivoice-ref-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        total = probe_duration(tmp)

        if total is not None and end_value is not None:
            end_value = min(end_value, total)
        wanted = start > 0 or (
            end_value is not None and (total is None or end_value < total - 0.05)
        )
        length = end_value if end_value is not None else total
        if length is not None:
            length -= start
        auto = False
        if max_seconds > 0 and length is not None and length > max_seconds:
            length = float(max_seconds)
            end_value = start + length
            auto = not wanted
        elif max_seconds > 0 and length is None and not wanted:
            # Länge unbekannt (kein ffprobe): lieber unangetastet lassen als
            # blind schneiden -- die Bibliothek prüft dann eben nicht.
            length = None

        if not wanted and not auto:
            return ReferenceCut(
                data=data,
                suffix=suffix,
                media_type=AUDIO_MEDIA_TYPES.get(suffix, "application/octet-stream"),
                original_seconds=total,
                seconds=total,
                start=0.0,
                end=total,
                cut=False,
                auto_trimmed=False,
            )
        if length is None or length <= 0:
            raise AudioEncodeError("Der Ausschnitt liegt außerhalb der Aufnahme.")
        payload = clip_audio(tmp, start=start, seconds=length, audio_format="wav")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return ReferenceCut(
        data=payload,
        suffix=".wav",
        media_type="audio/wav",
        original_seconds=total,
        seconds=length,
        start=start,
        end=start + length,
        cut=True,
        auto_trimmed=auto,
    )


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
