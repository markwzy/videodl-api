"""复用现有站点提取器，只返回分享链接指定的小红书视频。"""

from copy import deepcopy
import logging
import re
from urllib.parse import urljoin, urlsplit

from cachetools import cached
from fastapi import HTTPException
import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, float_or_none

from .downloads import media_platform
from .media import available_formats
from .parse_cache import PARSE_CACHE, PARSE_CONDITION, canonical_url


NOTE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}
SHARE_HOSTS = {"xhslink.com", "www.xhslink.com"}


def _resolve_url(url: str) -> tuple[str, str]:
    for _ in range(6):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        # 短链只能在官方域名内跳转，不能借重定向访问内网或第三方站点。
        if (parts.scheme not in {"http", "https"} or parts.port not in (None, 80, 443)
                or parts.username or parts.password or host not in NOTE_HOSTS | SHARE_HOSTS):
            raise HTTPException(status_code=422, detail="小红书分享链接跳转到了不支持的地址")
        if host in NOTE_HOSTS:
            match = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-f]{24})/?", parts.path)
            if not match:
                raise HTTPException(status_code=422, detail="请提供小红书单条视频笔记的分享链接")
            # 保留签名参数，去掉它们会让原本公开可看的笔记变成无权限页面。
            return parts._replace(scheme="https", netloc="www.xiaohongshu.com", fragment="").geturl(), match.group(1)
        url = parts._replace(scheme="https", netloc=host, fragment="").geturl()
        with requests.get(url, allow_redirects=False, stream=True, timeout=(10, 15)) as response:
            if response.status_code not in (301, 302, 303, 307, 308) or not response.headers.get("Location"):
                raise HTTPException(status_code=422, detail="小红书短链接无效或已过期，请重新复制分享链接")
            url = urljoin(url, response.headers["Location"])
    raise HTTPException(status_code=422, detail="小红书短链接跳转次数过多，请使用完整笔记链接")


def _video_item(entry: dict, expected_id: str) -> dict:
    if str(entry.get("id")) != expected_id:
        raise HTTPException(status_code=502, detail="小红书返回的笔记与分享链接不一致，请重新解析")
    candidates = []
    for variant in entry.get("formats") or []:
        if (variant.get("ext") != "mp4" or variant.get("protocol") not in {"http", "https"}
                or variant.get("vcodec") == "none" or variant.get("acodec") == "none" or variant.get("has_drm")):
            continue
        parts = urlsplit(variant.get("url") or "")
        # 官方数据仍返回明文媒体地址；同一地址已支持加密访问，不能把明文地址交给下载器。
        media_url = parts._replace(scheme="https").geturl()
        if parts.scheme not in {"http", "https"} or media_platform(media_url) != "rednote":
            continue
        headers = {
            name: value for name, value in {**(entry.get("http_headers") or {}), **(variant.get("http_headers") or {})}.items()
            if name.lower() in {"user-agent", "referer", "accept", "origin"} and isinstance(value, str)
        }
        if not any(name.lower() == "referer" for name in headers):
            headers["Referer"] = "https://www.xiaohongshu.com/"
        candidates.append({
            "download_url": media_url, "width": variant.get("width"), "height": variant.get("height"),
            "fps": variant.get("fps"), "bitrate": float_or_none(variant.get("tbr"), invscale=1000),
            "filesize": variant.get("filesize"), "codec": variant.get("vcodec"),
            "default_download_headers": headers,
        })
    formats = available_formats(candidates, "rednote")
    if not formats:
        raise HTTPException(status_code=422, detail="小红书笔记未提供可下载视频，请确认是公开的视频笔记")
    cover = entry.get("thumbnail")
    if isinstance(cover, str) and cover.startswith("http://"):
        cover = "https://" + cover[len("http://"):]
    return {
        "source": "RednoteYtDlp", "identifier": expected_id,
        "title": entry.get("title") or "小红书视频", "cover_url": cover,
        "download_url": formats[0]["download_url"], "ext": "mp4",
        "default_download_headers": formats[0]["default_download_headers"], "formats": formats,
    }


def parse_rednote(url: str) -> list[dict]:
    try:
        # 缓存中的请求头和画质属于共享元数据，每次整理下载入口前必须复制。
        return deepcopy(_extract_rednote(canonical_url("rednote", url)))
    except (DownloadError, requests.RequestException) as exc:
        message = str(exc).lower()
        if any(text in message for text in ("login", "sign in", "403", "461", "471")):
            raise HTTPException(status_code=403, detail="小红书要求登录或验证，请重新复制可访问的完整分享链接") from exc
        if any(text in message for text in ("no video", "no formats", "404", "not available")):
            raise HTTPException(status_code=422, detail="小红书笔记不可用或未提供视频，请确认链接有效且笔记公开") from exc
        # 上游异常可能包含分享签名，只向接口返回固定提示。
        raise HTTPException(status_code=502, detail="小红书视频解析失败，请检查网络或分享链接是否已失效") from exc


@cached(cache=PARSE_CACHE, key=lambda url: ("rednote", url), condition=PARSE_CONDITION)
def _extract_rednote(url: str) -> list[dict]:
    url, expected_id = _resolve_url(url)
    options = {
        "format": "all", "allowed_extractors": ["XiaoHongShu"], "noplaylist": True,
        "skip_download": True, "cachedir": False, "quiet": True, "no_warnings": True,
        "socket_timeout": 15, "retries": 1, "extractor_retries": 1,
        "logger": logging.Logger("rednote-extractor", level=logging.CRITICAL),
    }
    with YoutubeDL(options) as client:
        info = client.extract_info(url, download=False, ie_key="XiaoHongShu") or {}
        return [_video_item(info, expected_id)]
