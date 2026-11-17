# Self-contained DGX Chat image: backend + prebuilt web client.
#
# Build from the repository root (not from server/):
#     docker build -t dgx-chat .
#     docker run -d -p 8439:8439 -v dgxchat-data:/data dgx-chat
#
# This image bundles the web UI, so it is usable on its own. The compose file in
# server/ still mounts the UI from the host for development.

# ---- stage 1: build the web client -----------------------------------------
FROM node:20-bookworm-slim AS web

WORKDIR /src/app
COPY app/package.json app/pnpm-lock.yaml ./
RUN corepack enable && corepack prepare pnpm@9 --activate \
    && pnpm install --frozen-lockfile --prod=false
COPY app/ ./
RUN node scripts/build.mjs

# ---- stage 2: runtime ------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DGXCHAT_DATA_DIR=/data \
    DGXCHAT_HOST=0.0.0.0 \
    DGXCHAT_PORT=8439 \
    WEB_DIR=/app/web

# ffmpeg transcodes non-PCM speech-provider output; the built-in `deepseek`
# provider returns ready-to-use PCM and never touches it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server/requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY server/app ./app
COPY server/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=web /src/app/dist ./web

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin dgxchat \
    && mkdir -p /data \
    && chown -R dgxchat:dgxchat /data /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8439

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8439/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8439", \
     "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
