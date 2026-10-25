"""保留已解析媒体的请求头，通过短期凭证流式转发，不保存视频文件。"""

from collections import OrderedDict
from dataclasses import dataclass, replace
import re
import secrets
from threading import Lock
import time
from urllib.parse import quote, urljoin, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pathvalidate import sanitize_filename
import requests
from starlette.background import BackgroundTask

from .parse_cache import clear_parse_cache


router = APIRouter()
TICKET_TTL = 600
# 一次解析可能包含多条视频和多个清晰度，不能在响应生成前就挤掉前面的下载入口。
MAX_TICKETS = 1024


@dataclass(frozen=True)
class DownloadTicket:
    url: str
    filename: str
    headers: dict[str, str]
    expires_at: float
    platform: str
    audio_url: str | None = None
    audio_headers: dict[str, str] | None = None


_tickets: OrderedDict[str, DownloadTicket] = OrderedDict()
_ticket_lock = Lock()


def media_platform(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
            return None
        if host.endswith(".douyinvod.com"):
            return "douyin"
        if host == "video.twimg.com":
            return "twitter"
        if host.endswith(".googlevideo.com"):
            return "youtube"
        # 只放行小红书视频分发域名，图片、站点首页和伪装后缀不能作为下载目标。
        if re.fullmatch(r"sns-(?:video|bak)-[a-z0-9-]+\.xhscdn\.(?:com|net)", host):
            return "rednote"
        return None
    except ValueError:
        return None


def create_download(
    url: str, title: str, headers: dict[str, str], *,
    audio_url: str | None = None, audio_headers: dict[str, str] | None = None,
) -> str | None:
    platform = media_platform(url)
    if platform is None:
        return None
    # 合成的两个输入必须同时属于油管；不能借音轨地址扩大下载目标范围。
    if audio_url and (platform != "youtube" or media_platform(audio_url) != platform):
        return None
    filename = (sanitize_filename(title, platform="universal")[:120].strip(". ") or "video") + ".mp4"
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    # 解析在线程池执行，凭证必须在锁内更新；限制数量和时效，避免状态持续积累。
    with _ticket_lock:
        for key, ticket in list(_tickets.items()):
            if ticket.expires_at <= now:
                del _tickets[key]
        while len(_tickets) >= MAX_TICKETS:
            _tickets.popitem(last=False)
        _tickets[token] = DownloadTicket(url, filename, dict(headers), now + TICKET_TTL, platform, audio_url, dict(audio_headers or {}))
    return f"/api/download/{token}"


def _get_ticket(token: str) -> DownloadTicket:
    with _ticket_lock:
        ticket = _tickets.get(token)
        if ticket is None or ticket.expires_at <= time.monotonic():
            _tickets.pop(token, None)
            raise HTTPException(status_code=410, detail="下载链接已失效，请重新解析视频")
        return ticket


def _open_media(ticket: DownloadTicket, range_header: str | None, if_range: str | None):
    headers = {**ticket.headers, "Accept-Encoding": "identity"}
    if range_header:
        if not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
            raise HTTPException(status_code=416, detail="仅支持单段下载范围")
        headers["Range"] = range_header
        if if_range:
            headers["If-Range"] = if_range
    url = ticket.url
    try:
        for _ in range(5):
            # 重定向不能跨越平台边界，避免扩大下载目标或把一个平台的请求上下文发给另一个平台。
            if media_platform(url) != ticket.platform:
                raise HTTPException(status_code=502, detail="视频站点返回了不支持的媒体地址")
            response = requests.get(url, headers=headers, stream=True, timeout=(10, 30), allow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    break
                url = urljoin(url, location)
                continue
            return response
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="连接视频站点超时，请稍后重试") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="连接视频站点失败，请重新解析后重试") from exc
    raise HTTPException(status_code=502, detail="视频站点重定向异常，请重新解析")


def _validate_media(upstream, *, audio: bool = False):
    if upstream.status_code not in (200, 206):
        if upstream.status_code in (401, 403, 404, 410):
            # 地址失效后必须重新访问平台，不能让用户重试时仍命中同一份无效缓存。
            clear_parse_cache()
        upstream.close()
        raise HTTPException(status_code=502, detail="视频站点拒绝下载，链接可能已过期，请重新解析")
    if not upstream.headers.get("Content-Type", "").lower().startswith("audio/" if audio else "video/"):
        clear_parse_cache()
        upstream.close()
        raise HTTPException(status_code=502, detail="视频站点未返回有效媒体，请重新解析")


def _merged_download(ticket: DownloadTicket, headers: dict):
    from .mux import MuxStream

    video = _open_media(ticket, None, None)
    audio = stream = None
    try:
        _validate_media(video)
        audio = _open_media(replace(ticket, url=ticket.audio_url, headers=ticket.audio_headers), None, None)
        _validate_media(audio, audio=True)
        stream = MuxStream(video, audio)
        # 启动失败必须在发送成功响应前报告，不能下载出一个错误页面冒充的视频文件。
        first = next(stream)
    except Exception as exc:
        if stream:
            stream.close()
        video.close()
        if audio is not None:
            audio.close()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=502, detail="音视频合成失败，请重新解析后下载") from exc

    def chunks():
        try:
            yield first
            yield from stream
        finally:
            stream.close()

    # 合成文件没有稳定的字节偏移，忽略范围请求并返回完整流，不能伪造断点续传能力。
    return StreamingResponse(chunks(), media_type="video/mp4", headers={**headers, "Accept-Ranges": "none"}, background=BackgroundTask(stream.close))


@router.get("/api/download/{token}")
def download_media(token: str, request: Request):
    ticket = _get_ticket(token)
    headers = {
        "Content-Disposition": f"attachment; filename=\"video.mp4\"; filename*=UTF-8''{quote(ticket.filename, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if ticket.audio_url:
        return _merged_download(ticket, headers)
    upstream = _open_media(ticket, request.headers.get("range"), request.headers.get("if-range"))
    if upstream.status_code == 416:
        content_range = upstream.headers.get("Content-Range")
        upstream.close()
        return Response(status_code=416, headers={"Content-Range": content_range} if content_range else {})
    _validate_media(upstream)
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"):
        if value := upstream.headers.get(name):
            headers[name] = value

    def chunks():
        try:
            # 每次只转发一个小块，大视频不会先装入内存，也不会写入容器磁盘。
            yield from upstream.iter_content(chunk_size=64 * 1024)
        finally:
            upstream.close()

    return StreamingResponse(
        chunks(), status_code=upstream.status_code,
        media_type=upstream.headers["Content-Type"], headers=headers,
        background=BackgroundTask(upstream.close),
    )
