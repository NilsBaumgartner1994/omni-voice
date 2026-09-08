"""YouTube als Quelle für Referenzaufnahmen.

Statt eine Datei hochzuladen, kann eine Person auch über einen YouTube-Link
angelegt werden: dieses Modul lädt mit ``yt-dlp`` die Tonspur des Videos als
MP3 herunter, holt -- wenn es welche gibt -- die Untertitel dazu und schneidet
daraus den gewünschten Ausschnitt.

Alles Geladene liegt in einem Zwischenspeicher neben der Stimm-Bibliothek
(``<data>/youtube-cache/<video-id>/``), damit die Oberfläche das ganze Video
anhören kann, während Start- und Endzeit gewählt werden -- ohne es für jeden
Klick erneut zu laden. Der Zwischenspeicher ist jederzeit wegwerfbar: fehlt er,
wird das Video beim nächsten Zugriff neu geholt.

Ohne ``yt-dlp`` (und ohne ffmpeg für das MP3) bleibt die Funktion einfach aus;
:meth:`YouTubeService.availability` sagt der Oberfläche warum.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

from .audio import clip_to_mp3, ffmpeg_binary

logger = logging.getLogger(__name__)

# Nur YouTube: der Link geht an ein externes Programm, das praktisch jede
# Adresse im Netz abrufen würde.
_ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

# Video-IDs landen in Dateipfaden und URLs, deshalb eng geführt.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,32}$")

_TIMESTAMP_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
)

# Untertitel-Auszeichnungen (<c>, <00:00:01.000>) und Cue-Einstellungen.
_TAG_RE = re.compile(r"<[^>]*>")

# Reihenfolge der Untertitelsprachen, wenn das Video keine eigene nennt.
_PREFERRED_SUB_LANGS = ("de", "en")


class YouTubeError(RuntimeError):
    """Vom Benutzer behebbares Problem (falscher Link, Video zu lang ...)."""


class YouTubeUnavailable(YouTubeError):
    """Auf diesem Server fehlt yt-dlp oder ffmpeg."""


@dataclass
class Segment:
    """Ein Stück Transkript mit Zeitmarken (Sekunden)."""

    start: float
    end: float
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }


@dataclass
class Video:
    """Ein heruntergeladenes Video: Tonspur, Stammdaten, Transkript."""

    id: str
    title: str
    url: str
    duration: float = 0.0
    uploader: str = ""
    thumbnail: str = ""
    transcript: list[Segment] = field(default_factory=list)
    transcript_kind: str = ""  # "manual" | "auto" | ""
    transcript_language: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "duration": round(self.duration, 2),
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "transcript": [segment.as_dict() for segment in self.transcript],
            "transcript_kind": self.transcript_kind,
            "transcript_language": self.transcript_language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Video:
        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            duration=float(data.get("duration") or 0.0),
            uploader=str(data.get("uploader") or ""),
            thumbnail=str(data.get("thumbnail") or ""),
            transcript=[
                Segment(
                    start=float(item.get("start") or 0.0),
                    end=float(item.get("end") or 0.0),
                    text=str(item.get("text") or ""),
                )
                for item in data.get("transcript") or []
            ],
            transcript_kind=str(data.get("transcript_kind") or ""),
            transcript_language=str(data.get("transcript_language") or ""),
        )


# -- Link und Transkript (reine Funktionen, ohne Netz) ----------------------
def video_id_from_url(url: str) -> str:
    """Video-ID aus einem YouTube-Link ziehen (sonst :class:`YouTubeError`)."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise YouTubeError("Bitte einen vollständigen YouTube-Link angeben.")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise YouTubeError(
            f"Es werden nur YouTube-Links unterstützt (dieser zeigt auf {host or '?'})."
        )

    candidate = ""
    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        parts = [part for part in parsed.path.split("/") if part]
        # /embed/<id>, /v/<id>, /shorts/<id>, /live/<id>
        if len(parts) >= 2 and parts[0] in ("embed", "v", "shorts", "live"):
            candidate = parts[1]

    if not _VIDEO_ID_RE.match(candidate or ""):
        raise YouTubeError(
            "In diesem Link steckt keine Video-Kennung "
            "(erwartet wird z. B. https://www.youtube.com/watch?v=…)."
        )
    return candidate


