"""从浏览器正常加载的作品详情响应中读取播放地址。"""

from copy import deepcopy
import re
import time
from urllib.parse import parse_qs, urlsplit

from cachetools import cached

from .browser import DOUYIN_BROWSER
from .media import available_formats
from .parse_cache import PARSE_CACHE, PARSE_CONDITION, canonical_url


def _video_id(url: str) -> str | None:
    parts = urlsplit(url)
    if match := re.search(r"/video/(\d+)", parts.path):
        return match.group(1)
    values = parse_qs(parts.query).get("modal_id", [])
    return values[0] if values and values[0].isdigit() else None


def _detail_item(body: object, expected_id: str) -> dict | None:
    if not isinstance(body, dict):
        return None
    detail = body.get("aweme_detail") or {}
    # 页面还会请求推荐作品，必须核对编号，避免返回其他视频或页面宣传素材。
    if not isinstance(detail, dict) or str(detail.get("aweme_id")) != expected_id:
        return None
    video = detail.get("video") or {}
    headers = {"Referer": "https://www.douyin.com/"}
    address = video.get("play_addr") or {}
    candidates = [{
        "download_url": address.get("url_list"), "width": address.get("width"),
        "height": address.get("height"), "filesize": address.get("data_size"),
        "codec": "H.265" if video.get("is_h265") else "H.264",
        "default_download_headers": headers,
    }]
    for variant in video.get("bit_rate") or []:
        # 分段自适应版本可能只有画面，下载入口只列出平台明确提供的独立媒体文件。
        if not isinstance(variant, dict) or variant.get("format") != "mp4":
            continue
        address = variant.get("play_addr") or {}
        candidates.append({
            "download_url": address.get("url_list"), "width": address.get("width"),
            "height": address.get("height"), "filesize": address.get("data_size"),
            "fps": variant.get("FPS"), "bitrate": variant.get("bit_rate"),
            "codec": "H.265" if variant.get("is_h265") else "H.264",
            "default_download_headers": headers,
        })
    formats = available_formats(candidates, "douyin")
    if not formats:
        return None
    return {
        "source": "DouyinBrowser",
        "identifier": expected_id,
        "title": detail.get("desc") or "未命名视频",
        "download_url": formats[0]["download_url"],
        "cover_url": (video.get("cover") or {}).get("url_list"),
        # 详情中的播放模式可能是自适应分段；这里选取的是独立的原始媒体地址。
        "ext": "mp4",
        "default_download_headers": headers,
        "formats": formats,
    }


def parse_douyin(url: str) -> list[dict]:
    # 返回副本，后续结果整理不能改动其他请求会复用的元数据。
    return deepcopy(_parse_douyin(url))


@cached(
    cache=PARSE_CACHE,
    key=lambda url: ("douyin", canonical_url("douyin", url)),
    condition=PARSE_CONDITION,
)
def _parse_douyin(url: str) -> list[dict]:
    with DOUYIN_BROWSER.page() as page:
        # 在导航前监听，直接使用网页自己的详情请求，避免依赖已经失效的分享页内嵌字段。
        page.listen.start("/aweme/v1/web/aweme/detail/")
        page.get(url)
        # 短链接导航后才能确定作品编号；只处理单条视频，不能从主页任意挑选结果。
        expected_id = _video_id(url) or _video_id(page.url)
        if not expected_id:
            return []
        deadline = time.monotonic() + 30
        while (remaining := deadline - time.monotonic()) > 0:
            packet = page.listen.wait(timeout=min(3, remaining), raise_err=False)
            if not packet or packet.is_failed:
                continue
            if item := _detail_item(packet.response.body, expected_id):
                # 下载时延续解析浏览器的身份和来源，裸地址在其他客户端可能被站点拒绝。
                item["default_download_headers"]["User-Agent"] = page.user_agent
                for variant in item["formats"]:
                    variant["default_download_headers"]["User-Agent"] = page.user_agent
                return [item]
        return []
