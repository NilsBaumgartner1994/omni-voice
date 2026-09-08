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

from .config import Settings, default_library_dir
from .engine import BaseEngine, SynthesisError, SynthesisRequest, build_engine
from .library import (
    ALLOWED_AUDIO_SUFFIXES,
    LibraryError,
    Upload,
    Voice,
    VoiceLibrary,
    VoiceNotFound,
)
from .timing import DurationForecaster, RequestFeatures
from .voice_design import VOICE_DESIGN_CATEGORIES
from .voices import VoiceService
from .wav import encode_wav

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


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
    library: VoiceLibrary | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or build_engine(settings)
    library = library or VoiceLibrary(
        settings.library_dir or default_library_dir(),
        max_audio_bytes=settings.max_ref_audio_bytes,
        max_image_bytes=settings.max_image_bytes,
    )
    voices = VoiceService(library, engine, cache_size=settings.voice_cache_size)
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
    app.state.library = library
    app.state.voices = voices

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
            # Schlüssel der geladenen Gewichte: ändert er sich, müssen die
            # gespeicherten Stimmen neu berechnet werden.
            "voice_model_key": voices.model_key,
            "library_dir": library.root,
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
                if suffix in ALLOWED_AUDIO_SUFFIXES:
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

        # Eine gespeicherte Person ersetzt den Upload: ihr Referenz-Audio liegt
        # schon in der Bibliothek, und die daraus berechnete Stimme womöglich
        # auch -- dann entfällt die Vorbereitung komplett.
        voice = None
        voice_id = _clean(data.get("voice_id"))
        if voice_id:
            voice = _voice_or_404(voice_id)
            mode = "clone"
            ref_bytes = None

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

        if voice is not None:
            req.ref_text = req.ref_text or voice.ref_text or None
            req.ref_audio_path = library.audio_path(voice.id)
            try:
                # Kann beim ersten Mal Sekunden bis Minuten dauern, deshalb
                # genau wie die Synthese neben dem Event-Loop.
                req.voice_artifact = await run_in_threadpool(voices.artifact, voice)
            except SynthesisError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except FileNotFoundError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    # -- Stimm-Bibliothek ------------------------------------------------
    def _voice_or_404(voice_id: str) -> Voice:
        try:
            return library.get(voice_id)
        except VoiceNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def _upload(form: Any, field: str, *, limit: int, label: str):
        item = form.get(field)
        if item is None or not hasattr(item, "read"):
            return None
        too_large = HTTPException(
            status_code=413,
            detail=f"{label} ist zu groß (max. {limit // (1024 * 1024)} MB).",
        )
        if (item.size or 0) > limit:
            raise too_large
        data = await item.read()
        if len(data) > limit:
            raise too_large
        if not data:
            return None
        return Upload(
            filename=item.filename or "",
            data=data,
            media_type=item.content_type or None,
        )

    def _library_error(exc: LibraryError) -> HTTPException:
        return HTTPException(status_code=422, detail=str(exc))

    @app.get("/api/voices")
    def list_voices() -> dict[str, Any]:
        entries = [voices.describe(voice) for voice in library.list()]
        return {
            "count": len(entries),
            "voices": entries,
            "model_key": voices.model_key,
            "library_dir": library.root,
        }

    @app.post("/api/voices", status_code=201)
    async def create_voice(request: Request) -> dict[str, Any]:
        form = await request.form()
        audio = await _upload(
            form,
            "ref_audio",
            limit=settings.max_ref_audio_bytes,
            label="Das Referenz-Audio",
        )
        if audio is None:
            raise HTTPException(
                status_code=422, detail="Bitte ein Referenz-Audio hochladen."
            )
        image = await _upload(
            form, "image", limit=settings.max_image_bytes, label="Das Bild"
        )
        try:
            voice = library.create(
                name=_clean(form.get("name")) or "",
                audio=audio,
                ref_text=_clean(form.get("ref_text")) or "",
                description=_clean(form.get("description")) or "",
                language=_clean(form.get("language")),
                image=image,
            )
        except LibraryError as exc:
            raise _library_error(exc) from exc
        return voices.describe(voice)

    @app.post("/api/voices/prepare-all")
    async def prepare_all_voices(force: bool = False) -> dict[str, Any]:
        """Alle Stimmen für die aktuell geladenen Gewichte durchrechnen."""
        results = []
        for voice in library.list():
            entry: dict[str, Any] = {"id": voice.id, "name": voice.name}
            try:
                entry.update(
                    await run_in_threadpool(voices.prepare, voice, force=force)
                )
            except (SynthesisError, FileNotFoundError, OSError) as exc:
                entry.update({"prepared": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - eine kaputte Stimme
                logger.exception("Stimme %s konnte nicht vorbereitet werden", voice.id)
                entry.update(
                    {"prepared": False, "error": f"{type(exc).__name__}: {exc}"}
                )
            results.append(entry)
        return {
            "model_key": voices.model_key,
            "count": len(results),
            "failed": sum(1 for entry in results if not entry.get("prepared")),
            "results": results,
        }

    @app.get("/api/voices/{voice_id}")
    def get_voice(voice_id: str) -> dict[str, Any]:
        return voices.describe(_voice_or_404(voice_id))

    @app.post("/api/voices/{voice_id}")
    async def update_voice(voice_id: str, request: Request) -> dict[str, Any]:
        _voice_or_404(voice_id)
        form = await request.form()
        audio = await _upload(
            form,
            "ref_audio",
            limit=settings.max_ref_audio_bytes,
            label="Das Referenz-Audio",
        )
        image = await _upload(
            form, "image", limit=settings.max_image_bytes, label="Das Bild"
        )
        fields: dict[str, Any] = {}
        for field in ("name", "ref_text", "description", "language"):
            if field in form:
                fields[field] = str(form.get(field) or "")
        try:
            voice = library.update(
                voice_id,
                audio=audio,
                image=image,
                remove_image=_as_bool(form.get("remove_image"), False),
                **fields,
            )
        except LibraryError as exc:
            raise _library_error(exc) from exc
        # Ein neues Referenz-Audio ergibt einen neuen Fingerabdruck; der alte
        # Eintrag im Zwischenspeicher passt dann nicht mehr.
        voices.forget(voice_id)
        return voices.describe(voice)

    @app.delete("/api/voices/{voice_id}")
    def delete_voice(voice_id: str) -> dict[str, Any]:
        _voice_or_404(voice_id)
        voices.forget(voice_id)
        library.delete(voice_id)
        return {"deleted": voice_id}

    @app.get("/api/voices/{voice_id}/audio")
    def voice_audio(voice_id: str) -> FileResponse:
        voice = _voice_or_404(voice_id)
        path = library.audio_path(voice_id)
        if not path:
            raise HTTPException(status_code=404, detail="Kein Referenz-Audio.")
        return FileResponse(path, media_type=voice.audio.media_type)

    @app.get("/api/voices/{voice_id}/image")
    def voice_image(voice_id: str) -> FileResponse:
        voice = _voice_or_404(voice_id)
        path = library.image_path(voice_id)
        if not path:
            raise HTTPException(status_code=404, detail="Kein Bild hinterlegt.")
        return FileResponse(path, media_type=voice.image.media_type)

    @app.post("/api/voices/{voice_id}/prepare")
    async def prepare_voice(voice_id: str, force: bool = False) -> dict[str, Any]:
        voice = _voice_or_404(voice_id)
        try:
            result = await run_in_threadpool(voices.prepare, voice, force=force)
        except SynthesisError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {**voices.describe(voice), **result}

    return app


def get_app() -> FastAPI:
    """Entry point for `uvicorn omnivoice_server.app:get_app --factory`."""
    return create_app()
