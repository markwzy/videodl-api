"""验证清晰度排序、真实尺寸和选中版本的下载边界。"""

from fastapi.testclient import TestClient

from app import downloads, main
from app.media import available_formats


def variant(width, height, name=None, **kwargs):
    return {
        "download_url": f"https://video.twimg.com/{name or str(width) + 'x' + str(height)}.mp4", "width": width, "height": height,
        "default_download_headers": {"User-Agent": "private-media-agent"}, **kwargs,
    }


def test_portrait_and_landscape_use_short_side_without_changing_dimensions():
    formats = available_formats([variant(720, 1280), variant(1920, 1080)], "twitter")
    assert [item["label"] for item in formats] == ["1080p · 1920×1080", "720p · 720×1280"]


def test_duplicate_resolution_keeps_best_frame_rate_then_bitrate():
    formats = available_formats([
        variant(720, 1280, "low", fps=30, bitrate=1000000),
        variant(720, 1280, "high", fps=60, bitrate=2000000),
        variant(720, 1280, "medium", fps=60, bitrate=1500000),
    ], "twitter")
    assert len(formats) == 1
    assert formats[0]["download_url"].endswith("/high.mp4")


def test_unknown_dimensions_are_not_invented_and_bad_targets_are_rejected():
    formats = available_formats([
        variant(None, None),
        variant(4096, 2160, download_url="https://127.0.0.1/video.mp4"),
        variant(4096, 2160, download_url="https://video.twimg.com:invalid/video.mp4"),
    ], "twitter")
    assert len(formats) == 1
    assert formats[0]["label"] == "分辨率未知"
    assert formats[0]["width"] is None and formats[0]["height"] is None


def test_selected_quality_downloads_its_own_url_and_keeps_headers_private(monkeypatch):
    formats = available_formats([variant(1280, 720, "high"), variant(640, 360, "low")], "twitter")
    info = {"source": "TwitterYtDlp", "title": "示例视频", "download_url": formats[0]["download_url"], "formats": formats}
    first = main._result_item(info)
    second = main._result_item(info)
    assert first["local_download_url"] == first["formats"][0]["local_download_url"]
    assert {item["local_download_url"] for item in first["formats"]}.isdisjoint(
        {item["local_download_url"] for item in second["formats"]},
    )
    assert "private-media-agent" not in str(first)
    assert "default_download_headers" not in str(first)
    selected = first["formats"][1]
    ticket = downloads._get_ticket(selected["local_download_url"].rsplit("/", 1)[1])
    assert ticket.url == "https://video.twimg.com/low.mp4"
    assert "360p" in ticket.filename

    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        class Response:
            status_code = 206
            headers = {"Content-Type": "video/mp4"}

            def iter_content(self, **kwargs):
                return iter([b"low quality"])

            def close(self):
                pass
        return Response()

    monkeypatch.setattr(downloads.requests, "get", get)
    response = TestClient(main.app).get(selected["local_download_url"], headers={"Range": "bytes=0-10"})
    assert response.status_code == 206
    assert response.content == b"low quality"
    assert calls[0][0] == ticket.url
    assert calls[0][1]["User-Agent"] == "private-media-agent"


def test_one_response_does_not_evict_earlier_video_quality_tickets():
    formats = available_formats([variant(160 * i, 90 * i, str(i)) for i in range(1, 11)], "twitter")
    infos = [main._result_item({
        "source": "TwitterYtDlp", "title": str(index), "download_url": formats[0]["download_url"], "formats": formats,
    }) for index in range(main.MAX_ITEMS)]
    for info in infos:
        for item in info["formats"]:
            ticket = downloads._get_ticket(item["local_download_url"].rsplit("/", 1)[1])
            assert ticket.url == item["download_url"]
