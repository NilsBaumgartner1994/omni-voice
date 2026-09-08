"""Bildersuche zum Namen -- mit vorgegebenen Antworten statt Netz."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine
from omnivoice_server.imagesearch import ImageSearch, ImageSearchError

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

WIKIPEDIA = {
    "query": {
        "pages": [
            {
                "title": "Anna Beispiel",
                "fullurl": "https://de.wikipedia.org/wiki/Anna_Beispiel",
                "original": {
                    "source": "https://upload.wikimedia.org/anna.jpg",
                    "width": 800,
                    "height": 1000,
                },
                "thumbnail": {"source": "https://upload.wikimedia.org/anna_320.jpg"},
            },
            # Ohne Bild ist der Treffer für uns wertlos.
            {"title": "Anna (Begriffsklärung)"},
        ]
    }
}

COMMONS = {
    "query": {
        "pages": [
            {
                "title": "File:Anna zweite.jpg",
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/anna2.jpg",
                        "thumburl": "https://upload.wikimedia.org/anna2_320.jpg",
                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:x",
                        "width": 640,
                        "height": 480,
                        "extmetadata": {
                            "Artist": {"value": '<a href="#">Fotograf</a>'},
                            "LicenseShortName": {"value": "CC BY-SA 4.0"},
                        },
                    }
                ],
            },
            # Dasselbe Bild wie bei Wikipedia: darf nicht doppelt auftauchen.
            {
                "title": "File:Anna.jpg",
                "imageinfo": [{"url": "https://upload.wikimedia.org/anna.jpg"}],
            },
        ]
    }
}


class StubSearch(ImageSearch):
    """Antworten der MediaWiki-API vorgeben, Bild-Download inbegriffen."""

    def _get(self, url: str, *, limit: int) -> tuple[bytes, str]:
        if "commons.wikimedia.org/w/api.php" in url:
            return json.dumps(COMMONS).encode(), "application/json"
        if "wikipedia.org/w/api.php" in url:
            return json.dumps(WIKIPEDIA).encode(), "application/json"
        return PNG, "image/png"


@pytest.fixture()
def search() -> StubSearch:
    return StubSearch()


def _client(tmp_path, images: ImageSearch) -> TestClient:
    settings = Settings(
        engine="dummy",
        load_asr=False,
        max_text_chars=200,
        library_dir=str(tmp_path / "voices"),
    )
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False, images=images)
    return TestClient(app)


# -- Suche ------------------------------------------------------------------
def test_search_merges_both_sources_without_duplicates(search: StubSearch) -> None:
    hits = search.search("Anna Beispiel")
    assert [hit.url for hit in hits] == [
        "https://upload.wikimedia.org/anna.jpg",
        "https://upload.wikimedia.org/anna2.jpg",
    ]
    assert hits[0].credit == "Wikipedia"
    assert "CC BY-SA 4.0" in hits[1].credit
    assert "<a" not in hits[1].credit


def test_search_needs_a_query(search: StubSearch) -> None:
    with pytest.raises(ImageSearchError):
        search.search("   ")


def test_disabled_search_refuses(tmp_path) -> None:
    off = StubSearch(enabled=False)
    assert off.availability()["enabled"] is False
    with pytest.raises(ImageSearchError):
        off.search("Anna")


# -- Herunterladen ----------------------------------------------------------
def test_download_only_from_the_search_hosts(search: StubSearch) -> None:
    data, filename, media_type = search.download("https://upload.wikimedia.org/a.png")
    assert data == PNG
    assert filename == "portrait.png"
    assert media_type == "image/png"

    for url in (
        "https://example.org/a.png",  # fremder Host
        "http://upload.wikimedia.org/a.png",  # kein HTTPS
        "https://upload.wikimedia.org/a.svg",  # kein unterstütztes Bild
    ):
        with pytest.raises(ImageSearchError):
            search.download(url)


def test_download_watches_the_size() -> None:
    class Huge(StubSearch):
        def _get(self, url: str, *, limit: int) -> tuple[bytes, str]:
            return b"x" * (limit + 1), "image/png"

    with pytest.raises(ImageSearchError, match="zu groß"):
        Huge().download("https://upload.wikimedia.org/a.png")


# -- Web-API ----------------------------------------------------------------
def test_api_search(tmp_path, search: StubSearch) -> None:
    with _client(tmp_path, search) as client:
        assert client.get("/api/info").json()["image_search"]["enabled"] is True

        answer = client.get("/api/image-search", params={"q": "Anna Beispiel"})
        assert answer.status_code == 200
        body = answer.json()
        assert body["count"] == 2
        assert body["results"][0]["title"] == "Anna Beispiel"
        # Für den Fall, dass nichts passt: Links in die Bildersuche.
        assert [entry["label"] for entry in body["browser_search"]] == [
            "DuckDuckGo",
            "Google",
            "Bing",
        ]

        assert client.get("/api/image-search", params={"q": " "}).status_code == 422


def test_api_search_off(tmp_path) -> None:
    with _client(tmp_path, StubSearch(enabled=False)) as client:
        answer = client.get("/api/image-search", params={"q": "Anna"})
        assert answer.status_code == 503


def test_found_image_is_stored_with_the_person(tmp_path, search: StubSearch) -> None:
    with _client(tmp_path, search) as client:
        answer = client.post(
            "/api/voices",
            data={
                "name": "Anna Beispiel",
                "image_url": "https://upload.wikimedia.org/anna.jpg",
                "prepare": "false",
            },
            files={"ref_audio": ("anna.wav", b"RIFF0000WAVEfmt ", "audio/wav")},
        )
        assert answer.status_code == 201, answer.text
        voice = answer.json()
        assert voice["has_image"] is True
        assert voice["image"]["filename"] == "portrait.jpg"

        image = client.get(f"/api/voices/{voice['id']}/image")
        assert image.status_code == 200
        assert image.content == PNG


def test_image_from_a_foreign_host_is_refused(tmp_path, search: StubSearch) -> None:
    with _client(tmp_path, search) as client:
        answer = client.post(
            "/api/voices",
            data={"name": "Anna", "image_url": "https://example.org/anna.jpg"},
            files={"ref_audio": ("anna.wav", b"RIFF0000WAVEfmt ", "audio/wav")},
        )
        assert answer.status_code == 422
