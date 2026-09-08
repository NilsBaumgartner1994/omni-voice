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

from .audio import (
    API_DEFAULT_FORMAT,
    FORMATS,
    AudioEncodeError,
    available_formats,
    default_download_format,
    encode,
    mp3_supported,
    normalize_format,
    wav_to_mp3,
)
from .config import Settings, default_library_dir, default_youtube_cache_dir
from .engine import BaseEngine, SynthesisError, SynthesisRequest, build_engine
from .i18n import catalogue as i18n_catalogue
from .i18n import resolve_locale
from .imagesearch import ImageSearch, ImageSearchError, browser_search_urls
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
from .youtube import (
    YouTubeError,
    YouTubeService,
    YouTubeUnavailable,
    transcript_between,
    video_id_from_url,
    watch_url,
)

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


def _audio_format(value: Any, default: str = API_DEFAULT_FORMAT) -> str:
    """Gewünschtes Ausgabeformat prüfen (unbekannt -> 422, kein ffmpeg -> 503)."""
    try:
        key = normalize_format(_clean(value), default)
    except AudioEncodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if key == "mp3" and not mp3_supported():
        raise HTTPException(
            status_code=503,
            detail="MP3 ist auf diesem Server nicht verfügbar (ffmpeg fehlt).",
        )
    return key


