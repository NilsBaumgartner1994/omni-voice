"""Tests for the duration forecast (no model weights needed)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from omnivoice_server.app import create_app
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine, SynthesisRequest
from omnivoice_server.timing import DurationForecaster, RequestFeatures


def _features(**kwargs) -> RequestFeatures:
    return RequestFeatures(**{"text_chars": 150, **kwargs})


# -- work model ----------------------------------------------------------
def test_work_scales_with_text_steps_and_guidance() -> None:
    base = _features(num_step=32, guidance_scale=2.0)
    assert _features(text_chars=300).work == pytest.approx(base.work * 2)
    assert _features(num_step=64).work == pytest.approx(base.work * 2)
    assert _features(guidance_scale=0.0).work == pytest.approx(base.work / 2)
    # A fixed length wins over the text-length approximation.
    assert _features(duration=10.0).audio_seconds == 10.0
    # Faster speech means less audio for the same text.
    assert _features(speed=2.0).work == pytest.approx(base.work / 2)


def test_empty_request_still_has_positive_work() -> None:
    assert _features(text_chars=0, num_step=0, speed=0).work > 0


# -- forecaster ----------------------------------------------------------
def test_no_history_means_no_estimate() -> None:
    assert DurationForecaster().estimate(_features()) is None


def test_estimate_scales_a_single_previous_run() -> None:
    """The user's case: 60 s for input X predicts the runtime of input Y."""
    forecaster = DurationForecaster()
    forecaster.record(_features(text_chars=150), 60.0)

    same = forecaster.estimate(_features(text_chars=150))
    assert same is not None
    assert same.seconds == pytest.approx(60.0)
    assert same.samples == 1

    # Half the text, half the diffusion steps -> a quarter of the time.
    faster = forecaster.estimate(_features(text_chars=75, num_step=16))
    assert faster.seconds == pytest.approx(15.0)


def test_median_ignores_a_single_outlier() -> None:
    forecaster = DurationForecaster()
    for seconds in (60.0, 62.0, 600.0, 58.0, 61.0):
        forecaster.record(_features(), seconds)
    estimate = forecaster.estimate(_features())
    assert 55.0 < estimate.seconds < 70.0
    assert estimate.low_seconds <= estimate.seconds <= estimate.high_seconds


def test_history_is_per_machine_setup() -> None:
    forecaster = DurationForecaster()
    forecaster.record(_features(), 60.0, env="dummy|cpu")
    assert forecaster.estimate(_features(), env="dummy|cpu") is not None
    assert forecaster.estimate(_features(), env="omnivoice|cuda") is None


def test_modes_are_pooled_until_the_mode_has_its_own_runs() -> None:
    forecaster = DurationForecaster()
    forecaster.record(_features(mode="auto"), 60.0)
    borrowed = forecaster.estimate(_features(mode="clone"))
    assert borrowed.based_on == "all"

    for _ in range(2):
        forecaster.record(_features(mode="clone"), 90.0)
    own = forecaster.estimate(_features(mode="clone"))
    assert own.based_on == "mode"
    assert own.seconds == pytest.approx(90.0)


def test_history_is_capped() -> None:
    forecaster = DurationForecaster(max_samples=2)
    for seconds in (10.0, 20.0, 30.0):
        forecaster.record(_features(), seconds)
    assert forecaster.count == 2
    assert forecaster.estimate(_features()).seconds == pytest.approx(25.0)


def test_history_survives_a_restart(tmp_path) -> None:
    path = str(tmp_path / "sub" / "timings.json")
    first = DurationForecaster(path=path)
    first.record(_features(), 42.0, env="dummy|cpu")

    second = DurationForecaster(path=path)
    assert second.count == 1
    assert second.estimate(_features(), env="dummy|cpu").seconds == pytest.approx(42.0)
    assert second.history("dummy|cpu")["last_seconds"] == 42.0


def test_broken_history_file_is_ignored(tmp_path) -> None:
    path = tmp_path / "timings.json"
    path.write_text("{not json", encoding="utf-8")
    assert DurationForecaster(path=str(path)).count == 0

    path.write_text(json.dumps({"version": 99, "samples": []}), encoding="utf-8")
    assert DurationForecaster(path=str(path)).count == 0


def test_unwritable_path_keeps_forecasting_in_memory(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    forecaster = DurationForecaster(path=str(blocker / "timings.json"))
    forecaster.record(_features(), 30.0)
    assert forecaster.estimate(_features()).seconds == pytest.approx(30.0)
    assert forecaster.history()["persistent"] is False


# -- engine + API --------------------------------------------------------
def test_engine_reports_its_generation_time() -> None:
    engine = DummyEngine(Settings(engine="dummy"))
    engine.load()
    seen: list[float] = []
    engine.observer = lambda request, seconds: seen.append(seconds)
    engine.synthesize(SynthesisRequest(text="Hallo"))
    assert len(seen) == 1 and seen[0] > 0
    assert engine.env_key == "dummy|dummy|cpu|float32"


def test_a_failing_observer_does_not_break_the_generation() -> None:
    engine = DummyEngine(Settings(engine="dummy"))
    engine.load()

    def boom(request, seconds):
        raise RuntimeError("kaputt")

    engine.observer = boom
    assert len(engine.synthesize(SynthesisRequest(text="Hallo"))) > 0


@pytest.fixture()
def client() -> TestClient:
    settings = Settings(engine="dummy")
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False)
    with TestClient(app) as test_client:
        yield test_client


def test_estimate_is_empty_before_the_first_run(client: TestClient) -> None:
    body = client.get("/api/estimate", params={"text_chars": 150}).json()
    assert body["estimate_seconds"] is None
    assert body["samples"] == 0
    assert body["audio_seconds"] == 10.0  # 150 chars / 15 chars per second
    assert body["history"]["count"] == 0


def test_a_generation_feeds_the_estimate(client: TestClient) -> None:
    text = "Hallo Welt. " * 20
    response = client.post("/api/tts", json={"text": text})
    assert float(response.headers["x-omnivoice-generation-seconds"]) > 0

    body = client.get("/api/estimate", params={"text_chars": len(text)}).json()
    assert body["estimate_seconds"] > 0
    assert body["samples"] == 1
    assert body["history"]["count"] == 1

    # Twice the text is expected to take about twice as long. The API rounds
    # to two decimals, which is up to 0.01 s off on the sub-second runtimes of
    # the dummy engine -- the exact scaling is checked on the raw estimate in
    # test_estimate_scales_a_single_previous_run.
    longer = client.get("/api/estimate", params={"text_chars": 2 * len(text)}).json()
    assert longer["estimate_seconds"] == pytest.approx(
        2 * body["estimate_seconds"], rel=0.05, abs=0.02
    )

    assert client.get("/api/info").json()["timing_history"]["count"] == 1


def test_estimate_rejects_nonsense_parameters(client: TestClient) -> None:
    response = client.get("/api/estimate", params={"num_step": "viele"})
    assert response.status_code == 422
