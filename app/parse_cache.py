"""只缓存短期有效的解析元数据，下载凭证仍由每次请求单独生成。"""

import re
from threading import Condition
from urllib.parse import parse_qs, urlsplit

from cachetools import TTLCache


class SuccessfulResultsCache(TTLCache):
    def __setitem__(self, key, value):
        # 空结果通常来自登录或站点临时故障，不能阻止用户立即重试。
        if value:
            super().__setitem__(key, value)


PARSE_CACHE = SuccessfulResultsCache(maxsize=128, ttl=120)
# 缓存访问需要加锁，条件变量还能让同一链接的并发请求共享一次平台调用。
PARSE_CONDITION = Condition()


def canonical_url(platform: str, url: str) -> str:
    parts = urlsplit(url)
    if platform == "youtube":
        # 分享参数和播放列表不改变目标视频，短链与完整链接应共享同一次解析。
        video_id = parts.path.strip("/") if parts.hostname == "youtu.be" else parse_qs(parts.query).get("v", [""])[0]
        if not video_id and (match := re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})/?", parts.path)):
            video_id = match.group(1)
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return f"https://www.youtube.com/watch?v={video_id}"
    if platform == "twitter":
        # 保留视频序号，只去掉分享追踪参数并统一域名，避免不同视频误用同一结果。
        return parts._replace(scheme="https", netloc="x.com", query="", fragment="").geturl()
    if platform == "douyin" and (match := re.search(r"/video/(\d+)", parts.path)):
        return f"https://www.douyin.com/video/{match.group(1)}"
    # 短链接可能依赖查询参数，未解析前不能随意删掉。
    return parts._replace(fragment="").geturl()


def clear_parse_cache() -> None:
    with PARSE_CONDITION:
        PARSE_CACHE.clear()
