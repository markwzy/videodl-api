"""由维护中的站点提取器处理 X，登录状态仅用于官方接口请求。"""

from copy import deepcopy
import hashlib
from itertools import islice
import logging
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from cachetools import cached
from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError, float_or_none

from .media import available_formats
from .parse_cache import PARSE_CACHE, PARSE_CONDITION, canonical_url


def _video_item(entry: dict) -> dict | None:
    candidates = []
    for variant in [entry, *(entry.get("formats") or [])]:
        # 提取器同时返回播放列表和音频轨，只列出能直接下载的视频文件。
        if not isinstance(variant, dict) or variant.get("ext") != "mp4" or variant.get("protocol") != "https" or variant.get("vcodec") == "none":
            continue
        # 各档位单独处理请求头；账号凭证不能进入媒体请求或 API 响应。
        headers = {
            name: value for name, value in {**(entry.get("http_headers") or {}), **(variant.get("http_headers") or {})}.items()
            if name.lower() in {"user-agent", "referer", "accept", "origin"} and isinstance(value, str)
        }
        if not any(name.lower() == "referer" for name in headers):
            headers["Referer"] = "https://x.com/"
        candidates.append({
            "download_url": variant.get("url"), "width": variant.get("width"), "height": variant.get("height"),
            "fps": variant.get("fps"), "bitrate": float_or_none(variant.get("tbr"), invscale=1000),
            "filesize": variant.get("filesize"), "codec": variant.get("vcodec"),
            "default_download_headers": headers,
        })
    formats = available_formats(candidates, "twitter")
    if not formats:
        return None
    return {
        "source": "TwitterYtDlp",
        "identifier": str(entry.get("id") or ""),
        "title": entry.get("title") or "X 视频",
        "cover_url": entry.get("thumbnail"),
        "download_url": formats[0]["download_url"],
        "ext": "mp4",
        "default_download_headers": formats[0]["default_download_headers"],
        "formats": formats,
    }


def parse_twitter(url: str, limit: int) -> list[dict]:
    cookie_source = os.getenv("X_COOKIES_FILE", "").strip()
    # 每次请求拥有独立登录副本，提取器更新会话时不会修改只读原件或影响其他请求。
    with TemporaryDirectory(prefix="videodl-x-") as temp:
        options = {
            "format": "best[ext=mp4][protocol=https]",
            "allowed_extractors": ["twitter.*"],
            "skip_download": True,
            "cachedir": False,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 15,
            "retries": 1,
            "extractor_retries": 1,
            "playlistend": limit,
            # 库的异常可能包含登录文件的原始行，边界层统一转换成不含凭证的提示。
            "logger": logging.Logger("x-extractor", level=logging.CRITICAL),
        }
        identity = None
        if cookie_source:
            cookie_copy = Path(temp) / "cookies.txt"
            try:
                shutil.copyfile(cookie_source, cookie_copy)
                cookie_copy.chmod(0o600)
                # 使用实际读取的登录副本计算身份，换账号或更新文件后不会命中旧账号的缓存。
                identity = hashlib.sha256(cookie_copy.read_bytes()).digest()
            except OSError as exc:
                raise HTTPException(status_code=503, detail="无法读取 X 登录文件，请检查本机文件和只读挂载") from exc
            options["cookiefile"] = str(cookie_copy)
        try:
            return deepcopy(_extract_twitter(url, limit, options, identity))
        except CookieLoadError as exc:
            raise HTTPException(status_code=503, detail="X 登录文件无效，请提供 Netscape 格式的 cookies.txt") from exc
        except DownloadError as exc:
            message = str(exc).lower()
            if any(text in message for text in ("login", "log in", "not authorized", "401", "403")):
                raise HTTPException(status_code=403, detail="X 要求有效登录，请配置可查看该推文的账号登录文件后重试") from exc
            if "no video" in message or "no formats" in message:
                detail = (
                    "X 未返回可下载视频，请确认账号可查看该推文且登录文件仍有效"
                    if cookie_source else "X 未返回可下载视频；受限推文需要配置有效的账号登录文件"
                )
                raise HTTPException(status_code=422, detail=detail) from exc
            raise HTTPException(status_code=502, detail="X 视频解析失败，请检查网络、登录状态或站点接口变化") from exc


@cached(
    cache=PARSE_CACHE,
    key=lambda url, limit, options, identity: ("twitter", canonical_url("twitter", url), limit, identity),
    condition=PARSE_CONDITION,
)
def _extract_twitter(url: str, limit: int, options: dict, identity: bytes | None) -> list[dict]:
    # 临时文件路径不参与缓存标识；登录副本的指纹已经包含在缓存键内。
    with YoutubeDL(options) as client:
        info = client.extract_info(url, download=False, ie_key="Twitter") or {}
        entries = info["entries"] if "entries" in info else [info]
        return [
            item for entry in islice(entries, limit)
            if isinstance(entry, dict) and (item := _video_item(entry))
        ]
