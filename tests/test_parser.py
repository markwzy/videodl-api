"""隔离网络依赖，验证上游无效结果不会阻断浏览器抓取。"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from app import main


@pytest.mark.parametrize(
    "platform_results,browser_calls,expected",
    [
        ([{"download_url": "https://cdn.example.org/platform.mp4"}], 0, "platform.mp4"),
        ([{"err_msg": "missing video"}], 1, "browser.mp4"),
        ([{"download_url": "http://127.0.0.1/private.mp4"}], 1, "browser.mp4"),
        ([], 0, None),
    ],
)
def test_platform_result_controls_browser(monkeypatch, platform_results, browser_calls, expected):
    calls = []
    released = []

    class FakeVideoClient:
        def __init__(self, **kwargs):
            assert "TwitterVideoClient" not in kwargs["allowed_video_sources"]

        def parsefromurl(self, url):
            return platform_results

    class FakeGrabber:
        def __init__(self, **kwargs):
            pass

        def parsefromurl(self, url):
            calls.append(url)
            return [{"download_url": "https://cdn.example.org/browser.mp4"}]

    upstream = ModuleType("videodl.videodl")
    upstream.VideoClient = FakeVideoClient
    browser = ModuleType("app.browser")
    browser.SystemMediaGrabber = FakeGrabber
    browser.DOUYIN_BROWSER = SimpleNamespace(close=lambda: released.append(True))
    monkeypatch.setitem(sys.modules, "videodl.videodl", upstream)
    monkeypatch.setitem(sys.modules, "app.browser", browser)
    results = main._parse_blocking("https://www.bilibili.com/video/BV123")
    assert len(calls) == browser_calls
    assert released == [True]
    if expected:
        assert len(results) == 1
        assert results[0]["download_url"].endswith(expected)
    else:
        assert results == []
