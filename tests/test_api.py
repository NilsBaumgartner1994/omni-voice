"""API tests that run without model weights (dummy engine)."""

from __future__ import annotations

import io
import wave

import pytest
from fastapi.testclient import TestClient

from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine, OmniVoiceEngine, build_engine


@pytest.fixture()
def client() -> TestClient:
    settings = Settings(engine="dummy", load_asr=False, max_text_chars=200)
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


def _wav_seconds(payload: bytes) -> float:
    with wave.open(io.BytesIO(payload)) as handle:
        return handle.getnframes() / handle.getframerate()


def test_index_and_assets(client: TestClient) -> None:
    for path, marker in (
        ("/", "OmniVoice"),
        ("/app.js", "generate"),
        ("/style.css", "--accent"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text


def test_health_reports_ready(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model"]["state"] == "ready"


def test_health_is_503_while_loading() -> None:
    settings = Settings(engine="dummy")
    engine = DummyEngine(settings)  # never loaded
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        response = test_client.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "idle"


def test_info_and_languages(client: TestClient) -> None:
    info = client.get("/api/info").json()
    assert info["engine"] == "dummy"
    assert info["sampling_rate"] == 24000
    assert {c["key"] for c in info["voice_design"]} >= {"gender", "age", "pitch"}

    languages = client.get("/api/languages").json()
    assert languages["count"] == len(languages["languages"]) > 0


def test_tts_json_returns_wav(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "Hallo Welt", "speed": 1.0})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-omnivoice-sampling-rate"] == "24000"
    assert _wav_seconds(response.content) > 0.5
    assert response.content[:4] == b"RIFF"


def test_tts_form_with_reference_audio(client: TestClient) -> None:
    reference = client.post("/api/tts", json={"text": "Referenz"}).content
    response = client.post(
        "/api/tts",
        data={"text": "Geklont", "mode": "clone", "ref_text": "Referenz"},
        files={"ref_audio": ("ref.wav", reference, "audio/wav")},
    )
    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"


def test_duration_overrides_speed(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "Kurz", "duration": 3})
    assert 2.9 < _wav_seconds(response.content) < 3.1


@pytest.mark.parametrize(
    "payload,status",
    [
        ({"text": "   "}, 422),
        ({"text": "x" * 500}, 413),
        ({"text": "hi", "mode": "unknown"}, 422),
        ({"text": "hi", "speed": "schnell"}, 422),
    ],
)
def test_input_validation(client: TestClient, payload: dict, status: int) -> None:
    assert client.post("/api/tts", json=payload).status_code == status


def test_clone_without_reference_is_rejected(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "hi", "mode": "clone"})
    assert response.status_code == 409
    assert "Referenz" in response.json()["detail"]


def test_engine_selection() -> None:
    assert isinstance(build_engine(Settings(engine="dummy")), DummyEngine)
    assert isinstance(build_engine(Settings(engine="omnivoice")), OmniVoiceEngine)


def test_dtype_defaults_per_device() -> None:
    torch = pytest.importorskip("torch")
    resolve = OmniVoiceEngine._resolve_dtype
    assert resolve(None, "cpu") is torch.float32
    assert resolve(None, "cuda") is torch.float16
    assert resolve("bfloat16", "cpu") is torch.bfloat16
    with pytest.raises(ValueError):
        resolve("float8", "cpu")


def test_design_without_attributes_is_rejected(client: TestClient) -> None:
    response = client.post("/api/tts", json={"text": "hi", "mode": "design"})
    assert response.status_code == 409
    assert "instruct" in response.json()["detail"]


def test_numpy_arrays_are_encoded(client: TestClient) -> None:
    numpy = pytest.importorskip("numpy")
    from omnivoice_server.wav import encode_wav

    payload = encode_wav(numpy.array([0.0, 0.5, -0.5, 2.0], dtype="float32"), 24000)
    with wave.open(io.BytesIO(payload)) as handle:
        assert handle.getnframes() == 4
        assert handle.getframerate() == 24000
        frames = handle.readframes(4)
    assert frames[-2:] == (32767).to_bytes(2, "little")  # clipped, not wrapped


def test_health_stays_responsive_during_generation() -> None:
    """Synthesis must not block the event loop (minutes-long CPU runs)."""
    import threading
    import time

    from omnivoice_server.engine import DummyEngine

    class SlowEngine(DummyEngine):
        def _synthesize(self, request):
            time.sleep(1.0)
            return super()._synthesize(request)

    settings = Settings(engine="dummy")
    engine = SlowEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)

    with TestClient(app) as test_client:
        done = threading.Event()

        def _generate():
            test_client.post("/api/tts", json={"text": "lange Synthese"})
            done.set()

        worker = threading.Thread(target=_generate)
        worker.start()
        time.sleep(0.2)  # make sure the generation is in flight
        started = time.monotonic()
        response = test_client.get("/api/health")
        elapsed = time.monotonic() - started
        worker.join(timeout=10)

    assert done.is_set()
    assert response.status_code == 200
    assert elapsed < 0.5, f"/api/health war {elapsed:.2f}s blockiert"


def test_oversized_reference_audio_is_rejected() -> None:
    settings = Settings(engine="dummy", max_ref_audio_bytes=1024)
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/tts",
            data={"text": "hi", "mode": "clone", "ref_text": "hi"},
            files={"ref_audio": ("big.wav", b"\0" * 4096, "audio/wav")},
        )
    assert response.status_code == 413
