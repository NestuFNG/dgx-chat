"""Cloud sync API used by the client only (SPEC §2.3).

Sessions are stored as the client's own JSON (opaque to this server) next to
denormalised columns for listing.  ``user_id`` is part of every primary key, so
one user can never read or overwrite another user's rows.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .auth import authenticate
from .db import Database, now_ms
from .files import FileStore, file_response_headers, sanitize_local_id
from .models import ApiError, not_found
from . import credentials
from .security import check_storage_quota, reject_oversized_content_length

router = APIRouter(tags=["sync"])

SETTINGS_MAX_BYTES = 64 * 1024
#: Tombstones older than this are purged opportunistically on sync calls.
TOMBSTONE_RETENTION_MS = 90 * 24 * 3600 * 1000


def _coerce_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return fallback
    return fallback


def _summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "createdAt": int(row["created_at"]),
        "updatedAt": int(row["updated_at"]),
    }


async def _read_json(request: Request, limit: int) -> Any:
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError(413, "请求体过大", "request_too_large")
    if not raw:
        raise ApiError(400, "请求体为空", "invalid_request_error")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"请求体不是合法 JSON：{exc.msg}", "invalid_request_error") from exc


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


@router.get("/api/sessions")
async def list_sessions(request: Request) -> list[dict[str, Any]]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    rows = await db.fetch_all(
        "SELECT id, title, created_at, updated_at FROM session_summaries"
        " WHERE user_id = ? ORDER BY updated_at DESC, id ASC",
        (user.id,),
    )
    return [_summary(row) for row in rows]


@router.get("/api/sessions/sync")
async def sync_sessions(request: Request, since: int = 0) -> dict[str, Any]:
    """Delta sync for multi-device clients.

    Returns sessions changed after ``since`` plus tombstones for sessions deleted
    after ``since``, so another device can drop them locally.  The existing
    ``GET /api/sessions`` shape is untouched.
    """
    user = await authenticate(request)
    db: Database = request.app.state.db
    since_ms = max(0, int(since or 0))
    server_time = now_ms()

    # Opportunistic retention: drop tombstones nobody can still care about.
    await db.execute(
        "DELETE FROM deleted_sessions WHERE user_id = ? AND deleted_at < ?",
        (user.id, server_time - TOMBSTONE_RETENTION_MS),
    )

    rows = await db.fetch_all(
        "SELECT id, title, created_at, updated_at FROM session_summaries"
        " WHERE user_id = ? AND updated_at > ? ORDER BY updated_at ASC",
        (user.id, since_ms),
    )
    deleted = await db.fetch_all(
        "SELECT session_id FROM deleted_sessions"
        " WHERE user_id = ? AND deleted_at > ? ORDER BY deleted_at ASC",
        (user.id, since_ms),
    )
    return {
        "sessions": [_summary(row) for row in rows],
        "deleted": [str(row["session_id"]) for row in deleted],
        "serverTime": server_time,
        "since": since_ms,
    }


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> Response:
    user = await authenticate(request)
    db: Database = request.app.state.db
    row = await db.fetch_one(
        "SELECT data FROM sessions WHERE user_id = ? AND id = ?", (user.id, session_id)
    )
    if row is None:
        raise not_found(f"会话不存在：{session_id}")
    return Response(content=row["data"], media_type="application/json; charset=utf-8")


@router.put("/api/sessions/{session_id}")
async def put_session(session_id: str, request: Request) -> JSONResponse:
    user = await authenticate(request)
    db: Database = request.app.state.db
    settings = request.app.state.settings
    if not session_id or len(session_id) > 200:
        raise ApiError(400, "非法的会话 id", "invalid_request_error", param="id")

    reject_oversized_content_length(request, settings.max_json_body_bytes)
    raw = await request.body()
    if len(raw) > settings.max_json_body_bytes:
        raise ApiError(413, "会话过大", "request_too_large")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"会话不是合法 JSON：{exc.msg}", "invalid_request_error") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "会话必须是 JSON 对象", "invalid_request_error")
    body_id = body.get("id")
    if body_id is not None and str(body_id) != session_id:
        raise ApiError(400, "路径 id 与请求体 id 不一致", "invalid_request_error", param="id")
    messages = body.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ApiError(400, "messages 必须是数组", "invalid_request_error", param="messages")

    stamp = now_ms()
    created_at = _coerce_int(body.get("createdAt"), stamp)
    updated_at = _coerce_int(body.get("updatedAt"), stamp)
    title = body.get("title")
    title = title if isinstance(title, str) else ""
    stored_text = raw.decode("utf-8")

    existing = await db.fetch_one(
        "SELECT updated_at FROM sessions WHERE user_id = ? AND id = ?", (user.id, session_id)
    )
    if existing is not None and int(existing["updated_at"]) > updated_at:
        # Last-write-wins by updatedAt: keep the newer cloud copy.
        row = await db.fetch_one(
            "SELECT id, title, created_at, updated_at FROM session_summaries"
            " WHERE user_id = ? AND id = ?",
            (user.id, session_id),
        )
        payload = _summary(row) if row else {
            "id": session_id,
            "title": title,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }
        payload["conflict"] = True
        return JSONResponse(payload)

    await db.execute(
        "INSERT INTO sessions (user_id, id, title, created_at, updated_at, data)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, id) DO UPDATE SET title=excluded.title,"
        " created_at=excluded.created_at, updated_at=excluded.updated_at, data=excluded.data",
        (user.id, session_id, title, created_at, updated_at, stored_text),
    )
    await db.execute(
        "INSERT INTO session_summaries (user_id, id, title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(user_id, id) DO UPDATE SET title=excluded.title,"
        " created_at=excluded.created_at, updated_at=excluded.updated_at",
        (user.id, session_id, title, created_at, updated_at),
    )
    return JSONResponse(
        {"id": session_id, "title": title, "createdAt": created_at, "updatedAt": updated_at}
    )


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    existing = await db.fetch_one(
        "SELECT 1 FROM sessions WHERE user_id = ? AND id = ?", (user.id, session_id)
    )
    stamp = now_ms()
    await db.execute("DELETE FROM sessions WHERE user_id = ? AND id = ?", (user.id, session_id))
    await db.execute(
        "DELETE FROM session_summaries WHERE user_id = ? AND id = ?", (user.id, session_id)
    )
    # Tombstone even when the row was already gone: another device may still hold
    # a cached copy and must learn about the deletion.
    await db.execute(
        "INSERT INTO deleted_sessions (user_id, session_id, deleted_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id, session_id) DO UPDATE SET deleted_at = excluded.deleted_at",
        (user.id, session_id, stamp),
    )
    return {"ok": True, "id": session_id, "deleted": existing is not None, "deletedAt": stamp}


# ---------------------------------------------------------------------------
# client-local attachments
# ---------------------------------------------------------------------------


@router.get("/api/files/{local_id}")
async def get_client_file(local_id: str, request: Request) -> Response:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    found = await store.read_client_file(user.id, local_id)
    if found is None:
        raise not_found(f"附件不存在：{local_id}")
    data, meta = found
    mime = meta["mime_type"] or "application/octet-stream"
    headers = file_response_headers(mime, meta["id"])
    headers["x-dgx-sha256"] = meta["sha256"]
    return Response(content=data, media_type=mime, headers=headers)


@router.put("/api/files/{local_id}")
async def put_client_file(local_id: str, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    settings = request.app.state.settings
    db: Database = request.app.state.db
    sanitize_local_id(local_id)
    reject_oversized_content_length(request, settings.max_upload_bytes)
    data = await request.body()
    if len(data) > settings.max_upload_bytes:
        raise ApiError(413, "附件过大", "request_too_large")
    if not data:
        raise ApiError(400, "附件内容为空", "invalid_request_error")
    existing = await db.fetch_one(
        "SELECT size_bytes FROM client_files WHERE user_id = ? AND local_id = ?",
        (user.id, local_id),
    )
    previous = int(existing["size_bytes"]) if existing else 0
    await check_storage_quota(db, settings, user.id, max(0, len(data) - previous))
    meta = await store.save_client_file(
        user.id, local_id, data, request.headers.get("content-type")
    )
    return {"ok": True, **meta}


@router.delete("/api/files/{local_id}")
async def delete_client_file(local_id: str, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    store: FileStore = request.app.state.files
    deleted = await store.delete_client_file(user.id, local_id)
    return {"ok": True, "id": local_id, "deleted": deleted}


# ---------------------------------------------------------------------------
# per-user settings
# ---------------------------------------------------------------------------


@router.get("/api/me/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Masked view: credential api_key values are never returned in full."""
    user = await authenticate(request)
    db: Database = request.app.state.db
    row = await db.fetch_one("SELECT data FROM user_settings WHERE user_id = ?", (user.id,))
    if row is None:
        return credentials.public_settings({})
    try:
        parsed = json.loads(row["data"])
    except json.JSONDecodeError:
        return credentials.public_settings({})
    return credentials.public_settings(parsed)


@router.put("/api/me/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    """Merge settings; credential fields accept values, "" to clear, __keep__.

    Returns the same masked document as ``GET`` so the UI can re-render directly.
    """
    user = await authenticate(request)
    db: Database = request.app.state.db
    reject_oversized_content_length(request, SETTINGS_MAX_BYTES)
    raw = await request.body()
    if len(raw) > SETTINGS_MAX_BYTES:
        raise ApiError(413, "设置过大", "request_too_large")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"设置不是合法 JSON：{exc.msg}", "invalid_request_error") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "设置必须是 JSON 对象", "invalid_request_error")

    row = await db.fetch_one("SELECT data FROM user_settings WHERE user_id = ?", (user.id,))
    existing: dict[str, Any] = {}
    if row is not None:
        try:
            parsed = json.loads(row["data"])
            existing = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            existing = {}
    merged = credentials.merge_settings(existing, body)
    serialized = json.dumps(merged, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > SETTINGS_MAX_BYTES:
        raise ApiError(413, "设置过大", "request_too_large")
    await db.execute(
        "INSERT INTO user_settings (user_id, data, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (user.id, serialized, now_ms()),
    )
    return credentials.public_settings(merged)


@router.get("/api/me")
async def get_me(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    return user.to_out().model_dump()
