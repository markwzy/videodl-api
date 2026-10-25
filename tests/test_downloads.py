"""验证下载保留请求上下文、拒绝任意代理目标，并在结束后释放上游连接。"""

from collections import OrderedDict
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import requests

from app import downloads, main
from app.media import available_formats


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
        "formats": available_formats([{
            "download_url": MEDIA_URL, "width": 720, "height": 1280,
            "default_download_headers": {"User-Agent": "private-agent"},
        }], "douyin"),
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


def test_each_quality_download_keeps_its_own_headers(monkeypatch, client, wait_for_task):
    # 页面切换的是各档位凭证；从解析响应走到下载接口，验证地址和请求头不会串档。
    variants = available_formats([
        {"download_url": MEDIA_URL + "high", "width": 1080, "height": 1920,
         "default_download_headers": {"Referer": "https://www.douyin.com/", "User-Agent": "high-agent"}},
        {"download_url": MEDIA_URL + "low", "width": 720, "height": 1280,
         "default_download_headers": {"Referer": "https://www.douyin.com/", "User-Agent": "low-agent"}},
    ], "douyin")
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [main._result_item({
        "source": "DouyinBrowser", "title": "不同画质", "download_url": MEDIA_URL + "high",
        "default_download_headers": variants[0]["default_download_headers"], "formats": variants,
    })])
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return upstream_response(status=200)

    monkeypatch.setattr(downloads.requests, "get", get)
    response = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123"})
    assert response.status_code == 202
    assert "high-agent" not in response.text and "low-agent" not in response.text
    task = wait_for_task(client, response.json()["id"])
    assert "high-agent" not in str(task) and "low-agent" not in str(task)
    item = task["result"]["items"][0]
    assert item["local_download_url"] == item["formats"][0]["local_download_url"]
    paths = [format["local_download_url"] for format in item["formats"]]
    assert len(set(paths)) == 2
    for path in paths:
        assert client.get(path).content == b"data"
    assert [(url, headers["User-Agent"]) for url, headers in calls] == [
        (MEDIA_URL + "high", "high-agent"), (MEDIA_URL + "low", "low-agent"),
    ]
    assert all(headers["Referer"] == "https://www.douyin.com/" for _, headers in calls)


def test_get_parse_redirect_downloads_first_video_highest_quality(monkeypatch, client):
    variants = available_formats([
        {"download_url": MEDIA_URL + "low", "width": 720, "height": 1280,
         "default_download_headers": {"Referer": "https://www.douyin.com/", "User-Agent": "low-agent"}},
        {"download_url": MEDIA_URL + "high", "width": 1080, "height": 1920,
         "default_download_headers": {"Referer": "https://www.douyin.com/", "User-Agent": "high-agent"}},
    ], "douyin")
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [main._result_item({
        "source": "DouyinBrowser", "title": "快捷指令视频", "download_url": variants[0]["download_url"],
        "formats": variants,
    }), {"local_download_url": "/api/download/other"}])
    calls = []
    upstream = upstream_response(status=200)

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return upstream

    monkeypatch.setattr(downloads.requests, "get", get)
    response = client.get("/api/parse", params={
        "text": "分享视频 https://v.douyin.com/abc/", "redirect": "true",
    })
    assert len(response.history) == 1
    redirect = response.history[0]
    assert redirect.status_code == 303
    assert redirect.headers["location"].startswith("http://testserver/api/download/")
    assert redirect.headers["cache-control"] == "no-store"
    assert redirect.headers["referrer-policy"] == "no-referrer"
    assert response.status_code == 200 and response.content == b"data"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert calls[0][0] == MEDIA_URL + "high"
    assert calls[0][1]["User-Agent"] == "high-agent"
    assert calls[0][1]["Referer"] == "https://www.douyin.com/"
    assert upstream.closed