def create_app(
    settings: Settings | None = None,
    engine: BaseEngine | None = None,
    load_on_startup: bool = True,
    forecaster: DurationForecaster | None = None,
    library: VoiceLibrary | None = None,
    youtube: YouTubeService | None = None,
    images: ImageSearch | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or build_engine(settings)
    library = library or VoiceLibrary(
        settings.library_dir or default_library_dir(),
        max_audio_bytes=settings.max_ref_audio_bytes,
        max_image_bytes=settings.max_image_bytes,
    )
    youtube = youtube or YouTubeService(
        settings.youtube_cache_dir or default_youtube_cache_dir(),
        max_video_seconds=settings.youtube_max_video_seconds,
        max_clip_seconds=settings.youtube_max_clip_seconds,
        cache_entries=settings.youtube_cache_entries,
        timeout_seconds=settings.youtube_timeout_seconds,
        binary=settings.ytdlp_binary,
        mp3_bitrate=settings.mp3_bitrate,
        enabled=settings.youtube_enabled,
    )
    images = images or ImageSearch(
        enabled=settings.image_search_enabled,
        language=settings.image_search_language,
        max_bytes=settings.max_image_bytes,
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
    app.state.youtube = youtube
    app.state.images = images

    # -- pages -----------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.get("/app.js", include_in_schema=False)
    def app_js() -> FileResponse:
        return FileResponse(
            os.path.join(STATIC_DIR, "app.js"), media_type="text/javascript"
        )

    @app.get("/i18n.js", include_in_schema=False)
    def i18n_js() -> FileResponse:
        return FileResponse(
            os.path.join(STATIC_DIR, "i18n.js"), media_type="text/javascript"
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
            # Welche Formate ausgeliefert werden können (MP3 nur mit ffmpeg)
            # und was das Download-Menü vorauswählt.
            "audio_formats": [fmt.as_dict() for fmt in available_formats()],
            "default_download_format": default_download_format(),
            "timing_history": forecaster.history(engine.env_key),
            # Schlüssel der geladenen Gewichte: ändert er sich, müssen die
            # gespeicherten Stimmen neu berechnet werden.
            "voice_model_key": voices.model_key,
            "library_dir": library.root,
            # Kann dieser Server eine Referenzaufnahme aus einem YouTube-Video
            # holen, und darf er Bilder zum Namen suchen?
            "youtube": youtube.availability(),
            "image_search": images.availability(),
        }

    @app.get("/api/languages")
    def languages() -> dict[str, Any]:
        names = engine.languages()
        return {"count": len(names), "languages": names}

    # -- Oberflächensprache ----------------------------------------------
    @app.get("/api/i18n")
    def i18n(request: Request, locale: str | None = None) -> dict[str, Any]:
        """Texte und Klangeffekte in der gewünschten Sprache.

        Ohne ``?locale=`` entscheidet der ``Accept-Language``-Header des
        Browsers, sonst Deutsch. Die Klangeffekte kommen alphabetisch nach
        ihrer Übersetzung zurück, damit die Oberfläche sie nur noch anzeigen
        muss.
        """

        wanted = resolve_locale(locale, request.headers.get("accept-language"))
        return i18n_catalogue(wanted)

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

        # Ohne `format` bleibt es bei WAV -- die Oberfläche holt sich das
        # MP3 später über /api/convert, statt neu zu synthetisieren.
        audio_format = _audio_format(data.get("format"))

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

        spec = FORMATS[audio_format]
        try:
            payload = await run_in_threadpool(
                encode,
                samples,
                engine.sampling_rate,
                audio_format,
                bitrate=settings.mp3_bitrate,
            )
        except AudioEncodeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(
            content=payload,
            media_type=spec.media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="omnivoice{spec.suffix}"'
                ),
                "X-OmniVoice-Sampling-Rate": str(engine.sampling_rate),
                "X-OmniVoice-Duration-Seconds": (
                    f"{len(samples) / engine.sampling_rate:.2f}"
                ),
                "X-OmniVoice-Generation-Seconds": f"{elapsed:.2f}",
            },
        )

    # -- Formatwechsel ----------------------------------------------------
    @app.post("/api/convert")
    async def convert(request: Request) -> Response:
        """Fertiges WAV in ein anderes Format umrechnen.

        Die Oberfläche erzeugt immer WAV und wandelt erst beim Herunterladen
        um: eine zweite Synthese wäre teuer und käme auch nicht wieder
        genauso heraus.
        """
        form = await request.form()
        upload = form.get("audio")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=422, detail="Bitte eine WAV-Datei mitschicken."
            )
        too_large = HTTPException(
            status_code=413,
            detail="Das Audio ist zu groß "
            f"(max. {settings.max_convert_bytes // (1024 * 1024)} MB).",
        )
        if (upload.size or 0) > settings.max_convert_bytes:
            raise too_large
        payload = await upload.read()
        if len(payload) > settings.max_convert_bytes:
            raise too_large
        if not payload:
            raise HTTPException(status_code=422, detail="Die Datei ist leer.")

        target = _audio_format(form.get("format"), default="mp3")
        spec = FORMATS[target]
        if target != "wav":
            try:
                payload = await run_in_threadpool(
                    wav_to_mp3, payload, bitrate=settings.mp3_bitrate
                )
            except AudioEncodeError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(
            content=payload,
            media_type=spec.media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="omnivoice{spec.suffix}"'
                )
            },
        )

    # -- Referenz aus einem YouTube-Video --------------------------------
    async def _body(request: Request) -> dict[str, Any]:
        """JSON oder Formular -- die Oberfläche schickt Formulare, curl JSON."""
        if request.headers.get("content-type", "").startswith("application/json"):
            data = await request.json()
            return data if isinstance(data, dict) else {}
        return {k: v for k, v in (await request.form()).items()}

    def _youtube_error(exc: YouTubeError) -> HTTPException:
        # Fehlt yt-dlp, liegt es am Server (503) -- sonst am Link (422).
        code = 503 if isinstance(exc, YouTubeUnavailable) else 422
        return HTTPException(status_code=code, detail=str(exc))

    @app.post("/api/youtube/fetch")
    async def youtube_fetch(request: Request) -> dict[str, Any]:
        """Tonspur und Untertitel eines Videos holen (und zwischenspeichern).

        Antwortet mit Stammdaten, dem Transkript samt Zeitmarken und der
        Adresse, unter der die Tonspur zum Anhören bereitliegt -- damit lassen
        sich Start und Ende wählen, bevor irgendetwas gespeichert wird.
        """
        data = await _body(request)
        url = _clean(data.get("url"))
        if not url:
            raise HTTPException(status_code=422, detail="Bitte einen Link angeben.")
        try:
            video = await run_in_threadpool(
                youtube.fetch, url, refresh=_as_bool(data.get("refresh"), False)
            )
        except YouTubeError as exc:
            raise _youtube_error(exc) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Der Zwischenspeicher streikt: {exc}"
            ) from exc
        payload = video.as_dict()
        payload["audio_url"] = f"/api/youtube/{video.id}/audio"
        payload["max_clip_seconds"] = youtube.max_clip_seconds
        return payload

    @app.get("/api/youtube/{video_id}/audio")
    def youtube_audio(video_id: str) -> FileResponse:
        """Die geladene Tonspur -- Grundlage für Anhören und Zuschneiden."""
        try:
            path = youtube.audio_path(video_id)
        except YouTubeError as exc:
            raise _youtube_error(exc) from exc
        if not path:
            raise HTTPException(
                status_code=404,
                detail="Dieses Video liegt nicht (mehr) bereit. Bitte den Link "
                "noch einmal laden.",
            )
        return FileResponse(path, media_type="audio/mpeg")

    @app.get("/api/youtube/{video_id}/transcript")
    def youtube_transcript(
        video_id: str, start: float = 0.0, end: float = 0.0
    ) -> dict[str, Any]:
        """Das Transkript des gewählten Ausschnitts."""
        try:
            video = youtube.cached(video_id)
        except YouTubeError as exc:
            raise _youtube_error(exc) from exc
        if video is None:
            raise HTTPException(
                status_code=404, detail="Dieses Video liegt nicht (mehr) bereit."
            )
        span_end = end if end > start else video.duration
        return {
            "id": video.id,
            "start": round(start, 2),
            "end": round(span_end, 2),
            "kind": video.transcript_kind,
            "language": video.transcript_language,
            "text": transcript_between(video.transcript, start, span_end),
        }

    # -- Bild zum Namen suchen -------------------------------------------
    @app.get("/api/image-search")
    async def image_search(q: str = "", limit: int = 8) -> dict[str, Any]:
        """Portraits zu einem Namen -- Wikipedia und Wikimedia Commons.

        Geliefert werden nur Vorschläge; heruntergeladen wird erst das eine
        Bild, das beim Speichern der Person als ``image_url`` mitkommt.
        """
        query = _clean(q) or ""
        if not query:
            raise HTTPException(
                status_code=422, detail="Bitte einen Namen zum Suchen angeben."
            )
        state = images.availability()
        if not state["enabled"]:
            raise HTTPException(status_code=503, detail=state["reason"])
        try:
            hits = await run_in_threadpool(images.search, query, limit)
        except ImageSearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "query": query,
            "count": len(hits),
            "results": [hit.as_dict() for hit in hits],
            # Findet Wikipedia nichts, hilft die Suche im Browser weiter.
            "browser_search": browser_search_urls(query),
        }

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

    async def _youtube_reference(
        form: Any,
    ) -> tuple[Upload | None, dict[str, Any] | None, str]:
        """Aus Link und Zeitmarken die Referenzaufnahme schneiden.

        Zurück kommen die MP3-Daten, die Herkunftsangabe für ``voice.json``
        und das Transkript des Ausschnitts (leer, wenn das Video keins hat).
        """
        url = _clean(form.get("youtube_url"))
        video_id = _clean(form.get("youtube_video_id"))
        if not url and not video_id:
            return None, None, ""
        start = _as_float(form.get("youtube_start"), 0.0)
        end = _as_float(form.get("youtube_end"), 0.0)
        if end <= start:
            raise HTTPException(
                status_code=422,
                detail="Bitte Start- und Endzeit des Ausschnitts angeben.",
            )
        try:
            if not video_id:
                video_id = video_id_from_url(url or "")
            data = await run_in_threadpool(youtube.clip, video_id, start, end, url=url)
            video = youtube.cached(video_id)
        except YouTubeError as exc:
            raise _youtube_error(exc) from exc
        except AudioEncodeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        source = {
            "kind": "youtube",
            "url": url or watch_url(video_id),
            "video_id": video_id,
            "start": round(start, 2),
            "end": round(end, 2),
            "title": video.title if video else "",
            "uploader": video.uploader if video else "",
        }
        transcript = ""
        if video and video.transcript:
            transcript = transcript_between(video.transcript, start, end)
            source["transcript_kind"] = video.transcript_kind
        return (
            Upload(filename="reference.mp3", data=data, media_type="audio/mpeg"),
            source,
            transcript,
        )

    async def _image_from_search(form: Any) -> Upload | None:
        """Ein in der Bildersuche ausgewähltes Bild holen."""
        url = _clean(form.get("image_url"))
        if not url:
            return None
        state = images.availability()
        if not state["enabled"]:
            raise HTTPException(status_code=503, detail=state["reason"])
        try:
            data, filename, media_type = await run_in_threadpool(images.download, url)
        except ImageSearchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if len(data) > settings.max_image_bytes:
            raise HTTPException(
                status_code=413,
                detail="Das Bild ist zu groß (max. "
                f"{settings.max_image_bytes // (1024 * 1024)} MB).",
            )
        return Upload(filename=filename, data=data, media_type=media_type)

    async def _prepare_quietly(voice: Voice, *, force: bool = False) -> dict[str, Any]:
        """Stimme berechnen, ohne dass ein Fehler den Aufruf umwirft.

        Beim Anlegen und beim Durchrechnen der ganzen Bibliothek zählt, dass
        die Person erhalten bleibt: schlägt die Berechnung fehl (Modell lädt
        noch, Aufnahme unbrauchbar), steht der Grund in ``error`` und
        „Vorbereiten“ holt es später nach.
        """
        try:
            return await run_in_threadpool(voices.prepare, voice, force=force)
        except (SynthesisError, FileNotFoundError, OSError) as exc:
            return {"prepared": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - eine kaputte Stimme
            logger.exception("Stimme %s konnte nicht vorbereitet werden", voice.id)
            return {"prepared": False, "error": f"{type(exc).__name__}: {exc}"}

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
        # Ohne hochgeladene Datei darf ein YouTube-Ausschnitt einspringen.
        source = None
        transcript = ""
        if audio is None:
            audio, source, transcript = await _youtube_reference(form)
        if audio is None:
            raise HTTPException(
                status_code=422,
                detail="Bitte ein Referenz-Audio hochladen oder einen "
                "YouTube-Link mit Start- und Endzeit angeben.",
            )
        image = await _upload(
            form, "image", limit=settings.max_image_bytes, label="Das Bild"
        )
        if image is None:
            image = await _image_from_search(form)
        try:
            voice = library.create(
                name=_clean(form.get("name")) or "",
                audio=audio,
                # Ohne eigenen Referenztext übernimmt das Video-Transkript.
                ref_text=_clean(form.get("ref_text")) or transcript,
                description=_clean(form.get("description")) or "",
                language=_clean(form.get("language")),
                image=image,
                source=source,
            )
        except LibraryError as exc:
            raise _library_error(exc) from exc
        # Eine frisch angelegte Person ist erst mit berechneter Stimme sofort
        # benutzbar, deshalb wird sie standardmäßig gleich vorbereitet.
        # `prepare=false` überspringt das (z. B. für Stapel-Importe).
        payload = voices.describe(voice)
        if _as_bool(form.get("prepare"), True):
            payload.update(await _prepare_quietly(voice))
        return payload

    @app.post("/api/voices/prepare-all")
    async def prepare_all_voices(force: bool = False) -> dict[str, Any]:
        """Alle Stimmen für die aktuell geladenen Gewichte durchrechnen."""
        results = []
        for voice in library.list():
            entry: dict[str, Any] = {"id": voice.id, "name": voice.name}
            entry.update(await _prepare_quietly(voice, force=force))
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
        source = None
        transcript = ""
        if audio is None:
            audio, source, transcript = await _youtube_reference(form)
        image = await _upload(
            form, "image", limit=settings.max_image_bytes, label="Das Bild"
        )
        if image is None:
            image = await _image_from_search(form)
        fields: dict[str, Any] = {}
        for field in ("name", "ref_text", "description", "language"):
            if field in form:
                fields[field] = str(form.get(field) or "")
        if transcript and not _clean(fields.get("ref_text")):
            fields["ref_text"] = transcript
        try:
            voice = library.update(
                voice_id,
                audio=audio,
                image=image,
                remove_image=_as_bool(form.get("remove_image"), False),
                source=source,
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
