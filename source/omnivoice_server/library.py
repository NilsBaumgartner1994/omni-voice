"""Stimm-Bibliothek: Personen, Bilder, Referenz-Audio und Referenztext.

Dieses Modul ist bewusst frei von Modell-Abhängigkeiten (kein torch, kein
omnivoice). Es kennt nur die *Quelldaten* einer Person -- also alles, was ein
Mensch eingibt und was beim Wechsel des TTS-Modells unverändert bleibt.

Was ein konkretes Modell daraus berechnet, liegt strikt getrennt davon unter
``<id>/derived/<modell-schlüssel>.bin`` und ist jederzeit wegwerfbar: die
Bibliothek behandelt es als undurchsichtige Bytefolge, erzeugt es nie selbst
und verliert nichts, wenn es gelöscht wird.

Ablage auf der Platte::

    <root>/<id>/voice.json          Stammdaten
    <root>/<id>/reference.wav       Referenz-Audio (Original-Dateiendung)
    <root>/<id>/portrait.jpg        Bild (optional)
    <root>/<id>/derived/<key>.bin   vom Modell berechnete Stimme
    <root>/<id>/derived/<key>.json  wozu sie gehört (Modell + Fingerabdruck)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ALLOWED_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".m4a",
    ".webm",
    ".opus",
    ".aac",
}

ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".webm": "audio/webm",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Die ID landet in Dateipfaden und in URLs, deshalb eng geführt.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

MAX_NAME_CHARS = 80
MAX_REF_TEXT_CHARS = 2000
MAX_DESCRIPTION_CHARS = 500


class LibraryError(RuntimeError):
    """Vom Benutzer behebbares Problem (fehlender Name, falscher Dateityp ...)."""


class VoiceNotFound(LibraryError):
    """Es gibt keine Stimme mit dieser ID."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _suffix(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _slug(name: str) -> str:
    plain = name.lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        plain = plain.replace(src, dst)
    plain = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
    return plain[:40] or "stimme"


@dataclass(frozen=True)
class Upload:
    """Eine hochgeladene Datei, losgelöst vom Web-Framework."""

    filename: str
    data: bytes
    media_type: str | None = None


@dataclass
class Asset:
    """Eine in der Bibliothek abgelegte Datei."""

    filename: str
    media_type: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Asset:
        return cls(
            filename=str(data.get("filename") or ""),
            media_type=str(data.get("media_type") or "application/octet-stream"),
            size=int(data.get("size") or 0),
            sha256=str(data.get("sha256") or ""),
        )


