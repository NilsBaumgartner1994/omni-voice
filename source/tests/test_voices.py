"""Stimm-Bibliothek: Ablage, Web-API und Trennung von den Modellgewichten."""

from __future__ import annotations

import io
import os
import wave

import pytest
from fastapi.testclient import TestClient

from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine
from omnivoice_server.library import Upload, VoiceLibrary, VoiceNotFound


def _wav(seconds: float = 0.2, value: int = 1000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        frames = int(seconds * 16000)
        handle.writeframes((value).to_bytes(2, "little", signed=True) * frames)
    return buffer.getvalue()


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def library(tmp_path) -> VoiceLibrary:
    return VoiceLibrary(str(tmp_path / "voices"))


@pytest.fixture()
def client(tmp_path) -> TestClient:
    settings = Settings(
        engine="dummy",
        load_asr=False,
        max_text_chars=200,
        library_dir=str(tmp_path / "voices"),
    )
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


def _create(client: TestClient, name: str = "Anna Beispiel", **extra) -> dict:
    files = {"ref_audio": ("anna.wav", _wav(), "audio/wav")}
    if extra.pop("with_image", True):
        files["image"] = ("anna.png", PNG, "image/png")
    data = {"name": name, "ref_text": "Dies ist meine Stimme.", **extra}
    response = client.post("/api/voices", data=data, files=files)
    assert response.status_code == 201, response.text
    return response.json()


# -- Bibliothek (ohne Web, ohne Modell) --------------------------------------
def test_fingerprint_ignores_name_and_image(library: VoiceLibrary) -> None:
    voice = library.create(
        name="Anna", audio=Upload("a.wav", _wav()), ref_text="Hallo Welt"
    )
    before = voice.fingerprint

    renamed = library.update(voice.id, name="Anna B.", image=Upload("p.png", PNG))
    assert renamed.fingerprint == before, "Name/Bild dürfen die Stimme nicht ändern"

    retexted = library.update(voice.id, ref_text="Etwas anderes")
    assert retexted.fingerprint != before, "Referenztext gehört zur Stimme"


def test_rejects_unknown_ids_and_paths(library: VoiceLibrary) -> None:
    for bad in ("../etc", "..", "/etc/passwd", "Anna", ""):
        with pytest.raises(VoiceNotFound):
            library.get(bad)


def test_rejects_wrong_file_types(library: VoiceLibrary) -> None:
    with pytest.raises(Exception, match="wird nicht unterstützt"):
        library.create(name="Anna", audio=Upload("notiz.txt", b"kein audio"))


def test_derived_data_lives_apart_from_the_sources(library: VoiceLibrary) -> None:
    voice = library.create(name="Anna", audio=Upload("a.wav", _wav()))
    library.write_derived(voice, "modell-a", b"berechnet")

    assert library.read_derived(voice, "modell-a") == b"berechnet"
    assert library.read_derived(voice, "modell-b") is None

    # Wegwerfen darf nur das Berechnete treffen.
    library.clear_derived(voice.id)
    assert library.read_derived(voice, "modell-a") is None
    assert library.get(voice.id).name == "Anna"
    assert library.audio_path(voice.id) is not None


def test_new_reference_audio_invalidates_derived(library: VoiceLibrary) -> None:
    voice = library.create(name="Anna", audio=Upload("a.wav", _wav()))
    library.write_derived(voice, "modell-a", b"berechnet")

    updated = library.update(voice.id, audio=Upload("b.wav", _wav(value=2000)))
    assert library.read_derived(updated, "modell-a") is None


# -- Web-API ------------------------------------------------------------------
def test_create_list_and_serve_assets(client: TestClient) -> None:
    created = _create(client)
    assert created["name"] == "Anna Beispiel"
    assert created["has_audio"] and created["has_image"]
    assert created["prepared"] is False

    listing = client.get("/api/voices").json()
    assert listing["count"] == 1
    assert listing["voices"][0]["id"] == created["id"]

    audio = client.get(f"/api/voices/{created['id']}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")

    image = client.get(f"/api/voices/{created['id']}/image")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")


def test_create_needs_name_and_audio(client: TestClient) -> None:
    without_audio = client.post("/api/voices", data={"name": "Anna"})
    assert without_audio.status_code == 422

    without_name = client.post(
        "/api/voices",
        data={"name": "  "},
        files={"ref_audio": ("a.wav", _wav(), "audio/wav")},
    )
    assert without_name.status_code == 422


def test_unknown_voice_is_404(client: TestClient) -> None:
    assert client.get("/api/voices/gibt-es-nicht").status_code == 404
    assert client.post("/api/voices/gibt-es-nicht/prepare").status_code == 404
    assert client.delete("/api/voices/gibt-es-nicht").status_code == 404


def test_prepare_then_generate_with_saved_voice(client: TestClient) -> None:
    voice = _create(client)

    prepared = client.post(f"/api/voices/{voice['id']}/prepare")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["prepared"] is True
    assert client.get("/api/voices").json()["voices"][0]["prepared"] is True

    response = client.post(
        "/api/tts", data={"text": "Hallo aus der Bibliothek.", "voice_id": voice["id"]}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/wav"


def test_generation_prepares_on_demand(client: TestClient) -> None:
    voice = _create(client)
    response = client.post(
        "/api/tts", data={"text": "Ohne Vorbereitung.", "voice_id": voice["id"]}
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/voices/{voice['id']}").json()["prepared"] is True


def test_update_keeps_person_but_drops_the_computed_voice(client: TestClient) -> None:
    voice = _create(client)
    client.post(f"/api/voices/{voice['id']}/prepare")

    updated = client.post(
        f"/api/voices/{voice['id']}",
        data={"name": "Anna Neu"},
        files={"ref_audio": ("neu.wav", _wav(value=3000), "audio/wav")},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["name"] == "Anna Neu"
    assert body["prepared"] is False, "neues Referenz-Audio => neu berechnen"
    assert body["fingerprint"] != voice["fingerprint"]


def test_switching_the_model_keeps_people_and_recomputes_voices(
    client: TestClient,
) -> None:
    """Der eigentliche Zweck der Trennung: Gewichte tauschen, Rest bleibt."""
    voice = _create(client)
    client.post(f"/api/voices/{voice['id']}/prepare")
    engine = client.app.state.engine

    engine.status.model = "ein-anderes-stimmmodell"
    client.app.state.voices.forget(voice["id"])

    listing = client.get("/api/voices").json()
    entry = listing["voices"][0]
    assert entry["prepared"] is False, "für neue Gewichte ist nichts berechnet"
    assert entry["name"] == voice["name"], "die Person bleibt unangetastet"
    assert entry["fingerprint"] == voice["fingerprint"]
    assert listing["model_key"] != voice["model_key"]

    result = client.post("/api/voices/prepare-all")
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["failed"] == 0 and body["count"] == 1
    assert client.get("/api/voices").json()["voices"][0]["prepared"] is True


def test_delete_removes_everything(client: TestClient, tmp_path) -> None:
    voice = _create(client)
    client.post(f"/api/voices/{voice['id']}/prepare")

    assert client.delete(f"/api/voices/{voice['id']}").status_code == 200
    assert client.get("/api/voices").json()["count"] == 0
    assert not os.path.exists(tmp_path / "voices" / voice["id"])


def test_info_exposes_the_model_key(client: TestClient) -> None:
    info = client.get("/api/info").json()
    assert info["voice_model_key"]
    assert info["library_dir"]
