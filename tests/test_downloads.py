"""验证下载保留请求上下文、拒绝任意代理目标，并在结束后释放上游连接。"""

from collections import OrderedDict
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import requests

from app import downloads, main


client = TestClient(main.app)
MEDIA_URL = "https://v26-web.douyinvod.com/video/example/"


@pytest.fixture(autouse=True)
def isolated_tickets(monkeypatch):
    monkeypatch.setattr(downloads, "_tickets", OrderedDict())


def upstream_response(status=206, content_type="video/mp4", **extra_headers):
    response = SimpleNamespace(
        status_code=status, closed=False,
        headers={"Content-Type": content_type, **extra_headers},
    )

    def close():
        response.closed = True

    response.close = close
    response.iter_content = lambda chunk_size: iter([b"data"])
    return response


def test_download_preserves_context_range_and_filename(monkeypatch):
    upstream = upstream_response(**{"Content-Range": "bytes 0-3/100", "Content-Length": "4"})
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return upstream

    monkeypatch.setattr(downloads.requests, "get", get)
    path = downloads.create_download(MEDIA_URL, "目标视频", {"Referer": "https://www.douyin.com/", "User-Agent": "original-browser"})
    response = client.get(path, headers={"Range": "bytes=0-3", "If-Range": '"etag"'})
    assert response.status_code == 206
    assert response.content == b"data"
    assert response.headers["content-range"] == "bytes 0-3/100"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert calls[0][1]["headers"]["Referer"] == "https://www.douyin.com/"
    assert calls[0][1]["headers"]["User-Agent"] == "original-browser"
    assert calls[0][1]["headers"]["Range"] == "bytes=0-3"
    assert calls[0][1]["headers"]["If-Range"] == '"etag"'
    assert calls[0][1]["stream"] is True
    assert upstream.closed


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/video.mp4", "https://example.org/video.mp4",
    "https://v26-web.douyinvod.com.evil.example/video", "https://user:pass@v26-web.douyinvod.com/video",
])
def test_ticket_rejects_untrusted_targets(url):
    assert downloads.create_download(url, "video", {}) is None


def test_expired_or_unknown_ticket_requires_reparse(monkeypatch):
    monkeypatch.setattr(downloads.time, "monotonic", lambda: 0)
    path = downloads.create_download(MEDIA_URL, "video", {})
    monkeypatch.setattr(downloads.time, "monotonic", lambda: downloads.TICKET_TTL + 1)
    assert client.get(path).status_code == 410
    assert client.get("/api/download/unknown").status_code == 410


def test_ticket_store_is_bounded(monkeypatch):
    monkeypatch.setattr(downloads, "MAX_TICKETS", 2)
    oldest = downloads.create_download(MEDIA_URL, "one", {})
    downloads.create_download(MEDIA_URL, "two", {})
    downloads.create_download(MEDIA_URL, "three", {})
    assert len(downloads._tickets) == 2
    assert client.get(oldest).status_code == 410


def test_private_redirect_is_rejected_and_closed(monkeypatch):
    upstream = upstream_response(status=302, Location="http://127.0.0.1/internal")
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return upstream

    monkeypatch.setattr(downloads.requests, "get", get)
    response = client.get(downloads.create_download(MEDIA_URL, "video", {}))
    assert response.status_code == 502
    assert calls == [MEDIA_URL]
    assert upstream.closed


@pytest.mark.parametrize("status,content_type", [(403, "text/html"), (200, "text/html")])
def test_upstream_error_is_not_returned_as_video(monkeypatch, status, content_type):
    upstream = upstream_response(status=status, content_type=content_type)
    monkeypatch.setattr(downloads.requests, "get", lambda *args, **kwargs: upstream)
    response = client.get(downloads.create_download(MEDIA_URL, "video", {}))
    assert response.status_code == 502
    assert upstream.closed


def test_invalid_range_does_not_contact_upstream(monkeypatch):
    monkeypatch.setattr(downloads.requests, "get", lambda *args, **kwargs: pytest.fail("范围无效时不应访问上游"))
    response = client.get(downloads.create_download(MEDIA_URL, "video", {}), headers={"Range": "bytes=0-3,5-8"})
    assert response.status_code == 416


def test_upstream_timeout_returns_gateway_timeout(monkeypatch):
    def get(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(downloads.requests, "get", get)
    assert client.get(downloads.create_download(MEDIA_URL, "video", {})).status_code == 504


def test_parse_result_only_exposes_ticket():
    item = main._result_item({
        "source": "DouyinBrowser", "title": "video", "download_url": MEDIA_URL,
        "default_download_headers": {"Referer": "https://www.douyin.com/", "User-Agent": "private-agent"},
    })
    assert item["local_download_url"].startswith("/api/download/")
    assert "default_download_headers" not in item
    assert "private-agent" not in str(item)


def test_x_download_uses_same_streaming_path(monkeypatch):
    upstream = upstream_response(**{"Content-Length": "4"})
    monkeypatch.setattr(downloads.requests, "get", lambda *args, **kwargs: upstream)
    path = downloads.create_download("https://video.twimg.com/video.mp4", "X video", {"Referer": "https://x.com/"})
    response = client.get(path, headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"data"
    assert upstream.closed


def test_redirect_cannot_cross_supported_platforms(monkeypatch):
    upstream = upstream_response(status=302, Location="https://video.twimg.com/video.mp4")
    monkeypatch.setattr(downloads.requests, "get", lambda *args, **kwargs: upstream)
    response = client.get(downloads.create_download(MEDIA_URL, "video", {}))
    assert response.status_code == 502
    assert upstream.closed
