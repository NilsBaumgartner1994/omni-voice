"""The entrypoint must not hand empty environment variables to the tools.

Docker Compose turns `HF_ENDPOINT: ${HF_ENDPOINT:-}` into a *set but empty*
variable inside the container. ``huggingface_hub`` resolves its endpoint with
``os.getenv("HF_ENDPOINT", "https://huggingface.co")``, so the empty value wins
over the default, every hub URL loses its scheme and the model download fails
with ``UnsupportedProtocol: Request URL is missing an 'http://' or 'https://'
protocol.``
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("env") is None,
    reason="bash/env are only guaranteed inside the container image",
)


def _entrypoint_env(**overrides: str) -> dict[str, str]:
    """Run the entrypoint with `env` as command and read back its environment."""
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "env"],
        env={"PATH": "/usr/bin:/bin", **overrides},
        capture_output=True,
        text=True,
        check=True,
    )
    seen: dict[str, str] = {}
    for line in result.stdout.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            seen[name] = value
    return seen


def test_empty_optional_variables_are_dropped() -> None:
    seen = _entrypoint_env(
        HF_ENDPOINT="",
        HF_HUB_OFFLINE="",
        OMP_NUM_THREADS="",
        OMNIVOICE_DTYPE="",
    )
    assert "HF_ENDPOINT" not in seen
    assert "HF_HUB_OFFLINE" not in seen
    assert "OMP_NUM_THREADS" not in seen
    assert "OMNIVOICE_DTYPE" not in seen


def test_configured_values_are_kept() -> None:
    seen = _entrypoint_env(
        HF_ENDPOINT="https://hf-mirror.com",
        HF_HUB_OFFLINE="1",
        OMP_NUM_THREADS="4",
        OMNIVOICE_MODEL="k2-fsa/OmniVoice",
    )
    assert seen["HF_ENDPOINT"] == "https://hf-mirror.com"
    assert seen["HF_HUB_OFFLINE"] == "1"
    assert seen["OMP_NUM_THREADS"] == "4"
    assert seen["OMNIVOICE_MODEL"] == "k2-fsa/OmniVoice"
