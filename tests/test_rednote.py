"""验证小红书目标笔记、分享签名和带请求头的下载链路。"""

from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from yt_dlp.utils import DownloadError

from app import downloads, main, rednote


NOTE_ID = "6adaf7cf000000002403fc30"
URL = f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=signature&xsec_source=pc_share"
MEDIA = "http://sns-video-v6.xhscdn.com/stream/video.mp4?sign=media-signature"


def entry():
    return {
        "id": NOTE_ID, "title": "新妆造也很俊！！", "thumbnail": "http://sns-webpic-qc.xhscdn.com/cover.jpg",
        "http_headers": {"User-Agent": "original-agent", "Cookie": "account-secret", "Authorization": "private-secret"},
        "formats": [
            {"url": MEDIA, "protocol": "http", "ext": "mp4", "width": 720, "height": 1280,
             "fps": 30, "tbr": 1413, "filesize": 4598338, "vcodec": "h264", "acodec": "aac"},
            {"url": MEDIA.replace("sns-video-v6", "sns-bak-v1"), "protocol": "http", "ext": "mp4",
             "width": 720, "height": 1280, "vcodec": "h264", "acodec": "aac"},
            {"url": MEDIA + "low", "protocol": "http", "ext": "mp4", "width": 540, "height": 960,
             "vcodec": "h264", "acodec": "aac", "http_headers": {"User-Agent": "low-agent"}},
        ],
    }


def test_share_parse_to_streaming_download(monkeypatch, client, wait_for_task):
    calls = []

    class FakeClient:
        def __init__(self, options):
            assert options["allowed_extractors"] == ["XiaoHongShu"]
            assert options["skip_download"] is True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, **kwargs):
            calls.append((url, kwargs))
            return entry()

    monkeypatch.setattr(rednote, "YoutubeDL", FakeClient)
    task = client.post("/api/parse", json={"text": f"28 【新妆造也很俊！！】 😆 分享 {URL}\u00a0"}).json()
    result = wait_for_task(client, task["id"])
    assert result["status"] == "succeeded"
    item = result["result"]["items"][0]
    assert item["source"] == "RednoteYtDlp"
    assert item["identifier"] == NOTE_ID
    assert [f["width"] for f in item["formats"]] == [720, 540]
    assert item["formats"][0]["bitrate"] == 1413000
    assert item["cover_url"].startswith("https://")
    assert item["download_url"] == MEDIA.replace("http://", "https://")
    assert item["local_download_url"] == item["formats"][0]["local_download_url"]
    assert "secret" not in str(item) and "original-agent" not in str(item)
    assert calls == [(URL, {"download": False, "ie_key": "XiaoHongShu"})]

    media_calls = []
    upstream = SimpleNamespace(status_code=206, headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-3/4598338"},
                               iter_content=lambda chunk_size: iter([b"data"]), close=lambda: None)

    def get(url, **kwargs):
        media_calls.append((url, kwargs))
        return upstream

    monkeypatch.setattr(downloads.requests, "get", get)
    response = client.get(item["local_download_url"], headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == b"data"
    assert media_calls[0][1]["headers"]["User-Agent"] == "original-agent"
    assert media_calls[0][1]["headers"]["Referer"] == "https://www.xiaohongshu.com/"
    assert "Cookie" not in media_calls[0][1]["headers"]
    assert media_calls[0][1]["headers"]["Range"] == "bytes=0-3"


def test_cache_is_copied_and_share_signatures_are_kept_separate(monkeypatch):
    calls = []
    class FakeClient:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, **kwargs):
            calls.append(url)
            return entry()

    monkeypatch.setattr(rednote, "YoutubeDL", FakeClient)
    rednote.parse_rednote(URL)[0]["formats"][0]["default_download_headers"]["User-Agent"] = "changed"
    assert rednote.parse_rednote(URL)[0]["formats"][0]["default_download_headers"]["User-Agent"] == "original-agent"
    other = URL.replace("signature", "other-signature")
    rednote.parse_rednote(other)
    assert calls == [URL, other]


