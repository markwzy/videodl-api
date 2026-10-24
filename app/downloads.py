"""保留已解析媒体的请求头，通过短期凭证流式转发，不保存视频文件。"""

from collections import OrderedDict
from dataclasses import dataclass
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
MAX_TICKETS = 128


@dataclass(frozen=True)
class DownloadTicket:
    url: str
    filename: str
    headers: dict[str, str]
    expires_at: float
    platform: str


_tickets: OrderedDict[str, DownloadTicket] = OrderedDict()
_ticket_lock = Lock()


def _media_platform(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
            return None
        if host.endswith(".douyinvod.com"):
            return "douyin"
        if host == "video.twimg.com":
            return "twitter"
        return None
    except ValueError:
        return None


def create_download(url: str, title: str, headers: dict[str, str]) -> str | None:
    platform = _media_platform(url)
    if platform is None:
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
        _tickets[token] = DownloadTicket(url, filename, dict(headers), now + TICKET_TTL, platform)
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
            if _media_platform(url) != ticket.platform:
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


@router.get("/api/download/{token}")
def download_media(token: str, request: Request):
    ticket = _get_ticket(token)
    upstream = _open_media(ticket, request.headers.get("range"), request.headers.get("if-range"))
    if upstream.status_code == 416:
        content_range = upstream.headers.get("Content-Range")
        upstream.close()
        return Response(status_code=416, headers={"Content-Range": content_range} if content_range else {})
    if upstream.status_code not in (200, 206):
        if upstream.status_code in (401, 403, 404, 410):
            # 媒体地址失效时清除解析缓存，保证提示用户重新解析后能真正访问平台取新地址。
            clear_parse_cache()
        upstream.close()
        raise HTTPException(status_code=502, detail="视频站点拒绝下载，链接可能已过期，请重新解析")
    if not upstream.headers.get("Content-Type", "").lower().startswith("video/"):
        clear_parse_cache()
        upstream.close()
        raise HTTPException(status_code=502, detail="视频站点未返回有效视频，请重新解析")
    headers = {
        "Content-Disposition": f"attachment; filename=\"video.mp4\"; filename*=UTF-8''{quote(ticket.filename, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
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
