"""验证缓存新鲜度、账号隔离及平台之间的排队隔离。"""

from threading import Event
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app import downloads, main, twitter
from app.parse_cache import PARSE_CACHE, PARSE_CONDITION, canonical_url


X_URL = "https://x.com/user/status/123?s=20"


@pytest.fixture(autouse=True)
def no_real_login_file(monkeypatch):
    monkeypatch.delenv("X_COOKIES_FILE", raising=False)


def install_extractor(monkeypatch, empty=False):
    calls = []

    class FakeClient:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, *args, **kwargs):
            return {} if empty else {
                "id": "123", "title": "原始标题", "ext": "mp4", "protocol": "https",
                "url": "https://video.twimg.com/example.mp4", "http_headers": {"User-Agent": "test"},
            }

    monkeypatch.setattr(twitter, "YoutubeDL", FakeClient)
    return calls


def test_share_aliases_reuse_metadata_but_return_independent_values(monkeypatch):
    calls = install_extractor(monkeypatch)
    first = twitter.parse_twitter(X_URL, 20)
    first[0]["title"] = "其他请求修改的标题"
    first[0]["default_download_headers"]["User-Agent"] = "changed"
    second = twitter.parse_twitter("https://twitter.com/user/status/123?s=99", 20)
    assert len(calls) == 1
    assert second[0]["title"] == "原始标题"
    assert second[0]["default_download_headers"]["User-Agent"] == "test"


def test_cache_expiration_fetches_platform_again(monkeypatch):
    calls = install_extractor(monkeypatch)
    twitter.parse_twitter(X_URL, 20)
    with PARSE_CONDITION:
        PARSE_CACHE.expire(PARSE_CACHE.timer() + PARSE_CACHE.ttl + 1)
    twitter.parse_twitter(X_URL, 20)
    assert len(calls) == 2


def test_empty_results_do_not_block_retry(monkeypatch):
    calls = install_extractor(monkeypatch, empty=True)
    assert twitter.parse_twitter(X_URL, 20) == []
    assert twitter.parse_twitter(X_URL, 20) == []
    assert len(calls) == 2


def test_changed_login_file_does_not_reuse_old_account_cache(monkeypatch, tmp_path):
    source = tmp_path / "account.txt"
    source.write_text("fake account A")
    monkeypatch.setenv("X_COOKIES_FILE", str(source))
    calls = install_extractor(monkeypatch)
    twitter.parse_twitter(X_URL, 20)
    source.write_text("fake account B")
    twitter.parse_twitter(X_URL, 20)
    assert len(calls) == 2


def test_cached_parse_issues_new_download_ticket(monkeypatch):
    calls = install_extractor(monkeypatch)
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    first = main._parse_blocking(X_URL)[0]
    second = main._parse_blocking(X_URL)[0]
    assert len(calls) == 1
    assert first["local_download_url"] != second["local_download_url"]


def test_rejected_download_forces_fresh_platform_parse(monkeypatch):
    calls = install_extractor(monkeypatch)
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    first = main._parse_blocking(X_URL)[0]
    upstream = SimpleNamespace(status_code=403, headers={}, close=lambda: None)
    monkeypatch.setattr(downloads.requests, "get", lambda *args, **kwargs: upstream)
    assert TestClient(main.app).get(first["local_download_url"]).status_code == 502
    main._parse_blocking(X_URL)
    assert len(calls) == 2


def test_video_index_is_part_of_x_cache_identity():
    assert canonical_url("twitter", "https://x.com/user/status/123/video/1") != canonical_url(
        "twitter", "https://x.com/user/status/123/video/2",
    )


def test_x_does_not_wait_for_busy_browser(monkeypatch, wait_for_task):
    browser_started = Event()
    release_browser = Event()

    def parse(url):
        if "douyin.com" in url:
            browser_started.set()
            if not release_browser.wait(timeout=5):
                raise RuntimeError("浏览器任务不应阻塞 X 请求")
        return [{"download_url": "https://cdn.example.org/video.mp4"}]

    monkeypatch.setattr(main, "_parse_blocking", parse)
    with TestClient(main.app) as client:
        browser_call = client.post("/api/parse", json={"text": "https://www.douyin.com/video/123"})
        try:
            assert browser_call.status_code == 202
            assert browser_started.wait(timeout=2)
            response = client.post("/api/parse", json={"text": X_URL})
            assert response.status_code == 202
            assert wait_for_task(client, response.json()["id"])["status"] == "succeeded"
            assert client.get(f'/api/tasks/{browser_call.json()["id"]}').json()["status"] == "running"
        finally:
            release_browser.set()
        assert wait_for_task(client, browser_call.json()["id"])["status"] == "succeeded"
