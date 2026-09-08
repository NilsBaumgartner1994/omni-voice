#!/usr/bin/env python3
"""Download all model weights into the cache volume before the first start.

No Hugging Face token is required: every repository used here is public.
Set ``HF_ENDPOINT`` (e.g. ``https://hf-mirror.com``) if huggingface.co is slow
or blocked in your network.
"""

from __future__ import annotations

import argparse
import os
import sys


def _human(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _directory_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                total += os.stat(full, follow_symlinks=False).st_size
            except OSError:
                pass
    return total


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "t", "yes", "y", "on")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get("OMNIVOICE_MODEL", "k2-fsa/OmniVoice"),
        help="Haupt-Modell (Standard: k2-fsa/OmniVoice).",
    )
    parser.add_argument(
        "--asr-model",
        default=os.environ.get("OMNIVOICE_ASR_MODEL", "openai/whisper-large-v3-turbo"),
        help="Whisper-Modell für die automatische Transkription.",
    )
    parser.add_argument(
        "--with-asr",
        action="store_true",
        default=_env_bool("OMNIVOICE_LOAD_ASR"),
        help="Zusätzlich das Whisper-ASR-Modell laden (mehrere GB).",
    )
    args = parser.parse_args(argv)

    from huggingface_hub import snapshot_download

    endpoint = os.environ.get("HF_ENDPOINT")
    print(f"Cache: {os.environ.get('HF_HOME', '~/.cache/huggingface')}")
    if endpoint:
        print(f"Endpoint: {endpoint}")
    print("Es wird kein Hugging-Face-Token benötigt (alle Repos sind öffentlich).\n")

    print(f"[1/3] {args.model} ...")
    model_path = snapshot_download(args.model)
    print(f"      -> {model_path} ({_human(_directory_size(model_path))})")

    # The audio tokenizer usually ships inside the main repo; older/other
    # checkpoints fall back to the standalone repository, mirroring
    # OmniVoice.from_pretrained().
    if os.path.isdir(os.path.join(model_path, "audio_tokenizer")):
        print("[2/3] Audio-Tokenizer ist im Hauptmodell enthalten.")
    else:
        print("[2/3] eustlb/higgs-audio-v2-tokenizer ...")
        tokenizer_path = snapshot_download("eustlb/higgs-audio-v2-tokenizer")
        print(f"      -> {tokenizer_path} ({_human(_directory_size(tokenizer_path))})")

    if args.with_asr:
        print(f"[3/3] {args.asr_model} ...")
        asr_path = snapshot_download(args.asr_model)
        print(f"      -> {asr_path} ({_human(_directory_size(asr_path))})")
    else:
        print(
            "[3/3] ASR übersprungen (OMNIVOICE_LOAD_ASR=false). "
            "Für automatische Transkription der Referenzstimme: --with-asr."
        )

    print("\nFertig. Der Container startet ab jetzt ohne weitere Downloads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
