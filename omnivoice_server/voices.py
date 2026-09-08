"""Brücke zwischen Stimm-Bibliothek und TTS-Modell.

Die Bibliothek (:mod:`omnivoice_server.library`) kennt kein Modell, die Engine
(:mod:`omnivoice_server.engine`) kennt keine Bibliothek. Dieses Modul bringt
beide zusammen: es lässt die Engine aus den Quelldaten einer Person eine Stimme
berechnen, legt das Ergebnis unter dem Schlüssel der aktuell geladenen Gewichte
ab und findet es beim nächsten Mal wieder.

Damit hängt am Modell nur noch das Berechnete: Gewichte tauschen, einmal
"neu berechnen" -- Namen, Bilder, Referenzaufnahmen und Texte bleiben stehen.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from .engine import BaseEngine
from .library import Voice, VoiceLibrary

logger = logging.getLogger(__name__)


class VoiceService:
    """Berechnet, speichert und findet modellabhängige Stimmen."""

    def __init__(
        self,
        library: VoiceLibrary,
        engine: BaseEngine,
        cache_size: int = 8,
    ) -> None:
        self.library = library
        self.engine = engine
        self.cache_size = max(1, cache_size)
        self._cache: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def model_key(self) -> str:
        """Schlüssel der aktuell geladenen Gewichte."""
        return self.engine.voice_key

    # -- Zustand ---------------------------------------------------------
    def describe(self, voice: Voice) -> dict[str, Any]:
        """Stammdaten plus: liegt für die aktuellen Gewichte etwas bereit?"""
        info = self.library.derived_info(voice, self.model_key)
        payload = voice.as_dict()
        payload["prepared"] = info is not None or self._key(voice) in self._cache
        payload["prepared_at"] = (info or {}).get("created_at")
        payload["model_key"] = self.model_key
        payload["has_audio"] = voice.audio is not None
        payload["has_image"] = voice.image is not None
        return payload

    def _key(self, voice: Voice) -> tuple[str, str, str]:
        return (voice.id, voice.fingerprint, self.model_key)

    # -- Nutzung ---------------------------------------------------------
    def artifact(self, voice: Voice, *, compute: bool = True) -> Any | None:
        """Berechnete Stimme holen: Speicher, dann Platte, dann rechnen."""
        key = self._key(voice)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

            payload = self.library.read_derived(voice, self.model_key)
            if payload is not None:
                artifact = self.engine.deserialize_voice(payload)
                if artifact is not None:
                    self._remember(key, artifact)
                    return artifact
                # Unlesbar oder von einer anderen Version geschrieben.
                self.library.clear_derived(voice.id, self.model_key)

            if not compute:
                return None

            artifact = self._compute(voice)
            self._remember(key, artifact)
            return artifact

    def prepare(self, voice: Voice, *, force: bool = False) -> dict[str, Any]:
        """Stimme für die aktuellen Gewichte berechnen und ablegen."""
        with self._lock:
            if force:
                self._cache.pop(self._key(voice), None)
                self.library.clear_derived(voice.id, self.model_key)
            elif self.library.derived_info(voice, self.model_key) is not None:
                return {"prepared": True, "recomputed": False, "seconds": 0.0}

            started = time.perf_counter()
            artifact = self._compute(voice)
            elapsed = time.perf_counter() - started
            self._remember(self._key(voice), artifact)

        return {
            "prepared": True,
            "recomputed": True,
            "seconds": round(elapsed, 2),
        }

    def forget(self, voice_id: str) -> None:
        """Zwischengespeicherte Stimmen einer Person vergessen (RAM)."""
        with self._lock:
            for key in [k for k in self._cache if k[0] == voice_id]:
                self._cache.pop(key, None)

    # -- intern ----------------------------------------------------------
    def _compute(self, voice: Voice) -> Any:
        """Rechnen lassen und -- wenn möglich -- auf die Platte legen."""
        audio_path = self.library.audio_path(voice.id)
        if not audio_path:
            raise FileNotFoundError(
                f"Für '{voice.name}' ist kein Referenz-Audio hinterlegt."
            )
        artifact = self.engine.prepare_voice(audio_path, voice.ref_text or None)
        payload = self.engine.serialize_voice(artifact)
        if payload:
            try:
                self.library.write_derived(voice, self.model_key, payload)
            except OSError:
                logger.warning(
                    "Berechnete Stimme konnte nicht gespeichert werden",
                    exc_info=True,
                )
        return artifact

    def _remember(self, key: tuple[str, str, str], artifact: Any) -> None:
        self._cache[key] = artifact
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
