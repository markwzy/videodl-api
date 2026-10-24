"""验证 X 原生媒体、登录文件隔离和平台错误边界，不发出真实网络请求。"""

from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from app import main, twitter


MEDIA_URL = "https://video.twimg.com/ext_tw_video/123/pu/vid/1280x720/video.mp4"


@pytest.fixture(autouse=True)
def no_account_file(monkeypatch):
    # 测试容器可以有真实的只读挂载，离线测试必须主动排除它。
    monkeypatch.delenv("X_COOKIES_FILE", raising=False)


def media_entry(identifier="123"):
    return {
        "id": identifier, "title": "X 示例视频", "url": MEDIA_URL,
        "ext": "mp4", "protocol": "https", "thumbnail": "https://pbs.twimg.com/cover.jpg",
        "http_headers": {"User-Agent": "test-agent", "Cookie": "fake-cookie", "Authorization": "fake-auth"},
    }


def install_client(monkeypatch, info=None, error=None):
    seen = {}

    class FakeClient:
        def __init__(self, options):
            seen["options"] = options
            if "cookiefile" in options:
                path = Path(options["cookiefile"])
                seen["cookie_path"] = path
                seen["cookie_content"] = path.read_text()
                path.write_text("updated session")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, **kwargs):
            seen["request"] = (url, kwargs)
            if error:
                raise error
            return info

    monkeypatch.setattr(twitter, "YoutubeDL", FakeClient)
    return seen


def test_native_video_does_not_expose_account_headers():
    item = twitter._video_item(media_entry())
    assert item["download_url"] == MEDIA_URL
    assert item["default_download_headers"] == {"User-Agent": "test-agent", "Referer": "https://x.com/"}
    assert "fake-cookie" not in str(item)
    assert "fake-auth" not in str(item)


@pytest.mark.parametrize("changes", [
    {"url": "https://video.twimg.com.evil.example/video.mp4"},
    {"url": "https://127.0.0.1/video.mp4"},
    {"url": "https://user:pass@video.twimg.com/video.mp4"},
    {"protocol": "m3u8_native"},
    {"ext": "webm"},
])
def test_non_native_or_non_file_media_is_rejected(changes):
    assert twitter._video_item({**media_entry(), **changes}) is None


def test_multiple_videos_use_sdk_file_selection_and_limit(monkeypatch):
    seen = install_client(monkeypatch, {"entries": [media_entry("1"), media_entry("2")]})
    items = twitter.parse_twitter("https://x.com/user/status/123", 1)
    assert [item["identifier"] for item in items] == ["1"]
    assert seen["request"][1] == {"download": False, "ie_key": "Twitter"}
    assert seen["options"]["format"] == "best[ext=mp4][protocol=https]"
    assert seen["options"]["cachedir"] is False


def test_cookie_source_is_not_changed_or_kept_in_working_copy(monkeypatch, tmp_path):
    source = tmp_path / "account.txt"
    source.write_text("original fake session")
    source.chmod(0o400)
    monkeypatch.setenv("X_COOKIES_FILE", str(source))
    seen = install_client(monkeypatch, media_entry())
    assert twitter.parse_twitter("https://x.com/user/status/123", 20)
    assert seen["cookie_content"] == "original fake session"
    assert source.read_text() == "original fake session"
    assert not seen["cookie_path"].exists()


def test_missing_cookie_file_is_configuration_error(monkeypatch, tmp_path):
    monkeypatch.setenv("X_COOKIES_FILE", str(tmp_path / "missing.txt"))
    with pytest.raises(HTTPException) as error:
        twitter.parse_twitter("https://x.com/user/status/123", 20)
    assert error.value.status_code == 503


@pytest.mark.parametrize("upstream_error,expected_status", [
    (DownloadError("Login required"), 403),
    (DownloadError("No video could be found in this tweet"), 422),
    (DownloadError("network failure"), 502),
    (CookieLoadError("invalid login line: fake-secret"), 503),
])
def test_platform_errors_have_safe_messages(monkeypatch, upstream_error, expected_status):
    install_client(monkeypatch, error=upstream_error)
    with pytest.raises(HTTPException) as error:
        twitter.parse_twitter("https://x.com/user/status/123", 20)
    assert error.value.status_code == expected_status
    assert "fake-secret" not in error.value.detail


@pytest.mark.parametrize("domain", ["x.com", "twitter.com", "mobile.twitter.com"])
def test_status_links_use_x_parser_and_local_download(monkeypatch, domain):
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    monkeypatch.setattr(twitter, "parse_twitter", lambda url, limit: [twitter._video_item(media_entry())])
    items = main._parse_blocking(f"https://{domain}/user/status/123?s=20")
    assert items[0]["source"] == "TwitterYtDlp"
    assert items[0]["local_download_url"].startswith("/api/download/")


def test_disabled_x_parser_is_not_called(monkeypatch):
    monkeypatch.setenv("VIDEO_SOURCES", "BilibiliVideoClient")
    monkeypatch.setattr(twitter, "parse_twitter", lambda *args: pytest.fail("不应调用未启用的解析器"))
    assert main._parse_blocking("https://x.com/user/status/123") == []


def test_only_x_does_not_enable_all_upstream_sources(monkeypatch):
    monkeypatch.setenv("VIDEO_SOURCES", "TwitterVideoClient")
    assert main._parse_blocking("https://www.bilibili.com/video/BV123") == []


def test_platform_login_error_survives_api_boundary(monkeypatch):
    def parse(url):
        raise HTTPException(status_code=403, detail="需要有效的 X 登录文件")

    monkeypatch.setattr(main, "_parse_blocking", parse)
    response = TestClient(main.app).post("/api/parse", json={"text": "https://x.com/user/status/123"})
    assert response.status_code == 403
    assert response.json()["detail"] == "需要有效的 X 登录文件"
