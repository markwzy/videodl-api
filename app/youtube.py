"""只解析目标视频，避免网页扫描误取广告或未解码的播放地址。"""

from copy import deepcopy
import logging

from cachetools import cached
from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, float_or_none

from .downloads import media_platform
from .media import available_formats
from .parse_cache import PARSE_CACHE, PARSE_CONDITION, canonical_url


def _headers(entry: dict, variant: dict) -> dict:
    # 只携带媒体所需的公开请求头，账号凭证不能进入下载请求或接口响应。
    return {
        name: value for name, value in {**(entry.get("http_headers") or {}), **(variant.get("http_headers") or {})}.items()
        if name.lower() in {"user-agent", "referer", "accept", "origin"} and isinstance(value, str)
    }


def _video_item(entry: dict) -> dict:
    if entry.get("is_live") or entry.get("live_status") == "is_upcoming":
        raise HTTPException(status_code=422, detail="暂不支持 YouTube 直播，请在直播结束后下载")
    # 只接受完整的官方直连文件；播放列表、分片清单和受保护内容不能伪装成单个视频。
    variants = [
        variant for variant in entry.get("formats", [])
        if variant.get("protocol") == "https" and not variant.get("has_drm")
        and media_platform(variant.get("url") or "") == "youtube"
    ]
    audio = max((
        variant for variant in variants
        if variant.get("ext") == "m4a" and variant.get("vcodec") == "none"
        and str(variant.get("acodec", "")).startswith("mp4a")
    ), key=lambda variant: variant.get("abr") or variant.get("tbr") or 0, default=None)
    candidates = []
    for variant in variants:
        if variant.get("ext") != "mp4" or variant.get("vcodec") in (None, "none") or not variant.get("acodec"):
            continue
        separated = variant.get("acodec") == "none"
        if separated and audio is None:
            continue
        candidates.append({
            "download_url": variant["url"], "width": variant.get("width"), "height": variant.get("height"),
            "fps": variant.get("fps"), "bitrate": float_or_none(variant.get("tbr"), invscale=1000),
            # 合成输出大小受封装影响，不能把只有画面的大小显示为完整文件大小。
            "filesize": None if separated else variant.get("filesize"), "codec": variant.get("vcodec"),
            "default_download_headers": _headers(entry, variant),
            "audio_download_url": audio["url"] if separated else None,
            "audio_download_headers": _headers(entry, audio) if separated else {},
        })
    formats = available_formats(candidates, "youtube")
    if not formats:
        raise HTTPException(status_code=422, detail="YouTube 未返回可下载的完整音视频，请确认视频公开且无需登录")
    return {
        "source": "YouTubeYtDlp", "identifier": str(entry.get("id") or ""),
        "title": entry.get("title") or "YouTube 视频", "cover_url": entry.get("thumbnail"),
        "download_url": formats[0]["download_url"], "audio_download_url": formats[0].get("audio_download_url"),
        "ext": "mp4", "default_download_headers": formats[0]["default_download_headers"], "formats": formats,
    }


def parse_youtube(url: str) -> list[dict]:
    try:
        return deepcopy(_extract_youtube(canonical_url("youtube", url)))
    except DownloadError as exc:
        message = str(exc).lower()
        if any(text in message for text in ("sign in", "login", "private", "age-restricted", "not a bot")):
            raise HTTPException(status_code=403, detail="YouTube 要求登录或验证，请确认视频公开并检查当前网络") from exc
        if any(text in message for text in ("unavailable", "removed", "not available")):
            raise HTTPException(status_code=422, detail="YouTube 视频不可用，可能已删除或存在地区限制") from exc
        raise HTTPException(status_code=502, detail="YouTube 解析失败，请检查网络或更新提取器及脚本运行依赖") from exc


@cached(cache=PARSE_CACHE, key=lambda url: ("youtube", url), condition=PARSE_CONDITION)
def _extract_youtube(url: str) -> list[dict]:
    options = {
        "format": "all", "allowed_extractors": ["youtube"], "noplaylist": True,
        "skip_download": True, "cachedir": False, "quiet": True, "no_warnings": True,
        "socket_timeout": 15, "retries": 1, "extractor_retries": 1,
        # 复用上游依赖附带的运行时及配套脚本，不在解析时临时下载并执行远程脚本。
        "js_runtimes": {"node": {}}, "remote_components": [],
        "logger": logging.Logger("youtube-extractor", level=logging.CRITICAL),
    }
    with YoutubeDL(options) as client:
        info = client.extract_info(url, download=False, ie_key="Youtube") or {}
        return [_video_item(info)]
