"""Launch the *original* upstream Gradio demo inside the container.

``omnivoice-demo`` hard-codes ``dtype=torch.float16``, which is unusable on a
CPU-only laptop. This wrapper reuses upstream's ``build_demo()`` UI unchanged
but loads the model through :class:`OmniVoiceEngine`, which picks a device and
dtype that actually work on the current machine.

Start it with ``docker compose run --rm --service-ports omnivoice gradio``.
"""

from __future__ import annotations

import logging
import os

from .config import Settings
from .engine import OmniVoiceEngine

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()

    engine = OmniVoiceEngine(settings)
    engine.load()
    if engine.status.state != "ready":
        logger.error("Modell konnte nicht geladen werden: %s", engine.status.detail)
        return 1

    from omnivoice.cli.demo import build_demo

    demo = build_demo(engine.model, settings.model)
    demo.queue().launch(
        server_name=settings.host,
        server_port=settings.port,
        root_path=os.environ.get("OMNIVOICE_ROOT_PATH") or None,
        share=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
