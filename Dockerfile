FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BROWSER_PATH=/usr/bin/chromium \
    BROWSER_NO_SANDBOX=1

# 浏览器用于页面抓取兜底，媒体处理和脚本运行依赖供各站点解析器使用。
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates chromium ffmpeg nodejs fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt /srv/requirements.txt
# 部分架构上的依赖需要源码编译；在同一层安装并移除工具，避免运行镜像保留编译环境。
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && pip install -r /srv/requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*
# 镜像分发时也须携带许可和版权声明，不能只在源码仓库中保留。
COPY LICENSE NOTICE /srv/
COPY app /srv/app
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/videodl_outputs \
    && chown -R appuser:appuser /srv/videodl_outputs
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
