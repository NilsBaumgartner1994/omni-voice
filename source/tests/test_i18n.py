"""Übersetzungen und der /api/i18n-Endpunkt."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omnivoice_server import i18n
from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine

# Die Steuerzeichen, die OmniVoice versteht -- Reihenfolge egal.
EXPECTED_TAGS = {
    "[laughter]",
    "[sigh]",
    "[confirmation-en]",
    "[question-en]",
    "[question-ah]",
    "[question-oh]",
    "[question-ei]",
    "[question-yi]",
    "[surprise-ah]",
    "[surprise-oh]",
    "[surprise-wa]",
    "[surprise-yo]",
    "[dissatisfaction-hnn]",
}


@pytest.fixture()
def client() -> TestClient:
    settings = Settings(engine="dummy", load_asr=False, max_text_chars=200)
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


def test_default_locale_is_german() -> None:
    assert i18n.DEFAULT_LOCALE == "de"
    assert i18n.translate("tag.laughter") == "Lachen"


def test_every_locale_covers_every_tag() -> None:
    for locale in i18n.LOCALES:
        tags = i18n.sound_tags(locale)
        assert {entry["tag"] for entry in tags} == EXPECTED_TAGS
        # Keine leere oder auf den Schlüssel zurückgefallene Beschriftung.
        for entry in tags:
            assert entry["label"] and not entry["label"].startswith("tag.")


def test_tags_are_sorted_alphabetically_per_locale() -> None:
    for locale in i18n.LOCALES:
        labels = [entry["label"] for entry in i18n.sound_tags(locale)]
        assert labels == sorted(labels, key=i18n.sort_key)


def test_german_sorting_folds_umlauts() -> None:
    labels = [entry["label"] for entry in i18n.sound_tags("de")]
    # "Überraschung" gehört zwischen "Seufzen" und "Unmut", nicht ans Ende.
    assert labels[0] == "Lachen"
    assert labels.index("Überraschung „ah!“") > labels.index("Seufzen")
    assert labels.index("Überraschung „yo!“") < labels.index("Unmut „hnn“")
    assert labels[-1] == "Zustimmung „mhm“"


def test_english_labels_differ_and_sort_on_their_own() -> None:
    english = i18n.sound_tags("en")
    assert english[0]["label"] == "Confirmation “mhm”"
    assert [entry["tag"] for entry in english] != [
        entry["tag"] for entry in i18n.sound_tags("de")
    ]


def test_resolve_locale() -> None:
    assert i18n.resolve_locale() == "de"
    assert i18n.resolve_locale("en") == "en"
    assert i18n.resolve_locale("EN_gb") == "en"
    # Unbekanntes fällt auf Deutsch zurück.
    assert i18n.resolve_locale("fr") == "de"
    # Ohne Wunsch entscheidet der Browser-Header.
    assert i18n.resolve_locale(None, "en-US,en;q=0.9") == "en"
    assert i18n.resolve_locale(None, "fr-FR,fr;q=0.9") == "de"
    # Der ausdrückliche Wunsch sticht den Header.
    assert i18n.resolve_locale("de", "en-US") == "de"


def test_messages_fall_back_to_german() -> None:
    keys = set(i18n.TRANSLATIONS["de"])
    for locale in i18n.LOCALES:
        assert set(i18n.messages(locale)) == keys


def test_api_returns_catalogue(client: TestClient) -> None:
    data = client.get("/api/i18n").json()
    assert data["locale"] == "de"
    assert data["default_locale"] == "de"
    assert "en" in data["locales"]
    assert data["messages"]["tag.laughter"] == "Lachen"
    assert {entry["tag"] for entry in data["sound_tags"]} == EXPECTED_TAGS
    assert data["sound_tags"][0]["label"] == "Lachen"


def test_api_honours_locale_and_header(client: TestClient) -> None:
    data = client.get("/api/i18n?locale=en").json()
    assert data["locale"] == "en"
    assert data["messages"]["tag.laughter"] == "Laughter"

    header = client.get("/api/i18n", headers={"Accept-Language": "en-GB,en;q=0.8"})
    assert header.json()["locale"] == "en"

    unknown = client.get("/api/i18n?locale=fr").json()
    assert unknown["locale"] == "de"


def test_ui_serves_the_dropdown(client: TestClient) -> None:
    page = client.get("/").text
    assert 'id="sound-tag"' in page
    assert client.get("/i18n.js").status_code == 200
    assert "buildSoundTags" in client.get("/app.js").text
