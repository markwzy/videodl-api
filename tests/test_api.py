"""Offline tests: mock the upstream network-dependent parser."""
from fastapi.testclient import TestClient
from app import main

client = TestClient(main.app)


def test_url_extraction():
    assert main.extract_video_url("看这个！ https://v.douyin.com/abc123/ 复制链接") == "https://v.douyin.com/abc123/"
    assert main.extract_video_url("链接：https://www.xiaohongshu.com/explore/abc。") == "https://www.xiaohongshu.com/explore/abc"


def test_private_address_rejected():
    for url in ("http://127.0.0.1/", "http://192.168.1.1", "http://localhost:8000", "http://169.254.169.254/latest/meta-data/"):
        response = client.post("/api/parse", json={"text": url})
        assert response.status_code == 422


def test_parse_mock(monkeypatch):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [{
        "source": "DouyinVideoClient", "title": "示例", "identifier": "123",
        "cover_url": None, "download_url": "https://cdn.example.org/video.mp4",
        "audio_download_url": None, "ext": "mp4", "is_hls": False,
        "requires_merge": False, "may_require_headers": True,
    }])
    response = client.post("/api/parse", json={"text": "分享：https://www.douyin.com/video/123"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] and data["count"] == 1
    assert data["items"][0]["download_url"] == "https://cdn.example.org/video.mp4"


def test_health():
    assert client.get("/api/health").json()["status"] == "ok"


def test_empty_parse_is_failure(monkeypatch):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [])
    response = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123"})
    assert response.status_code == 502
    assert "未解析到有效媒体地址" in response.json()["detail"]
