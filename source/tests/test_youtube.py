"""YouTube als Quelle für Referenzaufnahmen -- ohne Netz und ohne yt-dlp."""

from __future__ import annotations

import io
import os
import sys
import wave

import pytest
from fastapi.testclient import TestClient

from omnivoice_server.app import create_app
from omnivoice_server.audio import mp3_supported, wav_to_mp3
from omnivoice_server.config import Settings
from omnivoice_server.engine import DummyEngine
from omnivoice_server.youtube import (
    YouTubeError,
    YouTubeService,
    parse_vtt,
    transcript_between,
    video_id_from_url,
)

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"

# So sehen die automatischen Untertitel von YouTube aus: jede Zeile kommt im
# nächsten Block noch einmal, dazu Zeitmarken mitten im Text.
VTT = """WEBVTT
Kind: captions
Language: de

00:00:01.000 --> 00:00:04.000 align:start position:0%
hallo und <00:00:02.500><c>herzlich willkommen</c>

00:00:04.000 --> 00:00:07.000 align:start position:0%
hallo und herzlich willkommen
mein name ist anna

00:00:07.000 --> 00:00:11.000
mein name ist anna
und das ist meine stimme
"""


def _wav(seconds: float = 4.0) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        frames = int(seconds * 16000)
        handle.writeframes((1000).to_bytes(2, "little", signed=True) * frames)
    return buffer.getvalue()


class FakeYouTube(YouTubeService):
    """yt-dlp durch Dateien ersetzen: derselbe Ablauf, nur ohne Download."""

    def __init__(self, cache_dir: str, **kwargs) -> None:
        super().__init__(cache_dir, **kwargs)
        self.downloads = 0
        self.duration = 300.0
        self.audio = b"ID3 kein echtes MP3"

    def command(self) -> list[str]:
        return ["yt-dlp"]

    def availability(self) -> dict:
        # ffmpeg gibt es auf dem Testrechner nicht unbedingt; hier zählt der
        # Ablauf ringsherum.
        return {
            "enabled": self.enabled,
            "reason": "" if self.enabled else "abgeschaltet",
            "max_video_seconds": self.max_video_seconds,
            "max_clip_seconds": self.max_clip_seconds,
        }

    def _probe(self, command: list[str], url: str) -> dict:
        return {
            "id": VIDEO_ID,
            "title": "Ein Interview",
            "duration": self.duration,
            "uploader": "Beispielkanal",
            "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
            "language": "de",
            "subtitles": {},
            "automatic_captions": {"en": [{}], "de": [{}]},
        }

    def _download(
        self, command: list[str], url: str, folder: str, language: str, kind: str
    ) -> None:
        self.downloads += 1
        with open(os.path.join(folder, "audio.mp3"), "wb") as handle:
            handle.write(self.audio)
        if language:
            with open(
                os.path.join(folder, f"audio.{language}.vtt"), "w", encoding="utf-8"
            ) as handle:
                handle.write(VTT)


class ClipStub(FakeYouTube):
    """Wie oben, schneidet aber ohne ffmpeg (merkt sich nur die Zeiten)."""

    def clip(self, video_id, start, end, *, url=None) -> bytes:
        self.clipped = (video_id, start, end)
        return b"ID3 geschnitten"


@pytest.fixture()
def service(tmp_path) -> FakeYouTube:
    return FakeYouTube(str(tmp_path / "youtube"), cache_entries=2)


def _client(tmp_path, youtube: YouTubeService, **kwargs) -> TestClient:
    settings = Settings(
        engine="dummy",
        load_asr=False,
        max_text_chars=200,
        library_dir=str(tmp_path / "voices"),
        **kwargs,
    )
    engine = DummyEngine(settings)
    engine.load()
    app = create_app(settings, engine=engine, load_on_startup=False, youtube=youtube)
    return TestClient(app)


# -- Links ------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        VIDEO_URL,
        f"{VIDEO_URL}&t=42s",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
    ],
)
def test_reads_the_video_id(url: str) -> None:
    assert video_id_from_url(url) == VIDEO_ID


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/123456",
        "file:///etc/passwd",
        "https://www.youtube.com/",
        "https://youtube.com.example.org/watch?v=abc",
        "",
    ],
)
def test_rejects_everything_else(url: str) -> None:
    with pytest.raises(YouTubeError):
        video_id_from_url(url)


