"""使用真实媒体工具验证合成后的画面、声音及断连回收，不访问外部网络。"""

import json
from io import BytesIO
import subprocess
from threading import BoundedSemaphore

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import downloads, main, mux


class MediaResponse:
    def __init__(self, data, content_type="video/mp4", status=200):
        self.raw = BytesIO(data)
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        while chunk := self.raw.read(chunk_size):
            yield chunk

    def close(self):
        self.closed = True


@pytest.fixture
def media():
    def generate(source, codec):
        return subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source,
            "-t", "0.4", *codec, "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
        ])
    return generate("color=c=blue:size=64x64:rate=5", ["-c:v", "libx264"]), generate("sine=frequency=440", ["-c:a", "aac"])


def test_download_produces_mp4_with_both_tracks_and_ignores_byte_range(monkeypatch, media):
    video, audio = MediaResponse(media[0]), MediaResponse(media[1], "audio/mp4")
    calls = []

    def open_media(ticket, range_header, if_range):
        calls.append((ticket, range_header, if_range))
        return audio if ticket.url.endswith("audio") else video

    monkeypatch.setattr(downloads, "_open_media", open_media)
    path = downloads.create_download("https://rr1.googlevideo.com/video", "title", {"User-Agent": "video"},
                                     audio_url="https://rr2.googlevideo.com/audio", audio_headers={"User-Agent": "audio"})
    response = TestClient(main.app).get(path, headers={"Range": "bytes=0-10"})
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "none" and "content-length" not in response.headers
    assert video.closed and audio.closed
    assert [call[0].headers["User-Agent"] for call in calls] == ["video", "audio"]
    assert all(call[1:] == (None, None) for call in calls)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", "pipe:0"], input=response.content, capture_output=True, check=True)
    assert {stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]} == {"video", "audio"}


def test_disconnect_terminates_process_and_cleanup_is_idempotent(media):
    video, audio = MediaResponse(media[0]), MediaResponse(media[1], "audio/mp4")
    stream = mux.MuxStream(video, audio)
    assert next(stream)
    stream.close()
    stream.close()
    assert stream._process.poll() is not None
    assert video.closed and audio.closed


def test_full_capacity_closes_inputs(monkeypatch):
    monkeypatch.setattr(mux, "_slots", BoundedSemaphore(0))
    video, audio = MediaResponse(b""), MediaResponse(b"")
    with pytest.raises(HTTPException) as error:
        mux.MuxStream(video, audio)
    assert error.value.status_code == 503
    assert video.closed and audio.closed


def test_failed_start_releases_capacity_and_inputs(monkeypatch):
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(mux, "_slots", slots)

    def fail(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(mux.subprocess, "Popen", fail)
    video, audio = MediaResponse(b""), MediaResponse(b"")
    with pytest.raises(HTTPException):
        mux.MuxStream(video, audio)
    assert video.closed and audio.closed
    assert slots.acquire(blocking=False)


def test_invalid_audio_closes_both_inputs_before_success_response(monkeypatch):
    video, audio = MediaResponse(b"video"), MediaResponse(b"login", "text/html")
    monkeypatch.setattr(downloads, "_open_media", lambda ticket, *args: audio if ticket.url.endswith("audio") else video)
    path = downloads.create_download("https://rr1.googlevideo.com/video", "title", {}, audio_url="https://rr1.googlevideo.com/audio")
    response = TestClient(main.app).get(path)
    assert response.status_code == 502
    assert video.closed and audio.closed