def test_short_link_preserves_signature_and_closes_response(monkeypatch):
    calls, closed = [], []
    class Redirect:
        status_code = 302
        headers = {"Location": URL}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(True)
    monkeypatch.setattr(rednote.requests, "get", lambda url, **kwargs: calls.append((url, kwargs)) or Redirect())
    assert rednote._resolve_url("http://xhslink.com/a/share") == (URL, NOTE_ID)
    assert calls[0][0] == "https://xhslink.com/a/share"
    assert calls[0][1]["allow_redirects"] is False
    assert closed == [True]


@pytest.mark.parametrize("target", ["http://127.0.0.1/private", "https://www.xiaohongshu.com.evil.example/explore/" + NOTE_ID,
                                         "https://user:pass@www.xiaohongshu.com/explore/" + NOTE_ID])
def test_short_link_cannot_redirect_outside_official_hosts(monkeypatch, target):
    class Redirect:
        status_code = 302
        headers = {"Location": target}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    calls = []
    monkeypatch.setattr(rednote.requests, "get", lambda url, **kwargs: calls.append(url) or Redirect())
    with pytest.raises(HTTPException) as error:
        rednote._resolve_url("https://xhslink.com/a/share")
    assert error.value.status_code == 422
    assert calls == ["https://xhslink.com/a/share"]


@pytest.mark.parametrize("variant", [
    {"url": "https://sns-video-v6.xhscdn.com.evil.example/video.mp4"},
    {"url": "http://127.0.0.1/video.mp4"}, {"protocol": "m3u8_native"}, {"vcodec": "none"},
    {"acodec": "none"}, {"has_drm": True},
])
def test_invalid_or_incomplete_media_is_rejected(variant):
    info = entry()
    info["formats"] = [{**info["formats"][0], **variant}]
    with pytest.raises(HTTPException) as error:
        rednote._video_item(info, NOTE_ID)
    assert error.value.status_code == 422


def test_another_note_cannot_supply_the_download():
    with pytest.raises(HTTPException) as error:
        rednote._video_item({**entry(), "id": "another-note"}, NOTE_ID)
    assert error.value.status_code == 502


@pytest.mark.parametrize("message,status", [("403 login required", 403), ("No video formats found", 422), ("network failure", 502)])
def test_extractor_errors_do_not_use_page_scanning(monkeypatch, message, status):
    def fail(url):
        raise DownloadError(message)
    monkeypatch.setattr(rednote, "_extract_rednote", fail)
    with pytest.raises(HTTPException) as error:
        main._parse_blocking(URL)
    assert error.value.status_code == status


def test_disabled_rednote_is_not_sent_to_upstream(monkeypatch):
    monkeypatch.setenv("VIDEO_SOURCES", "RednoteVideoClient")
    assert main._parse_blocking("https://www.bilibili.com/video/BV123") == []
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    monkeypatch.setattr(rednote, "parse_rednote", lambda url: pytest.fail("关闭的平台不应访问网络"))
    assert main._parse_blocking(URL) == []


@pytest.mark.parametrize("host", ["sns-video-v6.xhscdn.com", "sns-bak-v1.xhscdn.com", "sns-video-hw.xhscdn.net"])
def test_rednote_media_hosts_are_supported(host):
    assert downloads.media_platform(f"https://{host}/stream/video.mp4") == "rednote"


@pytest.mark.parametrize("url", ["http://sns-video-v6.xhscdn.com/video.mp4", "https://sns-webpic-qc.xhscdn.com/image.jpg",
                                 "https://sns-video-v6.xhscdn.com.evil.example/video.mp4", "https://xhscdn.com/video.mp4"])
def test_other_targets_cannot_get_a_rednote_ticket(url):
    assert downloads.create_download(url, "video", {}) is None


def test_rednote_download_cannot_redirect_to_another_platform(monkeypatch, client):
    upstream = SimpleNamespace(status_code=302, headers={"Location": "https://video.twimg.com/video.mp4"}, close=lambda: None)
    calls = []
    monkeypatch.setattr(downloads.requests, "get", lambda url, **kwargs: calls.append(url) or upstream)
    path = downloads.create_download(MEDIA.replace("http://", "https://"), "video", {})
    assert client.get(path).status_code == 502
    assert len(calls) == 1