# -- Transkript -------------------------------------------------------------
def test_vtt_drops_repeated_lines_and_tags() -> None:
    segments = parse_vtt(VTT)
    assert [segment.text for segment in segments] == [
        "hallo und herzlich willkommen",
        "mein name ist anna",
        "und das ist meine stimme",
    ]
    assert segments[0].start == 1.0


def test_transcript_takes_every_overlapping_part() -> None:
    segments = parse_vtt(VTT)
    assert transcript_between(segments, 4.5, 6.0) == "mein name ist anna"
    assert transcript_between(segments, 0.0, 5.0).startswith("hallo und")
    assert transcript_between(segments, 5.0, 5.0) == ""


# -- Laden und Zwischenspeicher ---------------------------------------------
def test_fetch_stores_audio_and_transcript(service: FakeYouTube) -> None:
    video = service.fetch(VIDEO_URL)
    assert video.id == VIDEO_ID
    assert video.title == "Ein Interview"
    assert video.transcript_kind == "auto"
    assert video.transcript_language == "de"
    assert len(video.transcript) == 3
    assert service.audio_path(VIDEO_ID)

    # Zweiter Aufruf nimmt das Geladene, `refresh` holt es neu.
    service.fetch(VIDEO_URL)
    assert service.downloads == 1
    service.fetch(VIDEO_URL, refresh=True)
    assert service.downloads == 2


def test_fetch_refuses_long_videos(service: FakeYouTube) -> None:
    service.duration = 10 * 3600
    with pytest.raises(YouTubeError, match="lang"):
        service.fetch(VIDEO_URL)
    assert service.cached(VIDEO_ID) is None


def test_cache_keeps_only_the_last_videos(tmp_path) -> None:
    service = FakeYouTube(str(tmp_path / "youtube"), cache_entries=1)
    service.fetch(VIDEO_URL)
    other = "abcdefghijk"
    service.fetch(f"https://youtu.be/{other}")
    entries = sorted(os.listdir(service.cache_dir))
    assert len(entries) == 1


def test_clip_checks_the_range(service: FakeYouTube) -> None:
    service.fetch(VIDEO_URL)
    with pytest.raises(YouTubeError, match="Endzeit"):
        service.clip(VIDEO_ID, 10.0, 10.0)
    with pytest.raises(YouTubeError, match="höchstens"):
        service.clip(VIDEO_ID, 0.0, service.max_clip_seconds + 5)


@pytest.mark.skipif(not mp3_supported(), reason="ffmpeg wird zum Schneiden gebraucht")
def test_clip_cuts_with_ffmpeg(tmp_path) -> None:
    service = FakeYouTube(str(tmp_path / "youtube"))
    service.audio = wav_to_mp3(_wav(6.0))
    service.fetch(VIDEO_URL)
    payload = service.clip(VIDEO_ID, 1.0, 3.0)
    assert payload and len(payload) < len(service.audio)


# Der Ablauf mit einem echten Programm: Aufruf, Ausgabe, abgelegte Dateien.
FAKE_YTDLP = """#!{python}
import json
import os
import sys

args = sys.argv[1:]
if "--dump-single-json" in args:
    print(json.dumps({{
        "id": "{video_id}",
        "title": "Ein Interview",
        "duration": 300,
        "uploader": "Beispielkanal",
        "language": "de",
        "subtitles": {{"de": [{{}}]}},
        "automatic_captions": {{"de": [{{}}]}},
    }}))
    sys.exit(0)

template = args[args.index("-o") + 1]
folder = os.path.dirname(template)
language = args[args.index("--sub-langs") + 1]
assert "--write-subs" in args, "eigene Untertitel haben Vorrang"
with open(os.path.join(folder, "audio.mp3"), "wb") as handle:
    handle.write(b"ID3 kein echtes MP3")
with open(os.path.join(folder, "audio." + language + ".vtt"), "w") as handle:
    handle.write({vtt!r})
"""


def test_runs_ytdlp_as_a_program(tmp_path) -> None:
    binary = tmp_path / "yt-dlp"
    binary.write_text(
        FAKE_YTDLP.format(python=sys.executable, video_id=VIDEO_ID, vtt=VTT),
        encoding="utf-8",
    )
    binary.chmod(0o755)

    class Service(YouTubeService):
        # ffmpeg gibt es auf dem Testrechner nicht unbedingt; hier zählt der
        # Aufruf von yt-dlp selbst.
        def availability(self) -> dict:
            return {"enabled": True, "reason": ""}

    service = Service(str(tmp_path / "youtube"), binary=str(binary))
    video = service.fetch(VIDEO_URL)
    assert video.title == "Ein Interview"
    assert video.transcript_kind == "manual"
    assert [segment.text for segment in video.transcript][0] == (
        "hallo und herzlich willkommen"
    )
    assert service.audio_path(VIDEO_ID)


