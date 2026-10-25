"""Local-only video URL parser powered by CharlesPikachu/videodl.

The service only returns metadata/stream URLs: it does NOT save media.
For safety, docker-compose binds its port to 127.0.0.1 only.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .downloads import create_download, router as downloads_router
from .jobs import ParseJobs
from .parse_cache import canonical_url


DEFAULT_SOURCES = (
    "DouyinVideoClient,BilibiliVideoClient,RednoteVideoClient,"
    "KuaishouVideoClient,YouTubeVideoClient,WeiboVideoClient,"
    "XiguaVideoClient,ZhihuVideoClient,TencentVideoClient,YoukuVideoClient,TwitterVideoClient"
)
MAX_ITEMS = 20
PARSE_TIMEOUT = int(os.getenv("PARSE_TIMEOUT", "90"))
PARSE_QUEUE_SIZE = int(os.getenv("PARSE_QUEUE_SIZE", "8"))
PARSE_QUEUE_TIMEOUT = int(os.getenv("PARSE_QUEUE_TIMEOUT", "30"))
TWITTER_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
REDNOTE_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com", "www.xhslink.com"}
URL_PATTERN = re.compile(r'https?://[^\s<>\u3000"\'“”‘’]+', flags=re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;!?)\uFF0C\u3002\uFF1B\uFF01\uFF1F\uFF09\u300B\u300D\u3011\u3010\u300C\u3008\u2026"
APP_ROOT = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.parse_jobs = ParseJobs(
        lambda url: _parse_blocking(url), queue_size=PARSE_QUEUE_SIZE,
        queue_timeout=PARSE_QUEUE_TIMEOUT, parse_timeout=PARSE_TIMEOUT,
    )
    try:
        yield
    finally:
        from .browser import DOUYIN_BROWSER

        await app.state.parse_jobs.close()
        await asyncio.to_thread(DOUYIN_BROWSER.close)


app = FastAPI(title="VideoDL Parse API", version="1.0.0", description="Parse public video/share URLs with videodl; does not download files.", lifespan=lifespan)
app.include_router(downloads_router)


class ParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000, description="A link, or a share message containing a link")
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{20,64}$", description="同一次提交重试时复用的随机标识")


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
    # 每个版本单独生成凭证，切换清晰度不必再次解析；请求头始终保留在服务内部。
    result["formats"] = []
    result["local_download_url"] = None
    if result["source"] in {"DouyinBrowser", "TwitterYtDlp", "YouTubeYtDlp", "RednoteYtDlp"}:
        for variant in _get(info, "formats", []):
            media_url = _clean_media_url(variant["download_url"])
            if not media_url:
                continue
            ticket = create_download(
                media_url, f'[{variant["label"]}] {result["title"]}', variant["default_download_headers"],
                audio_url=variant.get("audio_download_url"), audio_headers=variant.get("audio_download_headers"),
            )
            if not ticket:
                continue
            result["formats"].append({
                **{key: variant[key] for key in ("format_id", "label", "width", "height", "fps", "bitrate", "filesize", "codec", "ext")},
                "download_url": media_url, "local_download_url": ticket,
                "requires_merge": bool(variant.get("audio_download_url")),
            })
        if result["formats"]:
            result["download_url"] = result["formats"][0]["download_url"]
            result["local_download_url"] = result["formats"][0]["local_download_url"]
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
    elif host in YOUTUBE_HOSTS:
        from .youtube import parse_youtube

        # 官方提取器绑定目标视频，失败时不能再扫描网页广告或推荐视频。
        infos = parse_youtube(url) if not sources or "YouTubeVideoClient" in sources else []
    elif host in REDNOTE_HOSTS:
        from .rednote import parse_rednote

        # 旧解析器依赖失效的页面结尾匹配，改用现有依赖的专用提取器，避免整页扫描误取素材。
        infos = parse_rednote(url) if not sources or "RednoteVideoClient" in sources else []
    else:
        # 本项目独立实现的解析器名称不能传给上游注册表，空列表也不能被误解为启用全部平台。
        upstream_sources = [source for source in sources if source not in {"TwitterVideoClient", "YouTubeVideoClient", "RednoteVideoClient"}]
        if sources and not upstream_sources:
            return []
        # 依赖较重，延迟导入可避免接口启动和离线测试加载全部站点插件。
        from videodl.videodl import VideoClient
        from .browser import DOUYIN_BROWSER, SystemMediaGrabber

        # 其他平台可能启动自己的浏览器，先释放空闲进程，避免叠加后超过容器进程限制。
        DOUYIN_BROWSER.close()
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


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    # 只开放固定图标文件，避免为一个图标暴露整个静态目录。
    return FileResponse(APP_ROOT / "static" / "favicon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "videodl-api"}


@app.post("/api/parse", status_code=202)
async def parse(req: ParseRequest, request: Request, response: Response):
    url = extract_video_url(req.text)
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    platform = "twitter" if host in TWITTER_HOSTS else "youtube" if host in YOUTUBE_HOSTS else "rednote" if host in REDNOTE_HOSTS else "douyin" if host == "douyin.com" or host.endswith(".douyin.com") else "other"
    identity = None
    if platform == "twitter" and (cookie_file := os.getenv("X_COOKIES_FILE", "").strip()):
        try:
            # 与元数据缓存一样按登录内容隔离，换账号后不能复用上个账号尚未完成的任务。
            identity = hashlib.sha256(Path(cookie_file).read_bytes()).digest()
        except OSError:
            raise HTTPException(status_code=503, detail="无法读取 X 登录文件，请检查本机文件和只读挂载")
    key = (canonical_url(platform, url), os.getenv("VIDEO_SOURCES", DEFAULT_SOURCES), identity)
    task = request.app.state.parse_jobs.submit(url, "api" if platform in {"twitter", "youtube", "rednote"} else "browser", key, req.request_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = f'/api/tasks/{task["id"]}'
    return task


@app.get("/api/tasks/{task_id}")
async def get_parse_task(task_id: str, request: Request, response: Response):
    # 任务标识同时是查询凭证，响应禁止缓存，避免过期状态或媒体入口被中间层复用。
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.parse_jobs.get(task_id)
