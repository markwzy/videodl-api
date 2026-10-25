"""Offline tests: mock the upstream network-dependent parser."""
from threading import Event

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app import main


def test_url_extraction():
    assert main.extract_video_url("看这个！ https://v.douyin.com/abc123/ 复制链接") == "https://v.douyin.com/abc123/"
    assert main.extract_video_url("链接：https://www.xiaohongshu.com/explore/abc。") == "https://www.xiaohongshu.com/explore/abc"


def test_private_address_rejected(client):
    for url in ("http://127.0.0.1/", "http://192.168.1.1", "http://localhost:8000", "http://169.254.169.254/latest/meta-data/"):
        response = client.post("/api/parse", json={"text": url})
        assert response.status_code == 422


def test_parse_mock(monkeypatch, client, wait_for_task):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [{
        "source": "DouyinVideoClient", "title": "示例", "identifier": "123",
        "cover_url": None, "download_url": "https://cdn.example.org/video.mp4",
        "audio_download_url": None, "ext": "mp4", "is_hls": False,
        "requires_merge": False, "may_require_headers": True,
    }])
    response = client.post("/api/parse", json={"text": "分享：https://www.douyin.com/video/123"})
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["location"] == f'/api/tasks/{response.json()["id"]}'
    task = wait_for_task(client, response.json()["id"])
    assert task["status"] == "succeeded"
    data = task["result"]
    assert data["success"] and data["count"] == 1
    assert data["items"][0]["download_url"] == "https://cdn.example.org/video.mp4"


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_empty_parse_is_failure(monkeypatch, client, wait_for_task):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [])
    response = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123"})
    assert response.status_code == 202
    task = wait_for_task(client, response.json()["id"])
    assert task["status"] == "failed"
    assert task["error"]["status_code"] == 502
    assert "未解析到有效媒体地址" in task["error"]["detail"]


def test_refresh_and_lost_submission_response_reuse_finished_task(monkeypatch, client, wait_for_task):
    calls = []
    monkeypatch.setattr(main, "_parse_blocking", lambda url: calls.append(url) or [{"download_url": "https://cdn.example.org/a.mp4"}])
    body = {"text": "https://www.douyin.com/video/123", "request_id": "a" * 32}
    task_id = client.post("/api/parse", json=body).json()["id"]
    original = wait_for_task(client, task_id)
    assert client.get(f"/api/tasks/{task_id}").json() == original
    assert client.get(f"/api/tasks/{task_id}").headers["cache-control"] == "no-store"
    assert client.post("/api/parse", json=body).json() == original
    assert len(calls) == 1
    conflict = client.post("/api/parse", json={**body, "text": "https://www.douyin.com/video/456"})
    assert conflict.status_code == 409


def test_unknown_task_and_invalid_submission_id(client):
    assert client.get("/api/tasks/unknown").status_code == 404
    assert client.post("/api/parse", json={"text": "https://x.com/u/status/1", "request_id": "short"}).status_code == 422


def test_full_queue_keeps_alias_deduplication_and_returns_retry_hint(monkeypatch):
    release = Event()
    calls = []

    def parse(url):
        calls.append(url)
        assert release.wait(3)
        return [{"download_url": "https://cdn.example.org/a.mp4"}]

    monkeypatch.setattr(main, "_parse_blocking", parse)
    monkeypatch.setattr(main, "PARSE_QUEUE_SIZE", 1)
    with TestClient(main.app) as client:
        try:
            first = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123?share=1"})
            assert first.status_code == 202
            waiting = client.post("/api/parse", json={"text": "https://www.douyin.com/video/456"})
            assert waiting.json()["queue_position"] == 1
            duplicate = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123?share=2"})
            assert duplicate.json()["id"] == first.json()["id"]
            full = client.post("/api/parse", json={"text": "https://www.douyin.com/video/789"})
            assert full.status_code == 503
            assert full.headers["retry-after"] == "5"
        finally:
            release.set()
    # 释放阻塞后，第二个合法任务可能赶在服务退出前启动；重复链接和被拒任务不能启动。
    assert calls[0] == "https://www.douyin.com/video/123?share=1"
    assert calls[1:] in ([], ["https://www.douyin.com/video/456"])


def test_get_parse_returns_absolute_browser_url_and_preserves_share_signature(monkeypatch, client):
    calls = []

    def parse(url):
        calls.append(url)
        return [{"download_url": "https://cdn.example.org/a.mp4", "local_download_url": "/api/download/example"}]

    monkeypatch.setattr(main, "_parse_blocking", parse)
    url = "https://www.xiaohongshu.com/explore/abc?xsec_token=a+b%2Fc&xsec_source=pc_share"
    response = client.get("/api/parse", params={"text": f"分享视频：{url} 复制后打开"})
    assert response.status_code == 200
    assert calls == [url]
    assert response.json()["browser_download_url"] == "http://testserver/api/download/example"
    assert response.json()["count"] == 1
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize("text", ["", "没有链接", "http://127.0.0.1/video", "http://192.168.1.1/", "x" * 8001])
def test_get_parse_rejects_invalid_input(monkeypatch, client, text):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: pytest.fail("非法输入不能调用解析器"))
    assert client.get("/api/parse", params={"text": text}).status_code == 422


@pytest.mark.parametrize("failure,code", [(None, 502), (HTTPException(status_code=403, detail="需要登录"), 403)])
def test_get_parse_reports_job_failure(monkeypatch, client, failure, code):
    def parse(url):
        if failure:
            raise failure
        return []

    monkeypatch.setattr(main, "_parse_blocking", parse)
    response = client.get("/api/parse", params={"text": "https://v.douyin.com/abc/", "redirect": "true"}, follow_redirects=False)
    assert response.status_code == code
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


def test_get_parse_without_local_download_does_not_redirect_to_raw_media(monkeypatch, client):
    monkeypatch.setattr(main, "_parse_blocking", lambda url: [{"download_url": "https://cdn.example.org/a.mp4"}])
    params = {"text": "https://www.bilibili.com/video/BV123"}
    assert client.get("/api/parse", params=params).json()["browser_download_url"] is None
    response = client.get("/api/parse", params={**params, "redirect": "true"}, follow_redirects=False)
    assert response.status_code == 422
    assert "暂不支持" in response.json()["detail"]


def test_get_parse_shares_post_queue_capacity(monkeypatch):
    release = Event()
    calls = []

    def parse(url):
        calls.append(url)
        assert release.wait(3)
        return [{"download_url": "https://cdn.example.org/a.mp4"}]

    monkeypatch.setattr(main, "_parse_blocking", parse)
    monkeypatch.setattr(main, "PARSE_QUEUE_SIZE", 1)
    with TestClient(main.app) as client:
        try:
            client.post("/api/parse", json={"text": "https://www.douyin.com/video/123"})
            client.post("/api/parse", json={"text": "https://www.douyin.com/video/456"})
            full = client.get("/api/parse", params={"text": "https://www.douyin.com/video/789"})
            assert full.status_code == 503
            assert full.headers["retry-after"] == "5"
        finally:
            release.set()
    assert "https://www.douyin.com/video/789" not in calls
