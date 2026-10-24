"""验证目标作品匹配和浏览器释放，避免页面素材被误认为用户的视频。"""

from types import SimpleNamespace

import pytest

from app import douyin, main


def detail_body(identifier="123"):
    return {"aweme_detail": {
        "aweme_id": identifier,
        "desc": "目标视频",
        "video": {
            "play_addr": {"url_list": ["https://cdn.example.org/target.mp4"]},
            "cover": {"url_list": ["https://cdn.example.org/cover.jpg"]},
            "format": "mp4",
        },
    }}


@pytest.mark.parametrize("body", [None, "", {}, {"aweme_detail": "invalid"}, detail_body("456")])
def test_unrelated_or_missing_detail_is_rejected(body):
    assert douyin._detail_item(body, "123") is None


def test_detail_has_target_metadata():
    item = main._result_item(douyin._detail_item(detail_body(), "123"))
    assert item["identifier"] == "123"
    assert item["title"] == "目标视频"
    assert item["download_url"] == "https://cdn.example.org/target.mp4"
    assert item["cover_url"] == "https://cdn.example.org/cover.jpg"


def test_adaptive_playback_mode_is_not_file_extension():
    body = detail_body()
    body["aweme_detail"]["video"]["format"] = "dash"
    item = main._result_item(douyin._detail_item(body, "123"))
    assert item["ext"] == "mp4"


def install_page(monkeypatch, bodies=(), error=None):
    packets = iter(SimpleNamespace(is_failed=False, response=SimpleNamespace(body=body)) for body in bodies)
    page = SimpleNamespace(url="https://www.douyin.com/video/123", user_agent="test-browser", closed=False)

    def get(url):
        if error:
            raise error

    def quit_page(**kwargs):
        page.closed = True

    page.get = get
    page.quit = quit_page
    page.listen = SimpleNamespace(start=lambda target: None, wait=lambda **kwargs: next(packets))
    monkeypatch.setattr(douyin, "SystemMediaGrabber", lambda **kwargs: SimpleNamespace(buildbrowserpage=lambda: page))
    return page


def test_short_link_waits_for_matching_detail_and_closes(monkeypatch):
    page = install_page(monkeypatch, [detail_body("456"), detail_body()])
    items = douyin.parse_douyin("https://v.douyin.com/example/")
    assert items[0]["identifier"] == "123"
    assert page.closed


def test_navigation_failure_closes_browser(monkeypatch):
    page = install_page(monkeypatch, error=RuntimeError("导航失败"))
    with pytest.raises(RuntimeError, match="导航失败"):
        douyin.parse_douyin("https://www.douyin.com/video/123")
    assert page.closed


def test_detail_timeout_closes_browser(monkeypatch):
    page = install_page(monkeypatch)
    times = iter([0, 31])
    monkeypatch.setattr(douyin.time, "monotonic", lambda: next(times))
    assert douyin.parse_douyin("https://www.douyin.com/video/123") == []
    assert page.closed


@pytest.mark.parametrize("url", ["https://www.douyin.com/video/123", "https://v.douyin.com/example/"])
def test_douyin_uses_target_detail_parser(monkeypatch, url):
    monkeypatch.setenv("VIDEO_SOURCES", "DouyinVideoClient")
    monkeypatch.setattr(douyin, "parse_douyin", lambda target: [douyin._detail_item(detail_body(), "123")])
    assert main._parse_blocking(url)[0]["source"] == "DouyinBrowser"


def test_douyin_respects_disabled_source(monkeypatch):
    monkeypatch.setenv("VIDEO_SOURCES", "BilibiliVideoClient")
    monkeypatch.setattr(douyin, "parse_douyin", lambda url: pytest.fail("不应启动未启用的解析器"))
    assert main._parse_blocking("https://www.douyin.com/video/123") == []