def test_reports_what_ytdlp_complained_about(tmp_path) -> None:
    binary = tmp_path / "yt-dlp"
    binary.write_text(
        "#!/bin/sh\necho 'ERROR: Video unavailable' >&2\nexit 1\n", encoding="utf-8"
    )
    binary.chmod(0o755)

    class Service(YouTubeService):
        def availability(self) -> dict:
            return {"enabled": True, "reason": ""}

    service = Service(str(tmp_path / "youtube"), binary=str(binary))
    with pytest.raises(YouTubeError, match="Video unavailable"):
        service.fetch(VIDEO_URL)


def test_disabled_service_says_why(tmp_path) -> None:
    service = YouTubeService(str(tmp_path), enabled=False)
    assert service.availability()["enabled"] is False
    with pytest.raises(YouTubeError):
        service.fetch(VIDEO_URL)


# -- Web-API ----------------------------------------------------------------
def test_api_fetch_and_audio(tmp_path, service: FakeYouTube) -> None:
    with _client(tmp_path, service) as client:
        assert client.get("/api/info").json()["youtube"]["enabled"] is True

        answer = client.post("/api/youtube/fetch", json={"url": VIDEO_URL})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert body["audio_url"] == f"/api/youtube/{VIDEO_ID}/audio"
        assert body["transcript"][0]["text"] == "hallo und herzlich willkommen"

        audio = client.get(body["audio_url"])
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/mpeg"

        cut = client.get(f"/api/youtube/{VIDEO_ID}/transcript?start=4.5&end=6")
        assert cut.json()["text"] == "mein name ist anna"

        foreign = client.post("/api/youtube/fetch", json={"url": "https://x.example"})
        assert foreign.status_code == 422
        assert client.post("/api/youtube/fetch", json={}).status_code == 422
        assert client.get("/api/youtube/zzzzzzzzzzz/audio").status_code == 404


def test_api_reports_missing_ytdlp(tmp_path) -> None:
    service = YouTubeService(str(tmp_path / "youtube"), binary="/kein/yt-dlp")
    with _client(tmp_path, service) as client:
        assert client.get("/api/info").json()["youtube"]["enabled"] is False
        answer = client.post("/api/youtube/fetch", json={"url": VIDEO_URL})
        assert answer.status_code == 503


def test_person_from_a_youtube_clip(tmp_path) -> None:
    service = ClipStub(str(tmp_path / "youtube"))
    with _client(tmp_path, service) as client:
        client.post("/api/youtube/fetch", json={"url": VIDEO_URL})
        answer = client.post(
            "/api/voices",
            data={
                "name": "Anna Beispiel",
                "youtube_video_id": VIDEO_ID,
                "youtube_url": VIDEO_URL,
                "youtube_start": "4.5",
                "youtube_end": "6",
                "prepare": "false",
            },
        )
        assert answer.status_code == 201, answer.text
        voice = answer.json()
        assert voice["has_audio"] is True
        assert voice["audio"]["filename"] == "reference.mp3"
        # Ohne eigenen Referenztext kommt er aus dem Transkript.
        assert voice["ref_text"] == "mein name ist anna"
        assert voice["source"]["kind"] == "youtube"
        assert voice["source"]["video_id"] == VIDEO_ID
        assert voice["source"]["start"] == 4.5
        assert service.clipped == (VIDEO_ID, 4.5, 6.0)

        # Die Aufnahme liegt in der Bibliothek und wird ausgeliefert.
        audio = client.get(f"/api/voices/{voice['id']}/audio")
        assert audio.status_code == 200
        assert audio.content == b"ID3 geschnitten"


def test_person_without_audio_or_link_is_rejected(tmp_path) -> None:
    service = ClipStub(str(tmp_path / "youtube"))
    with _client(tmp_path, service) as client:
        assert client.post("/api/voices", data={"name": "Ohne"}).status_code == 422
        # Link ohne Zeitmarken: der Ausschnitt fehlt.
        answer = client.post(
            "/api/voices", data={"name": "Ohne", "youtube_url": VIDEO_URL}
        )
        assert answer.status_code == 422
        assert "Endzeit" in answer.json()["detail"]
