"""Bilder zu einem Namen im Internet suchen.

Gesucht wird bei Wikipedia und Wikimedia Commons: beides antwortet ohne
Anmeldung und ohne Schlüssel, liefert zu Personen des öffentlichen Lebens
brauchbare Portraits und nennt zu jedem Bild die Lizenz. Eine allgemeine
Bildersuche (Google, Bing) ist ohne kostenpflichtigen Schlüssel nicht
erlaubt -- dafür verlinkt die Oberfläche stattdessen die Suche im Browser.

Nur Bilder von ``upload.wikimedia.org`` werden auch heruntergeladen: was in
die Bibliothek wandert, soll nicht von einer beliebigen Adresse kommen, die
jemand dem Server unterschiebt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

USER_AGENT = (
    "OmniVoice-Docker/1.0 (self-hosted voice library; "
    "https://github.com/NilsBaumgartner1994/omni-voice)"
)

# Von dort liefert die MediaWiki-API ihre Bilddateien aus.
ALLOWED_IMAGE_HOSTS = {"upload.wikimedia.org"}

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_QUERY_CHARS = 100


class ImageSearchError(RuntimeError):
    """Die Suche oder der Download hat nicht geklappt."""


@dataclass
class ImageHit:
    """Ein gefundenes Bild samt Herkunft."""

    title: str
    thumbnail: str
    url: str
    source: str = ""
    credit: str = ""
    width: int = 0
    height: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "thumbnail": self.thumbnail,
            "url": self.url,
            "source": self.source,
            "credit": self.credit,
            "width": self.width,
            "height": self.height,
        }


def browser_search_urls(query: str) -> list[dict[str, str]]:
    """Fertige Links auf die Bildersuche gängiger Suchmaschinen.

    Damit bleibt der Weg offen, ein Bild von Hand zu holen, wenn bei
    Wikipedia nichts Passendes liegt.
    """
    escaped = urllib.parse.quote_plus(query.strip())
    return [
        {
            "label": "DuckDuckGo",
            "url": f"https://duckduckgo.com/?q={escaped}&iax=images&ia=images",
        },
        {
            "label": "Google",
            "url": f"https://www.google.com/search?q={escaped}&tbm=isch",
        },
        {
            "label": "Bing",
            "url": f"https://www.bing.com/images/search?q={escaped}",
        },
    ]


class ImageSearch:
    """Sucht Bilder und holt das ausgewählte herunter."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        language: str = "de",
        timeout_seconds: int = 15,
        max_bytes: int = 5 * 1024 * 1024,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.enabled = enabled
        self.language = re.sub(r"[^a-z-]", "", (language or "de").lower()) or "de"
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    def availability(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "reason": "In der Konfiguration abgeschaltet."}
        return {"enabled": True, "reason": ""}

    # -- Suche -----------------------------------------------------------
    def search(self, query: str, limit: int = 8) -> list[ImageHit]:
        """Erst Artikelbilder (meist das Portrait), dann Commons-Dateien."""
        if not self.enabled:
            raise ImageSearchError(
                "Die Bildersuche ist auf diesem Server abgeschaltet."
            )
        text = (query or "").strip()
        if not text:
            raise ImageSearchError("Bitte einen Suchbegriff (den Namen) angeben.")
        text = text[:MAX_QUERY_CHARS]
        limit = max(1, min(int(limit or 8), 20))

        hits: list[ImageHit] = []
        seen: set[str] = set()
        for finder in (self._search_wikipedia, self._search_commons):
            if len(hits) >= limit:
                break
            try:
                found = finder(text, limit)
            except ImageSearchError:
                raise
            except Exception:  # noqa: BLE001 - eine Quelle darf ausfallen
                logger.warning("Bildersuche fehlgeschlagen", exc_info=True)
                continue
            for hit in found:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                hits.append(hit)
                if len(hits) >= limit:
                    break
        return hits

    def _search_wikipedia(self, query: str, limit: int) -> list[ImageHit]:
        endpoint = f"https://{self.language}.wikipedia.org/w/api.php"
        data = self._api(
            endpoint,
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": query,
                "gsrlimit": str(limit),
                "gsrnamespace": "0",
                "prop": "pageimages|info",
                "piprop": "thumbnail|original",
                "pithumbsize": "320",
                "inprop": "url",
            },
        )
        hits = []
        for page in (data.get("query") or {}).get("pages") or []:
            original = (page.get("original") or {}).get("source") or ""
            thumbnail = (page.get("thumbnail") or {}).get("source") or original
            if not original or not _is_image(original):
                continue
            hits.append(
                ImageHit(
                    title=str(page.get("title") or query),
                    thumbnail=thumbnail,
                    url=original,
                    source=str(page.get("fullurl") or ""),
                    credit="Wikipedia",
                    width=int((page.get("original") or {}).get("width") or 0),
                    height=int((page.get("original") or {}).get("height") or 0),
                )
            )
        return hits

    def _search_commons(self, query: str, limit: int) -> list[ImageHit]:
        data = self._api(
            "https://commons.wikimedia.org/w/api.php",
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "search",
                "gsrsearch": f"{query} filetype:bitmap",
                "gsrlimit": str(limit),
                "gsrnamespace": "6",
                "prop": "imageinfo",
                "iiprop": "url|size|extmetadata",
                "iiurlwidth": "320",
            },
        )
        hits = []
        for page in (data.get("query") or {}).get("pages") or []:
            info = (page.get("imageinfo") or [{}])[0]
            url = str(info.get("url") or "")
            if not url or not _is_image(url):
                continue
            meta = info.get("extmetadata") or {}
            artist = _plain(str((meta.get("Artist") or {}).get("value") or ""))
            licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
            hits.append(
                ImageHit(
                    title=str(page.get("title") or "").removeprefix("File:"),
                    thumbnail=str(info.get("thumburl") or url),
                    url=url,
                    source=str(info.get("descriptionurl") or ""),
                    credit=" · ".join(
                        part for part in ("Wikimedia Commons", artist, licence) if part
                    ),
                    width=int(info.get("width") or 0),
                    height=int(info.get("height") or 0),
                )
            )
        return hits

    def _api(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        payload = self._get(url, limit=2 * 1024 * 1024)[0]
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except ValueError as exc:
            raise ImageSearchError("Die Antwort der Bildersuche war unlesbar.") from exc
        return data if isinstance(data, dict) else {}

    # -- Herunterladen ---------------------------------------------------
    def download(self, url: str) -> tuple[bytes, str, str]:
        """Ein gefundenes Bild holen: ``(daten, dateiname, medientyp)``."""
        if not self.enabled:
            raise ImageSearchError(
                "Die Bildersuche ist auf diesem Server abgeschaltet."
            )
        parsed = urllib.parse.urlparse((url or "").strip())
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_IMAGE_HOSTS:
            raise ImageSearchError(
                "Übernommen werden nur Bilder aus den Suchergebnissen "
                f"({', '.join(sorted(ALLOWED_IMAGE_HOSTS))})."
            )
        suffix = _suffix(parsed.path)
        if suffix not in _IMAGE_SUFFIXES:
            raise ImageSearchError(f"Dateityp {suffix or '?'} wird nicht unterstützt.")
        data, media_type = self._get(url, limit=self.max_bytes)
        if not data:
            raise ImageSearchError("Das Bild war leer.")
        if len(data) > self.max_bytes:
            raise ImageSearchError(
                f"Das Bild ist zu groß (max. {self.max_bytes // (1024 * 1024)} MB)."
            )
        return data, f"portrait{suffix}", media_type or _MEDIA_TYPES[suffix]

    def _get(self, url: str, *, limit: int) -> tuple[bytes, str]:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - Schema oben geprüft
                request, timeout=self.timeout_seconds
            ) as response:
                # Ein Byte mehr lesen, damit ein zu großes Bild auffällt.
                payload = response.read(limit + 1)
                media_type = (response.headers.get("Content-Type") or "").split(";")[0]
        except urllib.error.HTTPError as exc:
            raise ImageSearchError(
                f"Die Quelle antwortete mit HTTP {exc.code}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageSearchError(f"Die Quelle war nicht erreichbar: {exc}") from exc
        if len(payload) > limit:
            raise ImageSearchError(
                f"Die Antwort ist zu groß (max. {limit // (1024 * 1024)} MB)."
            )
        return payload, media_type.strip()


def _suffix(path: str) -> str:
    return os.path.splitext(urllib.parse.unquote(path or ""))[1].lower()


def _is_image(url: str) -> bool:
    return _suffix(urllib.parse.urlparse(url).path) in _IMAGE_SUFFIXES


def _plain(value: str) -> str:
    """Aus dem HTML-Schnipsel der Lizenzangabe reinen Text machen."""
    text = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", text).strip()[:120]


__all__ = [
    "ALLOWED_IMAGE_HOSTS",
    "ImageHit",
    "ImageSearch",
    "ImageSearchError",
    "browser_search_urls",
]
