"""整理平台实际提供的媒体版本，不能用封面尺寸冒充视频分辨率。"""

import math

from .downloads import media_platform


def _number(value: object) -> float | None:
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def available_formats(candidates: list[dict], platform: str) -> list[dict]:
    formats = []
    for candidate in candidates:
        urls = candidate.get("download_url") or []
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, (list, tuple)):
            continue
        url = next((url for url in urls if isinstance(url, str) and media_platform(url) == platform), None)
        if not url:
            continue
        width = int(_number(candidate.get("width")) or 0) or None
        height = int(_number(candidate.get("height")) or 0) or None
        formats.append({
            "download_url": url, "ext": "mp4", "width": width, "height": height,
            "bitrate": _number(candidate.get("bitrate")), "fps": _number(candidate.get("fps")),
            "filesize": int(_number(candidate.get("filesize")) or 0) or None,
            "codec": candidate.get("codec"),
            "label": f"{min(width, height)}p · {width}×{height}" if width and height else "分辨率未知",
            "default_download_headers": candidate.get("default_download_headers") or {},
            **({
                "audio_download_url": candidate.get("audio_download_url"),
                "audio_download_headers": candidate.get("audio_download_headers") or {},
            } if platform == "youtube" else {}),
        })
    # 竖屏按短边标注清晰度；同尺寸只保留帧率、码率较高的版本，避免重复档位。
    formats.sort(key=lambda item: (
        min(item["width"] or 0, item["height"] or 0),
        (item["width"] or 0) * (item["height"] or 0),
        item["fps"] or 0, item["bitrate"] or 0,
    ), reverse=True)
    result, seen, seen_urls = [], set(), set()
    for item in formats:
        identity = (item["width"], item["height"]) if item["width"] and item["height"] else item["download_url"]
        if identity in seen or item["download_url"] in seen_urls:
            continue
        seen.add(identity)
        seen_urls.add(item["download_url"])
        item["format_id"] = str(len(result))
        result.append(item)
    return result
