"""FastAPI application factory and assembly.

Route order matters: business routers are registered before the static/SPA
handler so ``/auth``, ``/api``, ``/anthropic``, ``/v1``, ``/models`` and
``/admin`` always win over the catch-all that serves the web client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import admin, anthropic_compat, auth, files, me, speech, sync
from .config import VERSION, Settings, ensure_directories, load_settings
from .db import Database, now_ms
from .files import FileStore
from .models import ApiError, error_body
from .search import SearchService
from .security import Limiters
from .upstream import AnthropicUpstream, UpstreamClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("dgxchat")

APP_STARTED_AT = time.time()

#: Constant Server header; uvicorn's default leaks the stack name.
SERVER_HEADER = "dgxchat"

#: Paths that must never fall through to the SPA handler.
API_PREFIXES = (
    "auth",
    "api",
    "anthropic",
    "v1",
    "models",
    "admin/users",
    "admin/stats",
    "health",
    "version",
    "docs",
    "redoc",
    "openapi.json",
)


def _api_error_response(request: Request, exc: ApiError) -> JSONResponse:
    return exc.to_response()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    ensure_directories(settings)
    db = Database(settings.db_path)
    await db.connect()
    upstream = UpstreamClient(settings)
    await upstream.start()
    anthropic = AnthropicUpstream(settings)
    await anthropic.start()
    search_service = SearchService(settings, anthropic)
    await search_service.start()

    app.state.db = db
    app.state.files = FileStore(settings, db)
    app.state.upstream = upstream
    app.state.anthropic = anthropic
    app.state.search_service = search_service
    app.state.limiters = Limiters.from_settings(settings)
    app.state.started_at = time.time()
    logger.info(
        "DGX Chat server %s ready | data=%s | upstream=%s | search_mode=%s | registration=%s",
        VERSION,
        settings.data_dir,
        settings.upstream_base_url,
        settings.search.mode,
        settings.registration_mode,
    )
    try:
        yield
    finally:
        await search_service.close()
        await anthropic.close()
        await upstream.close()
        await db.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    docs = settings.docs_enabled
    app = FastAPI(
        title="dgxchat",
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings

    _install_cors(app, settings)
    _install_exception_handlers(app)
    _install_routers(app)
    _install_meta_routes(app)
    _install_static(app, settings)
    return app


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def _install_cors(app: FastAPI, settings: Settings) -> None:
    allow_origins = settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        # Capacitor WebViews: https://localhost, http://localhost, capacitor://localhost
        # plus any LAN / loopback origin.
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"],
        allow_headers=[
            "authorization",
            "x-api-key",
            "content-type",
            "anthropic-beta",
            "anthropic-version",
            "accept",
            "x-requested-with",
            "cache-control",
        ],
        expose_headers=[
            "x-dgx-notice",
            "x-dgx-search",
            "x-dgx-request-id",
            "x-ds-request-id",
            "content-disposition",
            "retry-after",
        ],
        allow_private_network=True,
        max_age=86400,
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["server"] = SERVER_HEADER
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["referrer-policy"] = "no-referrer"
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _api_error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        location = ""
        message = "请求参数错误"
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ()))
            message = f"{location}: {first.get('msg', message)}" if location else first.get(
                "msg", message
            )
        return JSONResponse(
            error_body(message, "invalid_request_error", param=location or None),
            status_code=400,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        if request.url.path.startswith(("/anthropic/", "/api/", "/auth/", "/admin/", "/v1/")):
            return JSONResponse(
                error_body(str(exc.detail), "not_found_error" if exc.status_code == 404 else "api_error"),
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
        return JSONResponse(
            error_body(str(exc.detail), "not_found_error" if exc.status_code == 404 else "api_error"),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        # This response is produced by Starlette's outermost error middleware, so
        # the normal header middleware never sees it: set the header explicitly.
        return JSONResponse(
            error_body("internal error", "server_error"),
            status_code=500,
            headers={"server": SERVER_HEADER, "x-content-type-options": "nosniff"},
        )


# ---------------------------------------------------------------------------
# routers
# ---------------------------------------------------------------------------


def _install_routers(app: FastAPI) -> None:
    app.include_router(auth.router)
    app.include_router(files.router)
    app.include_router(anthropic_compat.router)
    app.include_router(speech.router)
    app.include_router(me.router)
    app.include_router(sync.router)
    app.include_router(admin.router)


# ---------------------------------------------------------------------------
# meta routes
# ---------------------------------------------------------------------------


def _install_meta_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Public: liveness only.  Administrators additionally see configuration."""
        db: Database = app.state.db
        settings: Settings = app.state.settings
        ok = True
        try:
            await db.fetch_value("SELECT 1", (), 0)
        except Exception as exc:  # noqa: BLE001
            ok = False
            logger.warning("health check: database unavailable: %s", exc)
        body: dict[str, Any] = {"ok": ok, "status": "up" if ok else "degraded"}
        user = await auth.optional_user(request)
        if user is not None and user.role == "admin":
            body.update(
                {
                    "version": VERSION,
                    "uptimeSeconds": int(time.time() - APP_STARTED_AT),
                    "dataDir": str(settings.data_dir),
                    "upstream": settings.upstream_base_url,
                    "searchMode": settings.search.mode,
                    "registrationMode": settings.registration_mode,
                    "ttsProvider": settings.speech.tts.provider,
                    "asrProvider": settings.speech.asr.provider,
                    "ffmpeg": bool(settings.speech.ffmpeg_bin),
                    "docs": settings.docs_enabled,
                }
            )
        return body

    @app.get("/version")
    async def version() -> dict[str, Any]:
        return {"version": VERSION}

    async def models(request: Request) -> dict[str, Any]:
        settings: Settings = app.state.settings
        await auth.authenticate(request)
        stamp = int(time.time())
        seen: list[dict[str, Any]] = []
        for client_id, upstream_id in settings.model_map.items():
            seen.append(
                {
                    "id": client_id,
                    "object": "model",
                    "created": stamp,
                    "owned_by": "dgx",
                    "upstream": upstream_id,
                }
            )
        return {"object": "list", "data": seen}

    app.add_api_route("/models", models, methods=["GET"], tags=["models"])
    app.add_api_route("/v1/models", models, methods=["GET"], tags=["models"])

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    async def admin_page() -> HTMLResponse:
        return HTMLResponse(admin.ADMIN_PAGE_HTML)


