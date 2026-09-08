"""``python -m omnivoice_server`` -- start the web server."""

from __future__ import annotations

import argparse
import logging

from .app import create_app
from .config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omnivoice_server",
        description="Kleine Weboberfläche und REST-API für OmniVoice.",
    )
    parser.add_argument("--host", default=None, help="Bind-Adresse (Standard: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Port (Standard: 7860)")
    parser.add_argument(
        "--engine",
        choices=("omnivoice", "dummy"),
        default=None,
        help="'dummy' erzeugt nur einen Testton (keine Modell-Downloads).",
    )
    parser.add_argument("--model", default=None, help="Modellpfad oder HF-Repo-ID.")
    parser.add_argument("--device", default=None, help="cpu | cuda | mps | xpu")
    parser.add_argument("--dtype", default=None, help="float32 | float16 | bfloat16")
    parser.add_argument("--log-level", default="info", help="uvicorn/Python Log-Level.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings.from_env()
    for field_name in ("host", "port", "engine", "model", "device", "dtype"):
        value = getattr(args, field_name)
        if value is not None:
            setattr(settings, field_name, value)

    import uvicorn

    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        # Model loading and generation can take a while on a laptop CPU.
        timeout_keep_alive=75,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
