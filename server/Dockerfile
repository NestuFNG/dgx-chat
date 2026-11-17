# DGX Chat backend — production image.
#
# python:3.12-slim, no compiler toolchain in any layer (all dependencies are
# wheels), ffmpeg available only for the openai/custom speech providers, and the
# process drops to an unprivileged user via docker-entrypoint.sh.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DGXCHAT_DATA_DIR=/data \
    DGXCHAT_HOST=0.0.0.0 \
    DGXCHAT_PORT=8439 \
    WEB_DIR=/app/web \
    PATH="/home/dgxchat/.local/bin:${PATH}"

# ffmpeg: required to transcode non-PCM speech-provider output to 24 kHz mono
# s16le.  The built-in `provider=deepseek` path needs no transcoding at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin dgxchat \
    && mkdir -p /data /app/web \
    && chown -R dgxchat:dgxchat /data /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8439

# Liveness only: /health is intentionally minimal for anonymous callers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8439/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8439", \
     "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