def _python_version() -> str:
    import platform

    return platform.python_version()


# ---------------------------------------------------------------------------
# landing page + static web client
# ---------------------------------------------------------------------------


def landing_html(settings: Settings) -> str:
    """Deliberately uninformative public page (no stack, model or product hints)."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Service</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#101114; color:#cfd3da;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; }
  main { text-align:center; }
  .dot { width:10px; height:10px; border-radius:50%; background:#3ecf8e; display:inline-block;
         margin-right:9px; box-shadow:0 0 10px rgba(62,207,142,.7); }
  p { font-size:15px; letter-spacing:.2px; }
</style>
</head>
<body><main><p><span class="dot"></span>Service is running.</p></main></body>
</html>"""


def _install_static(app: FastAPI, settings: Settings) -> None:
    web_dir: Path = settings.web_dir

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> Response:
        if web_dir.is_dir():
            candidate = web_dir / "index.html"
            if candidate.is_file():
                return FileResponse(candidate, media_type="text/html; charset=utf-8")
        return HTMLResponse(landing_html(settings))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str, request: Request) -> Response:
        """Static files + SPA deep-link fallback, never swallowing API 404s."""
        cleaned = full_path.strip("/")
        if cleaned and _is_api_path(cleaned):
            raise ApiError(404, f"接口不存在：/{cleaned}", "not_found_error")
        if web_dir.is_dir() and cleaned:
            try:
                if _safe_relative(cleaned):
                    candidate = (web_dir / cleaned).resolve()
                    base = web_dir.resolve()
                    if base in candidate.parents and candidate.is_file():
                        return FileResponse(candidate)
            except (OSError, ValueError):  # pragma: no cover - defensive
                pass
        if web_dir.is_dir():
            index_file = web_dir / "index.html"
            if index_file.is_file():
                return FileResponse(index_file, media_type="text/html; charset=utf-8")
        if not cleaned:
            return HTMLResponse(landing_html(settings))
        raise ApiError(404, f"未找到：/{cleaned}", "not_found_error")


def _is_api_path(path: str) -> bool:
    first = path.split("/", 1)[0]
    return path in API_PREFIXES or first in API_PREFIXES


def _safe_relative(path: str) -> bool:
    parts = Path(path).parts
    return not any(part in ("..", "", ".") for part in parts) and not Path(path).is_absolute()


app = create_app()
