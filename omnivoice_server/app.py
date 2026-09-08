"""FastAPI application: small web UI + JSON/REST API for OmniVoice."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .engine import BaseEngine, SynthesisError, SynthesisRequest, build_engine
from .timing import DurationForecaster, RequestFeatures
from .voice_design import VOICE_DESIGN_CATEGORIES
from .wav import encode_wav

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_ALLOWED_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".m4a",
    ".webm",
    ".opus",
    ".aac",
}


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422, detail=f"Ungültige Zahl: {value!r}"
        ) from None


def _as_int(value: Any, default: int) -> int:
    return int(round(_as_float(value, float(default))))


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def create_app(
    settings: Settings | None = None,
    engine: BaseEngine | None = None,
    load_on_startup: bool = True,
    forecaster: DurationForecaster | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or build_engine(settings)
    forecaster = forecaster or DurationForecaster(
        path=settings.timing_history_path,
        max_samples=settings.timing_history_size,
    )

    def _remember_duration(request: SynthesisRequest, seconds: float) -> None:
        forecaster.record(
            RequestFeatures.from_request(request), seconds, engine.env_key
        )

    engine.observer = _remember_duration

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if load_on_startup:
            # Loading happens in a background thread so the UI is reachable
            # immediately and can show download/loading progress.
            engine.load_in_background()
        yield

    app = FastAPI(
        title="OmniVoice Docker",
        description="Kleine Weboberfläche und REST-API für OmniVoice TTS.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.forecaster = forecaster

    # -- pages -----------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/app.js", include_in_schema=False)
    def app_js() -> FileResponse:
        return FileResponse(
            os.path.join(STATIC_DIR, "app.js"), media_type="text/javascript"
        )

    @app.get("/style.css", include_in_schema=False)
    def app_css() -> FileResponse:
        return FileResponse(
            os.path.join(STATIC_DIR, "style.css"), media_type="text/css"
        )

    # -- status ----------------------------------------------------------
    @app.get("/api/health")
    def health() -> JSONResponse:
        status = engine.status
        payload = {
            "status": "ok" if status.state == "ready" else status.state,
            "engine": engine.name,
            "model": status.as_dict(),
        }
        # 503 while loading keeps `docker compose` healthchecks and reverse
        # proxies from sending traffic to a model that cannot answer yet.
        code = 200 if status.state == "ready" else 503
        return JSONResponse(payload, status_code=code)

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "engine": engine.name,
            "model": engine.status.as_dict(),
            "sampling_rate": engine.sampling_rate,
            "limits": {
                "max_text_chars": settings.max_text_chars,
                "max_ref_audio_bytes": settings.max_ref_audio_bytes,
            },
            "asr_enabled": settings.load_asr,
            "asr_model": settings.asr_model if settings.load_asr else None,
            "voice_design": VOICE_DESIGN_CATEGORIES,
            "timing_history": forecaster.history(engine.env_key),
        }

    @app.get("/api/languages")
    def languages() -> dict[str, Any]:
        names = engine.languages()
        return {"count": len(names), "languages": names}

    # -- duration forecast -----------------------------------------------
    @app.get("/api/estimate")
    def estimate(
        text_chars: int = 0,
        num_step: int = 32,
        guidance_scale: float = 2.0,
        speed: float = 1.0,
        duration: float | None = None,
        mode: str = "auto",
    ) -> dict[str, Any]:
        """How long a generation with these settings is expected to take.

        The forecast is learned from previous runs on this machine, so it is
        empty (``estimate_seconds: null``) until the first job has finished.
        """
        features = RequestFeatures(
            text_chars=max(0, text_chars),
            num_step=num_step,
            guidance_scale=guidance_scale,
            speed=speed,
            duration=duration,
            mode=mode,
        )
        prediction = forecaster.estimate(features, engine.env_key)
        payload: dict[str, Any] = {
            "estimate_seconds": None,
            "low_seconds": None,
            "high_seconds": None,
            "samples": 0,
            "based_on": None,
            "audio_seconds": round(features.audio_seconds, 1),
        }
        if prediction is not None:
            payload.update(prediction.as_dict())
        payload["history"] = forecaster.history(engine.env_key)
        return payload

    # -- synthesis -------------------------------------------------------
    @app.post("/api/tts")
    async def tts(request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        ref_bytes: bytes | None = None
        ref_suffix = ".wav"

        if content_type.startswith("application/json"):
            data: dict[str, Any] = await request.json()
        else:
            form = await request.form()
            data = {k: v for k, v in form.items()}
            upload = form.get("ref_audio")
            if upload is not None and hasattr(upload, "read"):
                data.pop("ref_audio", None)
                too_large = HTTPException(
                    status_code=413,
                    detail="Referenz-Audio ist zu groß "
                    f"(max. {settings.max_ref_audio_bytes // (1024 * 1024)} MB).",
                )
                if (upload.size or 0) > settings.max_ref_audio_bytes:
                    raise too_large
                suffix = os.path.splitext(upload.filename or "")[1].lower()
                if suffix in _ALLOWED_AUDIO_SUFFIXES:
                    ref_suffix = suffix
                ref_bytes = await upload.read()
                if len(ref_bytes) > settings.max_ref_audio_bytes:
                    raise too_large

        text = _clean(data.get("text"))
        if not text:
            raise HTTPException(status_code=422, detail="Bitte einen Text angeben.")
        if len(text) > settings.max_text_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Text ist zu lang (max. {settings.max_text_chars} Zeichen).",
            )

        mode = (_clean(data.get("mode")) or "auto").lower()
        if mode not in ("auto", "clone", "design"):
            raise HTTPException(status_code=422, detail=f"Unbekannter Modus: {mode}")

        language = _clean(data.get("language"))
        if language and language.lower() in ("auto", "automatisch"):
            language = None

        # Auto mode means "let the model pick a voice", so any leftover
        # instruct from the UI is dropped.
        instruct = None if mode == "auto" else _clean(data.get("instruct"))

        duration = data.get("duration")
        duration_value = _as_float(duration, 0.0) if _clean(duration) else 0.0

        req = SynthesisRequest(
            text=text,
            mode=mode,
            language=language,
            instruct=instruct,
            ref_text=_clean(data.get("ref_text")),
            num_step=_as_int(data.get("num_step"), 32),
            guidance_scale=_as_float(data.get("guidance_scale"), 2.0),
            speed=_as_float(data.get("speed"), 1.0),
            duration=duration_value or None,
            denoise=_as_bool(data.get("denoise"), True),
            normalize_text=_as_bool(data.get("normalize_text"), False),
        )

        tmp_path = None
        try:
            if ref_bytes:
                fd, tmp_path = tempfile.mkstemp(suffix=ref_suffix, prefix="omnivoice-")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(ref_bytes)
                req.ref_audio_path = tmp_path

            try:
                # Generation blocks for seconds to minutes on a CPU; keeping it
                # off the event loop lets /api/health and the UI stay responsive.
                started = time.perf_counter()
                samples = await run_in_threadpool(engine.synthesize, req)
                elapsed = time.perf_counter() - started
            except SynthesisError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - reported to the caller
                logger.exception("Synthese fehlgeschlagen")
                raise HTTPException(
                    status_code=500, detail=f"{type(exc).__name__}: {exc}"
                ) from exc
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        wav_bytes = encode_wav(samples, engine.sampling_rate)
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={
                "Content-Disposition": 'attachment; filename="omnivoice.wav"',
                "X-OmniVoice-Sampling-Rate": str(engine.sampling_rate),
                "X-OmniVoice-Duration-Seconds": (
                    f"{len(samples) / engine.sampling_rate:.2f}"
                ),
                "X-OmniVoice-Generation-Seconds": f"{elapsed:.2f}",
            },
        )

    return app


def get_app() -> FastAPI:
    """Entry point for `uvicorn omnivoice_server.app:get_app --factory`."""
    return create_app()