@dataclass
class Voice:
    """Eine Person mit ihrer Referenzstimme."""

    id: str
    name: str
    description: str = ""
    ref_text: str = ""
    language: str | None = None
    audio: Asset | None = None
    image: Asset | None = None
    # Woher die Referenzaufnahme stammt (z. B. YouTube-Link mit Zeitmarken).
    # Reine Herkunftsangabe: die Stimme hängt nicht daran.
    source: dict[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""
    revision: int = 1

    @property
    def fingerprint(self) -> str:
        """Identität der Quelldaten, aus denen ein Modell die Stimme rechnet.

        Nur Referenz-Audio und Referenztext gehen ein: Name oder Bild zu
        ändern macht eine bereits berechnete Stimme nicht ungültig.
        """
        audio_hash = self.audio.sha256 if self.audio else ""
        return _sha256(f"{audio_hash}\n{self.ref_text.strip()}".encode())[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "ref_text": self.ref_text,
            "language": self.language,
            "audio": self.audio.as_dict() if self.audio else None,
            "image": self.image.as_dict() if self.image else None,
            "source": self.source or None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Voice:
        audio = data.get("audio")
        image = data.get("image")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            description=str(data.get("description") or ""),
            ref_text=str(data.get("ref_text") or ""),
            language=data.get("language") or None,
            audio=Asset.from_dict(audio) if audio else None,
            image=Asset.from_dict(image) if image else None,
            source=data.get("source") if isinstance(data.get("source"), dict) else None,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            revision=int(data.get("revision") or 1),
        )


def _check_text(value: str | None, limit: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise LibraryError(f"{label} ist zu lang (max. {limit} Zeichen).")
    return text


class VoiceLibrary:
    """Ablage der Personen/Stimmen in einem Verzeichnis."""

    def __init__(
        self,
        root: str,
        max_audio_bytes: int = 25 * 1024 * 1024,
        max_image_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        self.max_audio_bytes = max_audio_bytes
        self.max_image_bytes = max_image_bytes
        self._lock = threading.Lock()

    # -- Pfade -----------------------------------------------------------
    def _dir(self, voice_id: str) -> str:
        if not _ID_RE.match(voice_id or ""):
            raise VoiceNotFound(f"Unbekannte Stimme: {voice_id!r}")
        return os.path.join(self.root, voice_id)

    def _meta_path(self, voice_id: str) -> str:
        return os.path.join(self._dir(voice_id), "voice.json")

    # -- Lesen -----------------------------------------------------------
    def list(self) -> list[Voice]:
        if not os.path.isdir(self.root):
            return []
        voices = []
        for entry in sorted(os.listdir(self.root)):
            if not _ID_RE.match(entry):
                continue
            try:
                voices.append(self.get(entry))
            except (VoiceNotFound, ValueError, OSError):
                continue
        voices.sort(key=lambda voice: voice.name.lower())
        return voices

    def get(self, voice_id: str) -> Voice:
        path = self._meta_path(voice_id)
        if not os.path.isfile(path):
            raise VoiceNotFound(f"Unbekannte Stimme: {voice_id!r}")
        with open(path, encoding="utf-8") as handle:
            return Voice.from_dict(json.load(handle))

    def audio_path(self, voice_id: str) -> str | None:
        voice = self.get(voice_id)
        if not voice.audio:
            return None
        path = os.path.join(self._dir(voice_id), voice.audio.filename)
        return path if os.path.isfile(path) else None

    def image_path(self, voice_id: str) -> str | None:
        voice = self.get(voice_id)
        if not voice.image:
            return None
        path = os.path.join(self._dir(voice_id), voice.image.filename)
        return path if os.path.isfile(path) else None

    # -- Schreiben -------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        audio: Upload,
        ref_text: str = "",
        description: str = "",
        language: str | None = None,
        image: Upload | None = None,
        source: dict[str, Any] | None = None,
    ) -> Voice:
        clean_name = _check_text(name, MAX_NAME_CHARS, "Der Name")
        if not clean_name:
            raise LibraryError("Bitte einen Namen angeben.")
        if audio is None:
            raise LibraryError("Bitte ein Referenz-Audio hochladen.")

        with self._lock:
            voice_id = f"{_slug(clean_name)}-{uuid.uuid4().hex[:6]}"
            folder = os.path.join(self.root, voice_id)
            os.makedirs(folder, exist_ok=True)
            try:
                voice = Voice(
                    id=voice_id,
                    name=clean_name,
                    description=_check_text(
                        description, MAX_DESCRIPTION_CHARS, "Die Beschreibung"
                    ),
                    ref_text=_check_text(
                        ref_text, MAX_REF_TEXT_CHARS, "Der Referenztext"
                    ),
                    language=(language or "").strip() or None,
                    source=source or None,
                    created_at=_now(),
                    updated_at=_now(),
                )
                voice.audio = self._store_asset(folder, audio, kind="audio")
                if image is not None:
                    voice.image = self._store_asset(folder, image, kind="image")
                self._write_meta(voice)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        return voice

    def update(
        self,
        voice_id: str,
        *,
        name: str | None = None,
        ref_text: str | None = None,
        description: str | None = None,
        language: str | None = None,
        audio: Upload | None = None,
        image: Upload | None = None,
        remove_image: bool = False,
        source: dict[str, Any] | None = None,
    ) -> Voice:
        with self._lock:
            voice = self.get(voice_id)
            folder = self._dir(voice_id)

            if name is not None:
                clean = _check_text(name, MAX_NAME_CHARS, "Der Name")
                if not clean:
                    raise LibraryError("Bitte einen Namen angeben.")
                voice.name = clean
            if description is not None:
                voice.description = _check_text(
                    description, MAX_DESCRIPTION_CHARS, "Die Beschreibung"
                )
            if ref_text is not None:
                voice.ref_text = _check_text(
                    ref_text, MAX_REF_TEXT_CHARS, "Der Referenztext"
                )
            if language is not None:
                voice.language = language.strip() or None
            if audio is not None:
                self._remove_asset(folder, voice.audio)
                voice.audio = self._store_asset(folder, audio, kind="audio")
                # Die Herkunft gehört zur Aufnahme: neue Aufnahme, neue (oder
                # gar keine) Quelle.
                voice.source = source or None
            elif source is not None:
                voice.source = source
            if remove_image:
                self._remove_asset(folder, voice.image)
                voice.image = None
            elif image is not None:
                self._remove_asset(folder, voice.image)
                voice.image = self._store_asset(folder, image, kind="image")

            voice.revision += 1
            voice.updated_at = _now()
            self._write_meta(voice)
        return voice

    def delete(self, voice_id: str) -> None:
        with self._lock:
            folder = self._dir(voice_id)
            if not os.path.isdir(folder):
                raise VoiceNotFound(f"Unbekannte Stimme: {voice_id!r}")
            shutil.rmtree(folder, ignore_errors=True)

    # -- Berechnete Stimmen (undurchsichtige Bytes) ----------------------
    def _derived_dir(self, voice_id: str) -> str:
        return os.path.join(self._dir(voice_id), "derived")

    @staticmethod
    def _key_hash(model_key: str) -> str:
        return _sha256(model_key.encode())[:16]

    def read_derived(self, voice: Voice, model_key: str) -> bytes | None:
        """Berechnete Stimme -- oder ``None``, wenn sie fehlt oder veraltet ist."""
        info = self.derived_info(voice, model_key)
        if info is None:
            return None
        path = os.path.join(
            self._derived_dir(voice.id), f"{self._key_hash(model_key)}.bin"
        )
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def derived_info(self, voice: Voice, model_key: str) -> dict[str, Any] | None:
        path = os.path.join(
            self._derived_dir(voice.id), f"{self._key_hash(model_key)}.json"
        )
        try:
            with open(path, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            return None
        # Nach einem neuen Referenz-Audio passt die alte Berechnung nicht mehr.
        if info.get("fingerprint") != voice.fingerprint:
            return None
        if info.get("model_key") != model_key:
            return None
        return info

    def write_derived(self, voice: Voice, model_key: str, payload: bytes) -> None:
        folder = self._derived_dir(voice.id)
        os.makedirs(folder, exist_ok=True)
        stem = os.path.join(folder, self._key_hash(model_key))
        _atomic_write(stem + ".bin", payload)
        info = {
            "model_key": model_key,
            "fingerprint": voice.fingerprint,
            "revision": voice.revision,
            "bytes": len(payload),
            "created_at": _now(),
        }
        _atomic_write(
            stem + ".json", json.dumps(info, ensure_ascii=False, indent=2).encode()
        )

    def clear_derived(self, voice_id: str, model_key: str | None = None) -> None:
        folder = self._derived_dir(voice_id)
        if not os.path.isdir(folder):
            return
        if model_key is None:
            shutil.rmtree(folder, ignore_errors=True)
            return
        stem = os.path.join(folder, self._key_hash(model_key))
        for path in (stem + ".bin", stem + ".json"):
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- intern ----------------------------------------------------------
    def _store_asset(self, folder: str, upload: Upload, *, kind: str) -> Asset:
        suffix = _suffix(upload.filename)
        if kind == "audio":
            allowed, limit, label = (
                ALLOWED_AUDIO_SUFFIXES,
                self.max_audio_bytes,
                "Referenz-Audio",
            )
            stem = "reference"
        else:
            allowed, limit, label = (
                ALLOWED_IMAGE_SUFFIXES,
                self.max_image_bytes,
                "Das Bild",
            )
            stem = "portrait"

        if suffix not in allowed:
            raise LibraryError(
                f"{label}: Dateityp {suffix or '?'} wird nicht unterstützt "
                f"(erlaubt: {', '.join(sorted(allowed))})."
            )
        if len(upload.data) > limit:
            raise LibraryError(
                f"{label} ist zu groß (max. {limit // (1024 * 1024)} MB)."
            )
        if not upload.data:
            raise LibraryError(f"{label} ist leer.")

        filename = stem + suffix
        _atomic_write(os.path.join(folder, filename), upload.data)
        return Asset(
            filename=filename,
            media_type=upload.media_type
            or _MEDIA_TYPES.get(suffix, "application/octet-stream"),
            size=len(upload.data),
            sha256=_sha256(upload.data),
        )

    @staticmethod
    def _remove_asset(folder: str, asset: Asset | None) -> None:
        if not asset:
            return
        try:
            os.unlink(os.path.join(folder, asset.filename))
        except OSError:
            pass

    def _write_meta(self, voice: Voice) -> None:
        payload = json.dumps(voice.as_dict(), ensure_ascii=False, indent=2)
        _atomic_write(self._meta_path(voice.id), payload.encode("utf-8"))


def _atomic_write(path: str, payload: bytes) -> None:
    """Schreiben ohne halbfertige Dateien, falls der Container stirbt."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