def check_video_id(video_id: str) -> str:
    if not _VIDEO_ID_RE.match((video_id or "").strip()):
        raise YouTubeError(f"Unbekanntes Video: {video_id!r}")
    return video_id.strip()


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={check_video_id(video_id)}"


def parse_vtt(payload: str) -> list[Segment]:
    """WebVTT in Zeitabschnitte zerlegen.

    Automatische Untertitel von YouTube wiederholen die vorherige Zeile in
    jedem Block (der „mitlaufende“ Text am unteren Bildrand). Deshalb wird pro
    Zeile entschieden: nur was sich gegenüber der zuletzt übernommenen Zeile
    geändert hat, wird ein neuer Abschnitt.
    """
    segments: list[Segment] = []
    last_line = ""
    lines = payload.splitlines()
    index = 0
    while index < len(lines):
        match = _TIMESTAMP_RE.search(lines[index])
        index += 1
        if not match:
            continue
        start = _seconds(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _seconds(match.group(5), match.group(6), match.group(7), match.group(8))
        while index < len(lines) and lines[index].strip():
            text = _clean_cue(lines[index])
            index += 1
            if not text or text == last_line:
                continue
            segments.append(Segment(start=start, end=max(end, start), text=text))
            last_line = text
    return segments


def transcript_between(segments: list[Segment], start: float, end: float) -> str:
    """Alle Abschnitte, die in den Bereich hineinragen, als ein Text."""
    if end <= start:
        return ""
    parts = [
        segment.text
        for segment in segments
        if segment.end > start and segment.start < end and segment.text
    ]
    return " ".join(parts).strip()


def _seconds(hours: str | None, minutes: str, secs: str, millis: str | None) -> float:
    total = int(hours or 0) * 3600 + int(minutes) * 60 + int(secs)
    if millis:
        total += int(millis.ljust(3, "0")) / 1000
    return float(total)


def _clean_cue(line: str) -> str:
    text = _TAG_RE.sub("", line)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# -- Dienst ----------------------------------------------------------------
class YouTubeService:
    """Lädt Videos, hält sie zwischen und schneidet Ausschnitte heraus."""

    def __init__(
        self,
        cache_dir: str,
        *,
        max_video_seconds: int = 3600,
        max_clip_seconds: int = 120,
        cache_entries: int = 5,
        timeout_seconds: int = 600,
        binary: str | None = None,
        mp3_bitrate: str = "192k",
        enabled: bool = True,
    ) -> None:
        self.cache_dir = os.path.abspath(os.path.expanduser(cache_dir))
        self.max_video_seconds = max_video_seconds
        self.max_clip_seconds = max_clip_seconds
        self.cache_entries = max(1, cache_entries)
        self.timeout_seconds = timeout_seconds
        self.binary = binary or None
        self.mp3_bitrate = mp3_bitrate
        self.enabled = enabled
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- Verfügbarkeit ---------------------------------------------------
    def command(self) -> list[str] | None:
        """Wie yt-dlp hier aufgerufen wird -- oder ``None``, wenn es fehlt."""
        if self.binary:
            found = shutil.which(self.binary) or (
                self.binary if os.path.isfile(self.binary) else None
            )
            return [found] if found else None
        found = shutil.which("yt-dlp")
        if found:
            return [found]
        try:  # als Python-Paket installiert
            import yt_dlp  # noqa: F401
        except ImportError:
            return None
        return [sys.executable, "-m", "yt_dlp"]

    def availability(self) -> dict[str, Any]:
        """Für ``/api/info``: geht es, und wenn nein -- warum nicht?"""
        if not self.enabled:
            return {"enabled": False, "reason": "In der Konfiguration abgeschaltet."}
        if self.command() is None:
            return {
                "enabled": False,
                "reason": "yt-dlp ist auf diesem Server nicht installiert.",
            }
        if ffmpeg_binary() is None:
            return {
                "enabled": False,
                "reason": "ffmpeg fehlt -- ohne es gibt es kein MP3.",
            }
        return {
            "enabled": True,
            "reason": "",
            "max_video_seconds": self.max_video_seconds,
            "max_clip_seconds": self.max_clip_seconds,
        }

    def _require(self) -> list[str]:
        state = self.availability()
        if not state["enabled"]:
            raise YouTubeUnavailable(
                f"YouTube-Links sind hier nicht verfügbar: {state['reason']}"
            )
        return self.command() or []

    # -- Pfade -----------------------------------------------------------
    def _folder(self, video_id: str) -> str:
        return os.path.join(self.cache_dir, check_video_id(video_id))

    def audio_path(self, video_id: str) -> str | None:
        path = os.path.join(self._folder(video_id), "audio.mp3")
        return path if os.path.isfile(path) else None

    def cached(self, video_id: str) -> Video | None:
        """Was schon geladen wurde -- oder ``None``."""
        folder = self._folder(video_id)
        meta = os.path.join(folder, "video.json")
        if not os.path.isfile(meta) or not self.audio_path(video_id):
            return None
        try:
            with open(meta, encoding="utf-8") as handle:
                return Video.from_dict(json.load(handle))
        except (OSError, ValueError):
            return None

    def _lock_for(self, video_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(video_id, threading.Lock())

    # -- Laden -----------------------------------------------------------
    def fetch(self, url: str, *, refresh: bool = False) -> Video:
        """Tonspur und Transkript eines Videos holen (oder wiederverwenden)."""
        command = self._require()
        video_id = video_id_from_url(url)
        with self._lock_for(video_id):
            if not refresh:
                cached = self.cached(video_id)
                if cached is not None:
                    _touch(self._folder(video_id))
                    return cached

            info = self._probe(command, watch_url(video_id))
            duration = float(info.get("duration") or 0.0)
            if duration <= 0:
                raise YouTubeError(
                    "Das Video hat keine bekannte Länge -- Livestreams lassen "
                    "sich nicht als Referenz verwenden."
                )
            if duration > self.max_video_seconds:
                raise YouTubeError(
                    f"Das Video ist {_minutes(duration)} lang; erlaubt sind "
                    f"höchstens {_minutes(self.max_video_seconds)}. Bitte einen "
                    "kürzeren Mitschnitt verlinken."
                )

            folder = self._folder(video_id)
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
            language, kind = _pick_subtitles(info)
            try:
                self._download(command, watch_url(video_id), folder, language, kind)
                if not self.audio_path(video_id):
                    raise YouTubeError(
                        "yt-dlp hat keine Tonspur geliefert (vielleicht ist das "
                        "Video gesperrt oder altersbeschränkt)."
                    )
                video = Video(
                    id=video_id,
                    title=str(info.get("title") or video_id),
                    url=watch_url(video_id),
                    duration=duration,
                    uploader=str(info.get("uploader") or info.get("channel") or ""),
                    thumbnail=str(info.get("thumbnail") or ""),
                )
                segments, found_language = _read_subtitles(folder, language)
                if segments:
                    video.transcript = segments
                    video.transcript_kind = kind
                    video.transcript_language = found_language
                _write_json(os.path.join(folder, "video.json"), video.as_dict())
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        self._prune()
        return video

    def clip(
        self, video_id: str, start: float, end: float, *, url: str | None = None
    ) -> bytes:
        """Den gewählten Ausschnitt als MP3 herausschneiden."""
        video_id = check_video_id(video_id)
        length = round(float(end) - float(start), 3)
        if length <= 0:
            raise YouTubeError("Die Endzeit muss hinter der Startzeit liegen.")
        if length > self.max_clip_seconds:
            raise YouTubeError(
                f"Der Ausschnitt ist {_minutes(length)} lang; erlaubt sind "
                f"höchstens {_minutes(self.max_clip_seconds)}."
            )
        path = self.audio_path(video_id)
        if path is None:
            # Der Zwischenspeicher ist wegwerfbar: notfalls neu laden.
            self.fetch(url or watch_url(video_id))
            path = self.audio_path(video_id)
        if path is None:
            raise YouTubeError("Die Tonspur des Videos ist nicht mehr vorhanden.")
        return clip_to_mp3(
            path,
            start=max(0.0, float(start)),
            seconds=length,
            bitrate=self.mp3_bitrate,
        )

    # -- Zwischenspeicher aufräumen --------------------------------------
    def _prune(self) -> None:
        """Nur die zuletzt benutzten Videos behalten."""
        try:
            entries = [
                os.path.join(self.cache_dir, name)
                for name in os.listdir(self.cache_dir)
                if _VIDEO_ID_RE.match(name)
            ]
        except OSError:
            return
        folders = [path for path in entries if os.path.isdir(path)]
        if len(folders) <= self.cache_entries:
            return
        folders.sort(key=_mtime, reverse=True)
        for path in folders[self.cache_entries :]:
            shutil.rmtree(path, ignore_errors=True)

    # -- yt-dlp ----------------------------------------------------------
    def _probe(self, command: list[str], url: str) -> dict[str, Any]:
        """Stammdaten abfragen, bevor irgendetwas heruntergeladen wird."""
        result = self._run(
            [
                *command,
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--skip-download",
                "--dump-single-json",
                "--",
                url,
            ]
        )
        try:
            info = json.loads(result.stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            raise YouTubeError("Die Antwort von yt-dlp war nicht lesbar.") from exc
        if not isinstance(info, dict):
            raise YouTubeError("yt-dlp hat kein einzelnes Video geliefert.")
        return info

    def _download(
        self,
        command: list[str],
        url: str,
        folder: str,
        language: str,
        kind: str,
    ) -> None:
        args = [
            *command,
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--retries",
            "3",
            "--socket-timeout",
            "30",
            "-f",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "5",
            "-o",
            os.path.join(folder, "audio.%(ext)s"),
        ]
        if language:
            args += [
                "--write-subs" if kind == "manual" else "--write-auto-subs",
                "--sub-langs",
                language,
                "--sub-format",
                "vtt/best",
                "--convert-subs",
                "vtt",
            ]
        args += ["--", url]
        self._run(args)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes]:
        """yt-dlp aufrufen -- ohne Shell, der Link ist ein eigenes Argument."""
        try:
            result = subprocess.run(
                args, capture_output=True, timeout=self.timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            raise YouTubeError(
                "yt-dlp hat zu lange gebraucht und wurde abgebrochen."
            ) from exc
        except OSError as exc:
            raise YouTubeUnavailable(f"yt-dlp ließ sich nicht starten: {exc}") from exc
        if result.returncode != 0:
            raise YouTubeError(f"yt-dlp: {_last_error(result.stderr)}")
        return result


# -- Hilfen ----------------------------------------------------------------
def _pick_subtitles(info: dict[str, Any]) -> tuple[str, str]:
    """Sprache und Art der Untertitel wählen: eigene vor automatischen."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if v}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
    spoken = str(info.get("language") or "").split("-")[0].lower()
    wanted = [lang for lang in (spoken, *_PREFERRED_SUB_LANGS) if lang]
    for available, kind in ((manual, "manual"), (auto, "auto")):
        if not available:
            continue
        for lang in wanted:
            for key in available:
                if key.split("-")[0].lower() == lang:
                    return key, kind
        return sorted(available)[0], kind
    return "", ""


def _read_subtitles(folder: str, language: str) -> tuple[list[Segment], str]:
    """Die von yt-dlp abgelegte .vtt-Datei einlesen."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return [], ""
    for name in names:
        if not name.endswith(".vtt"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                segments = parse_vtt(handle.read())
        except (OSError, UnicodeDecodeError):
            continue
        if segments:
            # "audio.de.vtt" -> "de"
            parts = name.split(".")
            found = parts[-2] if len(parts) > 2 else language
            return segments, found
    return [], ""


def _write_json(path: str, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _touch(folder: str) -> None:
    try:
        os.utime(folder, None)
    except OSError:
        pass


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _minutes(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} Sekunden"
    return f"{seconds // 60}:{seconds % 60:02d} Minuten"


def _last_error(stderr: bytes) -> str:
    lines = [
        line.strip()
        for line in stderr.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]
    if not lines:
        return "Der Aufruf ist fehlgeschlagen."
    # Die letzte ERROR-Zeile sagt am ehesten, was los war.
    for line in reversed(lines):
        if line.upper().startswith("ERROR"):
            return line[:300]
    return lines[-1][:300]


__all__ = [
    "Segment",
    "Video",
    "YouTubeError",
    "YouTubeService",
    "YouTubeUnavailable",
    "parse_vtt",
    "transcript_between",
    "video_id_from_url",
    "watch_url",
]
