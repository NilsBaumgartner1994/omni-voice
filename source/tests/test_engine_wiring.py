"""Wiring tests for the parts that need torch/omnivoice at runtime.

The real packages are multi-gigabyte downloads, so they are stubbed here: the
point is to verify *our* glue code (device/dtype choice, kwargs handed to
``generate()``, reuse of the upstream Gradio UI), not upstream itself.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from omnivoice_server.config import Settings
from omnivoice_server.engine import OmniVoiceEngine, SynthesisRequest


class _FakeArray(list):
    def tolist(self):
        return list(self)


class _FakeModel:
    sampling_rate = 24000

    def __init__(self, **kwargs):
        self.load_kwargs = kwargs
        self.generate_kwargs: dict[str, Any] = {}

    def generate(self, **kwargs):
        self.generate_kwargs = kwargs
        return [_FakeArray([0.0, 0.1, -0.1])]

    def create_voice_clone_prompt(self, ref_audio, ref_text=None):
        return {"ref_audio": ref_audio, "ref_text": ref_text}


@pytest.fixture()
def fake_omnivoice(monkeypatch: pytest.MonkeyPatch):
    torch = types.ModuleType("torch")
    torch.float16 = "float16"
    torch.bfloat16 = "bfloat16"
    torch.float32 = "float32"

    created = {}

    class _OmniVoice:
        @staticmethod
        def from_pretrained(name, **kwargs):
            model = _FakeModel(name=name, **kwargs)
            created["model"] = model
            return model

    class _GenConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    omnivoice = types.ModuleType("omnivoice")
    omnivoice.OmniVoice = _OmniVoice
    omnivoice.OmniVoiceGenerationConfig = _GenConfig

    common = types.ModuleType("omnivoice.utils.common")
    common.get_best_device = lambda: "cpu"

    utils = types.ModuleType("omnivoice.utils")
    utils.common = common

    lang_map = types.ModuleType("omnivoice.utils.lang_map")
    lang_map.LANG_NAMES = ["german", "english"]
    lang_map.lang_display_name = str.title

    for name, module in {
        "torch": torch,
        "omnivoice": omnivoice,
        "omnivoice.utils": utils,
        "omnivoice.utils.common": common,
        "omnivoice.utils.lang_map": lang_map,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    return created


def test_cpu_loads_in_float32(fake_omnivoice) -> None:
    engine = OmniVoiceEngine(Settings(engine="omnivoice"))
    engine.load()
    assert engine.status.state == "ready", engine.status.detail
    assert engine.status.device == "cpu"
    # float16 on CPU is the upstream demo's default and is unusably slow.
    assert engine.status.dtype == "float32"
    assert engine.model.load_kwargs["dtype"] == "float32"
    assert engine.model.load_kwargs["device_map"] == "cpu"
    assert engine.languages() == ["English", "German"]


def test_explicit_device_and_dtype(fake_omnivoice) -> None:
    engine = OmniVoiceEngine(Settings(engine="omnivoice", device="cuda", dtype="bf16"))
    engine.load()
    assert engine.model.load_kwargs["device_map"] == "cuda"
    assert engine.model.load_kwargs["dtype"] == "bfloat16"


def test_generate_kwargs(fake_omnivoice) -> None:
    engine = OmniVoiceEngine(Settings(engine="omnivoice"))
    engine.load()

    engine.synthesize(SynthesisRequest(text="Hallo", speed=1.2, language="German"))
    kwargs = engine.model.generate_kwargs
    assert kwargs["text"] == "Hallo"
    assert kwargs["speed"] == 1.2
    assert kwargs["language"] == "German"
    assert "duration" not in kwargs

    # duration wins over speed, mirroring upstream's own precedence
    engine.synthesize(SynthesisRequest(text="Hallo", speed=1.2, duration=5.0))
    assert engine.model.generate_kwargs["duration"] == 5.0
    assert "speed" not in engine.model.generate_kwargs


def test_clone_builds_prompt(fake_omnivoice) -> None:
    engine = OmniVoiceEngine(Settings(engine="omnivoice"))
    engine.load()
    engine.synthesize(
        SynthesisRequest(
            text="Hallo",
            mode="clone",
            ref_audio_path="/tmp/ref.wav",
            ref_text="Referenz",
        )
    )
    prompt = engine.model.generate_kwargs["voice_clone_prompt"]
    assert prompt == {"ref_audio": "/tmp/ref.wav", "ref_text": "Referenz"}


def test_gradio_wrapper_reuses_upstream_ui(fake_omnivoice, monkeypatch) -> None:
    launched = {}

    class _Demo:
        def queue(self):
            return self

        def launch(self, **kwargs):
            launched.update(kwargs)

    def _build_demo(model, checkpoint):
        launched["model"] = model
        launched["checkpoint"] = checkpoint
        return _Demo()

    demo_module = types.ModuleType("omnivoice.cli.demo")
    demo_module.build_demo = _build_demo
    cli_module = types.ModuleType("omnivoice.cli")
    cli_module.demo = demo_module
    monkeypatch.setitem(sys.modules, "omnivoice.cli", cli_module)
    monkeypatch.setitem(sys.modules, "omnivoice.cli.demo", demo_module)

    from omnivoice_server import gradio_app

    assert gradio_app.main([]) == 0
    assert launched["server_port"] == 7860
    assert launched["share"] is False
    assert isinstance(launched["model"], _FakeModel)
    assert launched["checkpoint"] == "k2-fsa/OmniVoice"
