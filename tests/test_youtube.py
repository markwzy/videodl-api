"""验证油管只提取目标视频、分离音轨绑定及下载凭证的端到端边界。"""

import pytest
from fastapi import HTTPException
from yt_dlp.utils import DownloadError

from app import downloads, main, youtube
from app.parse_cache import canonical_url


URL = "https://youtu.be/8lNWd7PvvCU?si=share"
VIDEO = "https://rr1.googlevideo.com/videoplayback?id=video"
AUDIO = "https://rr2.googlevideo.com/videoplayback?id=audio"


def entry():
    return {
        "id": "8lNWd7PvvCU", "title": "测试视频", "thumbnail": "https://i.ytimg.com/cover.jpg",
        "http_headers": {"User-Agent": "media-agent", "Cookie": "account-secret"},
        "formats": [
            {"url": VIDEO, "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "width": 1920, "height": 1080, "filesize": 100},
            {"url": AUDIO, "protocol": "https", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
             "http_headers": {"User-Agent": "audio-agent", "Authorization": "audio-secret"}},
            {"url": VIDEO + "low", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a.40.2", "width": 640, "height": 360, "filesize": 50},
            {"url": "https://127.0.0.1/video.mp4", "protocol": "https", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
            {"url": VIDEO + "hls", "protocol": "m3u8_native", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a"},
        ],
    }


def test_only_complete_downloads_are_offered_and_credentials_stay_private():
    result = main._result_item(youtube._video_item(entry()))
    assert [f["height"] for f in result["formats"]] == [1080, 360]
    assert result["formats"][0]["requires_merge"] is True
    assert result["formats"][0]["filesize"] is None
    assert result["formats"][1]["requires_merge"] is False
    assert result["formats"][1]["filesize"] == 50
    assert "secret" not in str(result) and "media-agent" not in str(result)
    ticket = downloads._get_ticket(result["local_download_url"].split("/")[-1])
    assert ticket.url == VIDEO and ticket.audio_url == AUDIO
    assert ticket.audio_headers == {"User-Agent": "audio-agent"}


def test_video_without_audio_is_not_offered_as_a_complete_download():
    info = entry()
    info["formats"] = info["formats"][:1]
    with pytest.raises(HTTPException, match="422"):
        youtube._video_item(info)


@pytest.mark.parametrize("flag", [{"is_live": True}, {"live_status": "is_upcoming"}])
def test_live_media_is_rejected(flag):
    with pytest.raises(HTTPException) as error:
        youtube._video_item({**entry(), **flag})
    assert error.value.status_code == 422


@pytest.mark.parametrize("url", [
    URL, "https://www.youtube.com/watch?v=8lNWd7PvvCU&list=playlist&t=42",
    "https://m.youtube.com/shorts/8lNWd7PvvCU", "https://www.youtube.com/embed/8lNWd7PvvCU",
])
def test_share_aliases_identify_one_video(url):
    assert canonical_url("youtube", url) == "https://www.youtube.com/watch?v=8lNWd7PvvCU"


def test_api_uses_dedicated_extractor_cache_and_binds_audio(monkeypatch, client, wait_for_task):
    calls = []

    class FakeClient:
        def __init__(self, options):
            assert options["noplaylist"] is True
            assert options["remote_components"] == []
            assert options["js_runtimes"] == {"node": {}}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, **kwargs):
            calls.append((url, kwargs))
            return entry()

    monkeypatch.setattr(youtube, "YoutubeDL", FakeClient)
    first = client.post("/api/parse", json={"text": URL}).json()
    task = wait_for_task(client, first["id"])
    assert task["status"] == "succeeded"
    item = task["result"]["items"][0]
    assert item["source"] == "YouTubeYtDlp" and item["local_download_url"]
    second = client.post("/api/parse", json={"text": "https://www.youtube.com/watch?v=8lNWd7PvvCU&list=test"}).json()
    assert wait_for_task(client, second["id"])["status"] == "succeeded"
    assert calls == [("https://www.youtube.com/watch?v=8lNWd7PvvCU", {"download": False, "ie_key": "Youtube"})]


@pytest.mark.parametrize("message,status", [("Sign in to confirm", 403), ("Video unavailable", 422), ("network failed", 502)])
def test_errors_do_not_fall_back_to_page_scanning(monkeypatch, message, status):
    def fail(url):
        raise DownloadError(message)

    monkeypatch.setattr(youtube, "_extract_youtube", fail)
    with pytest.raises(HTTPException) as error:
        main._parse_blocking(URL)
    assert error.value.status_code == status


def test_disabled_youtube_does_not_use_upstream(monkeypatch):
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    monkeypatch.setattr(youtube, "parse_youtube", lambda url: pytest.fail("关闭的平台不应执行解析"))
    assert main._parse_blocking(URL) == []


@pytest.mark.parametrize("audio", ["https://video.twimg.com/a.mp4", "https://rr1.googlevideo.com.evil.example/a", "https://127.0.0.1/a"])
def test_audio_cannot_expand_download_domain_boundary(audio):
    assert downloads.create_download(VIDEO, "title", {}, audio_url=audio) is None
