"""Local-only video URL parser powered by CharlesPikachu/videodl.

The service only returns metadata/stream URLs: it does NOT save media.
For safety, docker-compose binds its port to 127.0.0.1 only.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .downloads import create_download, router as downloads_router


DEFAULT_SOURCES = (
    "DouyinVideoClient,BilibiliVideoClient,RednoteVideoClient,"
    "KuaishouVideoClient,YouTubeVideoClient,WeiboVideoClient,"
    "XiguaVideoClient,ZhihuVideoClient,TencentVideoClient,YoukuVideoClient,TwitterVideoClient"
)
MAX_ITEMS = 20
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "90"))
# 浏览器可能占用上百个线程，串行解析可避免超过容器的进程限制；超时线程不能安全中断。
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="videodl")
# 官方接口不启动浏览器，单独排队可避免 X 请求被较慢的网页加载阻塞。
API_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="platform-api")
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com"}
URL_PATTERN = re.compile(r'https?://[^\s<>\u3000"\'“”‘’]+', flags=re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;!?)\uFF0C\u3002\uFF1B\uFF01\uFF1F\uFF09\u300B\u300D\u3011\u3010\u300C\u3008\u2026"
APP_ROOT = Path(__file__).parent

app = FastAPI(title="VideoDL Parse API", version="1.0.0", description="Parse public video/share URLs with videodl; does not download files.")
app.include_router(downloads_router)


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000, description="A link, or a share message containing a link")


def _is_public_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        # Ban credentials, localhost, single-label names, internal TLDs and
        # literal private/non-global IPs. DNS rebinding/redirects are NOT
        # comprehensively prevented by this check: do not expose publicly.
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return False
        if parts.port is not None and parts.port not in {80, 443}:
            return False
        host = host.rstrip(".").lower()
        if host in {"localhost", "host.docker.internal"} or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def extract_video_url(text: str) -> str:
    """Select the first valid public web URL from copied share text."""
    found = URL_PATTERN.findall(text)
    if not found:
        raise HTTPException(status_code=422, detail="没有找到 http:// 或 https:// 视频链接")
    for match in found:
        candidate = match.strip().rstrip(TRAILING_PUNCTUATION)
        if _is_public_http_url(candidate):
            return candidate
    raise HTTPException(status_code=422, detail="链接地址无效或指向本地/内网，已拒绝解析")


def _clean_media_url(value: object) -> str | None:
    if isinstance(value, (list, tuple)):
        value = next((s for s in value if isinstance(s, str) and _is_public_http_url(s)), None)
    if not isinstance(value, str):
        return None
    return value if _is_public_http_url(value) else None


def _get(source: object, key: str, default: object = None) -> object:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _result_item(info: object) -> dict:
    video = _clean_media_url(_get(info, "download_url"))
    audio = _clean_media_url(_get(info, "audio_download_url"))
    cover = _clean_media_url(_get(info, "cover_url"))
    ext = str(_get(info, "ext") or "").strip(".").lower()
    hls = bool(video and urlsplit(video).path.lower().endswith(".m3u8")) or ext == "m3u8"
    result = {
        "source": str(_get(info, "source") or ""),
        "title": str(_get(info, "title") or "未命名视频"),
        "identifier": str(_get(info, "identifier") or ""),
        "cover_url": cover,
        "download_url": video,
        "audio_download_url": audio,
        "ext": ext,
        "is_hls": hls,
        "requires_merge": bool(video and audio),
        "may_require_headers": bool(_get(info, "default_download_headers") or _get(info, "default_download_cookies")),
    }
    # 请求头仅保留在服务内部，浏览器只拿到短期下载凭证，不能自行指定代理目标。
    result["local_download_url"] = None
    if video and result["source"] in {"DouyinBrowser", "TwitterYtDlp"}:
        result["local_download_url"] = create_download(
            video, result["title"], _get(info, "default_download_headers") or {},
        )
    return result


def _parse_blocking(url: str) -> list[dict]:
    raw_sources = os.getenv("VIDEO_SOURCES", DEFAULT_SOURCES).strip()
    # 星号表示主动启用所有解析器，包含可能向第三方发送链接的通用解析器。
    sources = [] if raw_sources == "*" else [part.strip() for part in raw_sources.split(",") if part.strip()]
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host == "douyin.com" or host.endswith(".douyin.com"):
        from .douyin import parse_douyin

        # 抖音直接读取目标作品详情，不再使用失效的分享页解析或扫描整页素材。
        infos = parse_douyin(url) if not sources or "DouyinVideoClient" in sources else []
    elif host in TWITTER_HOSTS:
        from .twitter import parse_twitter

        # 登录受限内容不能落入匿名页面抓取，否则容易超时或误取页面素材。
        infos = parse_twitter(url, MAX_ITEMS) if not sources or "TwitterVideoClient" in sources else []
    else:
        # 本项目独立实现的解析器名称不能传给上游注册表，空列表也不能被误解为启用全部平台。
        upstream_sources = [source for source in sources if source != "TwitterVideoClient"]
        if sources and not upstream_sources:
            return []
        # 依赖较重，延迟导入可避免接口启动和离线测试加载全部站点插件。
        from videodl.videodl import VideoClient
        from .browser import SystemMediaGrabber

        # 上游要求可迭代对象，不能传入空值。
        client = VideoClient(allowed_video_sources=upstream_sources)
        # 直接启动系统浏览器，保留上游的媒体提取与校验逻辑。
        client.web_media_grabber = SystemMediaGrabber(
            browser_path="/usr/bin/chromium", enable_scroll=False, browser_timeout_sec=15,
        )
        infos = client.parsefromurl(url) or []
        # 上游可能返回包含错误的非空列表，不能把列表非空当作解析成功。
        # 空列表表示上游已经执行过页面抓取，只有非空无效结果才需要补上这一步。
        if infos and not any(_clean_media_url(_get(item, "download_url")) for item in infos):
            infos = client.web_media_grabber.parsefromurl(url) or []
    results: list[dict] = []
    for item in infos:
        record = _result_item(item)
        if record["download_url"]:
            results.append(record)
        if len(results) >= MAX_ITEMS:
            break
    return results


@app.get("/")
def homepage():
    return FileResponse(APP_ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "videodl-api"}


@app.post("/api/parse")
async def parse(req: ParseRequest):
    url = extract_video_url(req.text)
    try:
        loop = asyncio.get_running_loop()
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        executor = API_EXECUTOR if host in TWITTER_HOSTS else EXECUTOR
        results = await asyncio.wait_for(loop.run_in_executor(executor, _parse_blocking, url), timeout=PARSE_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="解析超时，请稍后再试") from exc
    except HTTPException:
        # 保留平台边界已经处理过的登录、配置和内容错误，不能统一包装成服务器故障。
        raise
    except Exception as exc:
        # Don't echo secret query params or site-specific tracebacks to browser.
        raise HTTPException(status_code=502, detail=f"解析器执行失败（{type(exc).__name__}），请检查容器日志") from exc
    if not results:
        raise HTTPException(status_code=502, detail="未解析到有效媒体地址；页面可能需要登录或验证，视频也可能已失效")
    return {
        "success": bool(results),
        "input_url": url,
        "count": len(results),
        "items": results,
        "message": "解析成功",
    }
