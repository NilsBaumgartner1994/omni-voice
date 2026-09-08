"""Download-Formate: WAV immer, MP3 wenn ffmpeg da ist."""

from __future__ import annotations

import io
import json
import pathlib
import wave

import pytest
from fastapi.testclient import TestClient

from omnivoice_server import audio
from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine

needs_ffmpeg = pytest.mark.skipif(
    not audio.mp3_supported(), reason="ffmpeg ist hier nicht installiert"
)


@pytest.fixture()
def client() -> TestClient:
    settings = Settings(engine="dummy", load_asr=False, max_text_chars=200)
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


def _wav(client: TestClient) -> bytes:
    response = client.post("/api/tts", json={"text": "Hallo Welt"})
    assert response.status_code == 200
    return response.content


# ------------------------------------------------------------- Modul
def test_normalize_format_accepts_the_usual_spellings() -> None:
    assert audio.normalize_format("MP3") == "mp3"
    assert audio.normalize_format(".wav") == "wav"
    assert audio.normalize_format(None) == "wav"
    assert audio.normalize_format("", default="mp3") == "mp3"
    with pytest.raises(audio.AudioEncodeError):
        audio.normalize_format("ogg")


def test_bad_bitrate_is_rejected() -> None:
    with pytest.raises(audio.AudioEncodeError, match="Bitrate"):
        audio.encode([0.0, 0.1], 24000, "mp3", bitrate="; rm -rf /")


def test_wav_stays_wav_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *args, **kwargs: None)
    assert audio.mp3_supported() is False
    assert [fmt.key for fmt in audio.available_formats()] == ["wav"]
    assert audio.default_download_format() == "wav"
    assert audio.encode([0.0, 0.5], 24000, "wav")[:4] == b"RIFF"


def test_wav_to_mp3_rejects_garbage() -> None:
    with pytest.raises(audio.AudioEncodeError, match="WAV"):
        audio.wav_to_mp3(b"kein audio")


# --------------------------------------------------------------- API
def test_info_lists_the_available_formats(client: TestClient) -> None:
    info = client.get("/api/info").json()
    keys = [entry["key"] for entry in info["audio_formats"]]
    assert "wav" in keys
    assert ("mp3" in keys) is audio.mp3_supported()
    assert info["default_download_format"] in keys


def test_tts_rejects_an_unknown_format(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "hi", "format": "ogg"})
    assert response.status_code == 422
    assert "ogg" in response.json()["detail"]


def test_tts_without_format_stays_wav(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "hi"})
    assert response.headers["content-type"] == "audio/wav"
    assert "omnivoice.wav" in response.headers["content-disposition"]


def test_mp3_needs_ffmpeg(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *args, **kwargs: None)
    response = client.post("/api/tts", json={"text": "hi", "format": "mp3"})
    assert response.status_code == 503
    assert "ffmpeg" in response.json()["detail"]


@needs_ffmpeg
def test_tts_can_answer_with_mp3(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "Hallo Welt", "format": "mp3"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert "omnivoice.mp3" in response.headers["content-disposition"]
    assert float(response.headers["x-omnivoice-duration-seconds"]) > 0.5
    # ID3-Tag oder direkt der erste Frame-Header.
    assert response.content[:3] == b"ID3" or response.content[0] == 0xFF


@needs_ffmpeg
def test_convert_turns_wav_into_a_smaller_mp3(client: TestClient) -> None:
    payload = _wav(client)
    response = client.post(
        "/api/convert",
        files={"audio": ("omnivoice.wav", payload, "audio/wav")},
        data={"format": "mp3"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert 0 < len(response.content) < len(payload)


def test_convert_to_wav_hands_the_input_back(client: TestClient) -> None:
    payload = _wav(client)
    response = client.post(
        "/api/convert",
        files={"audio": ("omnivoice.wav", payload, "audio/wav")},
        data={"format": "wav"},
    )
    assert response.status_code == 200
    assert response.content == payload
    with wave.open(io.BytesIO(response.content)) as handle:
        assert handle.getframerate() == 24000


def test_convert_rejects_what_is_not_a_wav(client: TestClient) -> None:
    response = client.post(
        "/api/convert",
        files={"audio": ("kaputt.wav", b"kein audio", "audio/wav")},
        data={"format": "mp3"},
    )
    assert response.status_code in (422, 503)


def test_convert_without_a_file_is_422(client: TestClient) -> None:
    response = client.post("/api/convert", data={"format": "mp3"})
    assert response.status_code == 422


def test_convert_honours_the_size_limit() -> None:
    settings = Settings(engine="dummy", max_convert_bytes=1024)
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/convert",
            files={"audio": ("gross.wav", b"\0" * 4096, "audio/wav")},
            data={"format": "mp3"},
        )
    assert response.status_code == 413


# ----------------------------------------------------------- Ruleset
def test_main_ruleset_allows_auto_merge() -> None:
    """Auto-Merge braucht Pflicht-PR und Checks, aber kein Review."""
    root = pathlib.Path(__file__).resolve().parents[2]
    data = json.loads(
        (root / ".github/rulesets/main-auto-merge.json").read_text(encoding="utf-8")
    )
    assert data["target"] == "branch"
    assert data["enforcement"] == "active"
    assert data["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]

    rules = {rule["type"]: rule.get("parameters", {}) for rule in data["rules"]}
    assert rules["pull_request"]["required_approving_review_count"] == 0
    assert rules["pull_request"]["require_last_push_approval"] is False
    assert "squash" in rules["pull_request"]["allowed_merge_methods"]

    checks = {
        entry["context"]
        for entry in rules["required_status_checks"]["required_status_checks"]
    }
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for context in checks:
        assert context in workflow, context
