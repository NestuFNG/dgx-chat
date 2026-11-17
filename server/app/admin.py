"""Admin API (SPEC §2.4) plus a dependency-free admin web page.

The page is intentionally a single self-contained HTML document: it must work
on a LAN with no CDN, no build step and no external font/JS.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import audit
from .auth import authenticate, create_user, find_user_by_id, hash_password, require_admin
from .config import VERSION
from .db import Database, now_ms
from .files import FileStore
from .models import AdminUserCreate, ApiError, ResetPasswordRequest, forbidden, not_found
from .security import storage_usage

router = APIRouter(tags=["admin"], prefix="/admin")


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    note: str | None = Field(default=None, max_length=200)
    max_uses: int = Field(default=1, ge=0, le=1000)
    expires_at: int | None = None  # epoch milliseconds
    expires_in_hours: int | None = Field(default=None, ge=1, le=24 * 365)


def _invite_out(row: Any) -> dict[str, Any]:
    max_uses = int(row["max_uses"] or 0)
    used = int(row["used_count"] or 0)
    return {
        "code": row["code"],
        "note": row["note"] or "",
        "maxUses": max_uses,
        "usedCount": used,
        "usesLeft": None if max_uses == 0 else max(0, max_uses - used),
        "expiresAt": int(row["expires_at"]) if row["expires_at"] is not None else None,
        "revoked": bool(row["revoked"]),
        "createdBy": row["created_by"],
        "createdAt": int(row["created_at"]),
    }


@router.get("/users")
async def list_users(request: Request) -> dict[str, Any]:
    await require_admin(request)
    db: Database = request.app.state.db
    rows = await db.fetch_all(
        "SELECT u.id, u.username, u.role, u.created_at, u.disabled,"
        " (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS session_count,"
        " (SELECT COUNT(*) FROM files f WHERE f.user_id = u.id AND f.deleted = 0) AS file_count"
        " FROM users u ORDER BY u.created_at ASC"
    )
    users = [
        {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "createdAt": int(row["created_at"]),
            "disabled": bool(row["disabled"]),
            "sessionCount": int(row["session_count"] or 0),
            "fileCount": int(row["file_count"] or 0),
        }
        for row in rows
    ]
    return {"users": users, "total": len(users)}


@router.post("/users")
async def create_admin_user(payload: AdminUserCreate, request: Request) -> JSONResponse:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    settings = request.app.state.settings
    user = await create_user(db, settings, payload.username, payload.password, payload.role)
    await audit.record(
        request,
        audit.USER_CREATED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user.id,
        detail={"username": user.username, "role": user.role, "via": "admin"},
        db=db,
        settings=settings,
    )
    return JSONResponse({"id": user.id, "username": user.username, "role": user.role,
                         "createdAt": user.created_at}, status_code=201)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    store: FileStore = request.app.state.files
    target = await find_user_by_id(db, user_id)
    if target is None:
        raise not_found(f"用户不存在：{user_id}")
    if target.id == admin.id:
        raise ApiError(400, "不能删除当前登录的管理员账号", "invalid_request_error")
    if target.role == "admin":
        admins = await db.fetch_value(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0", (), 0
        )
        if int(admins or 0) <= 1:
            raise ApiError(400, "不能删除最后一个管理员", "invalid_request_error")
    await store.delete_all_for_user(user_id)
    await db.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM session_summaries WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM client_files WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM deleted_sessions WHERE user_id = ?", (user_id,))
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await audit.record(
        request,
        audit.USER_DELETED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user_id,
        detail={"username": target.username, "role": target.role},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "id": user_id, "deleted": True}


@router.post("/users/{user_id}/disable")
async def disable_user(user_id: str, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    target = await find_user_by_id(db, user_id)
    if target is None:
        raise not_found(f"用户不存在：{user_id}")
    if target.id == admin.id:
        raise ApiError(400, "不能禁用当前登录的账号", "invalid_request_error")
    if target.role == "admin":
        admins = await db.fetch_value(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0", (), 0
        )
        if int(admins or 0) <= 1:
            raise ApiError(400, "不能禁用最后一个管理员", "invalid_request_error")
    await db.execute(
        "UPDATE users SET disabled = 1, updated_at = ? WHERE id = ?", (now_ms(), user_id)
    )
    counted = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM tokens WHERE user_id = ? AND revoked = 0", (user_id,)
    )
    revoked = int(counted["n"]) if counted else 0
    await db.execute("UPDATE tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    await audit.record(
        request,
        audit.USER_DISABLED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user_id,
        detail={"username": target.username, "revokedTokens": revoked},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "id": user_id, "disabled": True, "revokedTokens": revoked}


@router.post("/users/{user_id}/enable")
async def enable_user(user_id: str, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    target = await find_user_by_id(db, user_id)
    if target is None:
        raise not_found(f"用户不存在：{user_id}")
    await db.execute(
        "UPDATE users SET disabled = 0, updated_at = ? WHERE id = ?", (now_ms(), user_id)
    )
    await audit.record(
        request,
        audit.USER_ENABLED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user_id,
        detail={"username": target.username},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "id": user_id, "disabled": False}


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str, request: Request, payload: ResetPasswordRequest | None = None
) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    settings = request.app.state.settings
    target = await find_user_by_id(db, user_id)
    if target is None:
        raise not_found(f"用户不存在：{user_id}")
    generated = not (payload and payload.password)
    password = (payload.password if payload else None) or secrets.token_urlsafe(12)
    if len(password) < settings.password_min_length:
        raise ApiError(
            400,
            f"密码至少 {settings.password_min_length} 个字符",
            "invalid_request_error",
            param="password",
        )
    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(password, n=settings.scrypt_n), now_ms(), user_id),
    )
    # Password rotation invalidates every existing token for that user.
    await db.execute("UPDATE tokens SET revoked = 1 WHERE user_id = ?", (user_id,))
    await audit.record(
        request,
        audit.PASSWORD_RESET,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user_id,
        detail={"username": target.username, "mode": "generated" if generated else "explicit"},
        db=db,
        settings=settings,
    )
    return {"ok": True, "id": user_id, "password": password, "generated": generated}


# ---------------------------------------------------------------------------
# invite codes
# ---------------------------------------------------------------------------


@router.post("/invites")
async def create_invite(payload: InviteCreate, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    code = f"dgx-{secrets.token_hex(6)}"
    expires_at = payload.expires_at
    if payload.expires_in_hours is not None:
        expires_at = now_ms() + payload.expires_in_hours * 3_600_000
    if expires_at is not None and expires_at <= now_ms():
        raise ApiError(400, "过期时间必须晚于当前时间", "invalid_request_error", param="expires_at")
    await db.execute(
        "INSERT INTO invites (code, note, created_by, created_at, expires_at, max_uses,"
        " used_count, revoked) VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (
            code,
            (payload.note or "")[:200],
            admin.id,
            now_ms(),
            expires_at,
            payload.max_uses,
        ),
    )
    row = await db.fetch_one("SELECT * FROM invites WHERE code = ?", (code,))
    await audit.record(
        request,
        audit.INVITE_CREATED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=code,
        detail={"note": payload.note or "", "maxUses": payload.max_uses,
                "expiresAt": expires_at},
        db=db,
        settings=request.app.state.settings,
    )
    return _invite_out(row) if row else {"code": code}


@router.get("/invites")
async def list_invites(request: Request) -> dict[str, Any]:
    await require_admin(request)
    db: Database = request.app.state.db
    rows = await db.fetch_all("SELECT * FROM invites ORDER BY created_at DESC")
    invites = [_invite_out(row) for row in rows]
    return {"invites": invites, "total": len(invites)}


@router.delete("/invites/{code}")
async def revoke_invite(code: str, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    existing = await db.fetch_one("SELECT code FROM invites WHERE code = ?", (code,))
    if existing is None:
        raise not_found("邀请码不存在")
    await db.execute("UPDATE invites SET revoked = 1 WHERE code = ?", (code,))
    await audit.record(
        request,
        audit.INVITE_REVOKED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=code,
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "code": code, "revoked": True}


@router.post("/users/{user_id}/revoke-tokens")
async def revoke_tokens(user_id: str, request: Request) -> dict[str, Any]:
    admin = await require_admin(request)
    db: Database = request.app.state.db
    target = await find_user_by_id(db, user_id)
    if target is None:
        raise not_found(f"用户不存在：{user_id}")
    counted = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM tokens WHERE user_id = ? AND revoked = 0", (user_id,)
    )
    revoked = int(counted["n"]) if counted else 0
    await db.execute(
        "UPDATE tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)
    )
    await audit.record(
        request,
        audit.TOKENS_REVOKED,
        actor_id=admin.id,
        actor_name=admin.username,
        target=user_id,
        detail={"username": target.username, "revokedTokens": revoked},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "id": user_id, "revokedTokens": revoked}


@router.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    await require_admin(request)
    db: Database = request.app.state.db

    async def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(await db.fetch_value(sql, params, 0) or 0)

    users = await count("SELECT COUNT(*) FROM users")
    sessions = await count("SELECT COUNT(*) FROM sessions")
    files_count = await count("SELECT COUNT(*) FROM files WHERE deleted = 0")
    file_bytes = int(
        await db.fetch_value(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM files WHERE deleted = 0", (), 0
        )
        or 0
    )
    client_files = await count("SELECT COUNT(*) FROM client_files")
    requests = await count("SELECT COUNT(*) FROM usage_log")
    prompt_tokens = int(
        await db.fetch_value("SELECT COALESCE(SUM(input_tokens), 0) FROM usage_log", (), 0) or 0
    )
    completion_tokens = int(
        await db.fetch_value("SELECT COALESCE(SUM(output_tokens), 0) FROM usage_log", (), 0) or 0
    )
    errors = await count("SELECT COUNT(*) FROM usage_log WHERE status != 'ok'")
    invites_active = await count(
        "SELECT COUNT(*) FROM invites WHERE revoked = 0 AND (expires_at IS NULL OR expires_at > ?)"
        " AND (max_uses = 0 OR used_count < max_uses)",
        (now_ms(),),
    )
    per_user = await db.fetch_all(
        "SELECT user_id, COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(output_tokens),0) AS completion_tokens FROM usage_log"
        " GROUP BY user_id ORDER BY requests DESC LIMIT 20"
    )
    return {
        "version": VERSION,
        "users": users,
        "sessions": sessions,
        "files": files_count,
        "fileBytes": file_bytes,
        "clientFiles": client_files,
        "requests": requests,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "errors": errors,
        "activeInvites": invites_active,
        "registrationMode": request.app.state.settings.registration_mode,
        "perUser": [
            {
                "userId": row["user_id"],
                "requests": int(row["requests"]),
                "promptTokens": int(row["prompt_tokens"]),
                "completionTokens": int(row["completion_tokens"]),
            }
            for row in per_user
        ],
    }


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


@router.get("/audit")
async def list_audit(request: Request, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    await require_admin(request)
    db: Database = request.app.state.db
    limit = max(1, min(int(limit or 50), 500))
    offset = max(0, int(offset or 0))
    rows = await db.fetch_all(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    )
    total = int(await db.fetch_value("SELECT COUNT(*) FROM audit_log", (), 0) or 0)
    return {
        "entries": [
            {
                "id": int(row["id"]),
                "actorId": row["actor_id"],
                "actorName": row["actor_name"],
                "action": row["action"],
                "target": row["target"],
                "detail": row["detail"],
                "ip": row["ip"],
                "createdAt": int(row["created_at"]),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


@router.get("/diagnostics")
async def diagnostics(request: Request) -> dict[str, Any]:
    """Operational self-check: upstreams, storage, ffmpeg, limits, version."""
    admin = await require_admin(request)
    state = request.app.state
    settings = state.settings
    db: Database = state.db
    started_at = float(getattr(state, "started_at", time.time()))

    checks: list[dict[str, Any]] = []
    timeout = httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=None)
    for endpoint in settings.upstream_endpoints():
        url = f"{endpoint.base_url.rstrip('/')}/models"
        headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "name": endpoint.name,
            "baseUrl": endpoint.base_url,
            "dialect": endpoint.thinking_mode,
            "ok": False,
            "status": None,
            "latencyMs": None,
            "error": None,
        }
        try:
            response = await state.upstream.client.get(url, headers=headers, timeout=timeout)
            entry["status"] = response.status_code
            entry["ok"] = response.status_code == 200
            if response.status_code != 200:
                entry["error"] = response.text[:200]
        except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        entry["latencyMs"] = int((time.perf_counter() - started) * 1000)
        checks.append(entry)

    async def count(sql: str, params: tuple[Any, ...] = ()) -> int:
        return int(await db.fetch_value(sql, params, 0) or 0)

    db_size = 0
    try:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(settings.db_path) + suffix)
            if candidate.is_file():
                db_size += candidate.stat().st_size
    except OSError:
        pass

    disk_files = 0
    disk_bytes = 0
    try:
        for path in settings.files_dir.rglob("*"):
            if path.is_file():
                disk_files += 1
                disk_bytes += path.stat().st_size
    except OSError:
        pass

    limiters = getattr(state, "limiters", None)
    limiter_stats: dict[str, int] = {}
    if limiters is not None:
        for name in (
            "login_by_ip_user",
            "login_by_ip",
            "register_by_ip",
            "messages_by_user",
        ):
            limiter = getattr(limiters, name, None)
            if limiter is not None:
                limiter_stats[name] = limiter.snapshot()

    return {
        "version": VERSION,
        "uptimeSeconds": int(time.time() - started_at),
        "serverTime": now_ms(),
        "dataDir": str(settings.data_dir),
        "webDir": str(settings.web_dir),
        "webDirPresent": Path(settings.web_dir).is_dir(),
        "database": {
            "path": str(settings.db_path),
            "sizeBytes": db_size,
            "users": await count("SELECT COUNT(*) FROM users"),
            "sessions": await count("SELECT COUNT(*) FROM sessions"),
            "filesRows": await count("SELECT COUNT(*) FROM files WHERE deleted = 0"),
            "clientFileRows": await count("SELECT COUNT(*) FROM client_files"),
            "auditRows": await count("SELECT COUNT(*) FROM audit_log"),
            "tombstones": await count("SELECT COUNT(*) FROM deleted_sessions"),
        },
        "storage": {
            "diskFiles": disk_files,
            "diskBytes": disk_bytes,
            "userStorageMb": int(settings.user_storage_mb),
        },
        "upstreams": checks,
        "ffmpeg": {
            "present": bool(settings.speech.ffmpeg_bin),
            "path": settings.speech.ffmpeg_bin,
        },
        "config": {
            "registrationMode": settings.registration_mode,
            "searchMode": settings.search.mode,
            "searchProvider": settings.search.provider,
            "ttsProvider": settings.speech.tts.provider,
            "asrProvider": settings.speech.asr.provider,
            "docsEnabled": settings.docs_enabled,
            "messageRatePerMinute": settings.message_rate_per_minute,
            "dailyTokenQuota": settings.daily_token_quota,
            "dailyTtsChars": settings.daily_tts_chars,
            "tokenTtlDays": settings.token_ttl_days,
            "maxJsonBytes": settings.max_json_body_bytes,
            "maxUploadBytes": settings.max_upload_bytes,
        },
        "limiters": limiter_stats,
        "adminId": admin.id,
    }


# ---------------------------------------------------------------------------
# maintenance: orphan cleanup
# ---------------------------------------------------------------------------


@router.post("/maintenance/cleanup")
async def maintenance_cleanup(request: Request, dry_run: bool = True) -> dict[str, Any]:
    """Remove files with no row, rows with no file, and rows owned by no user.

    Dry-run by default (``?dry_run=0`` to actually delete).
    """
    admin = await require_admin(request)
    state = request.app.state
    settings = state.settings
    db: Database = state.db

    user_ids = {row["id"] for row in await db.fetch_all("SELECT id FROM users")}

    # 1) files table vs disk
    orphan_disk_files: list[str] = []
    missing_disk_files: list[dict[str, Any]] = []
    file_rows = await db.fetch_all(
        "SELECT id, user_id, size_bytes FROM files WHERE deleted = 0"
    )
    file_ids_by_user: dict[str, set[str]] = {}
    for row in file_rows:
        user_id = str(row["user_id"])
        file_ids_by_user.setdefault(user_id, set()).add(str(row["id"]))

    client_rows = await db.fetch_all(
        "SELECT user_id, local_id, size_bytes FROM client_files"
    )
    client_by_user: dict[str, dict[str, int]] = {}
    for row in client_rows:
        client_by_user.setdefault(str(row["user_id"]), {})[str(row["local_id"])] = int(
            row["size_bytes"]
        )

    seen_server_files: set[tuple[str, str]] = set()
    seen_client_files: set[tuple[str, str]] = set()
    files_root = Path(settings.files_dir)
    if files_root.is_dir():
        for user_dir in sorted(files_root.iterdir()):
            if not user_dir.is_dir():
                continue
            if user_dir.name not in user_ids:
                for child in user_dir.iterdir():
                    if child.is_file():
                        orphan_disk_files.append(str(child))
                continue
            known = file_ids_by_user.get(user_dir.name, set())
            known_client = client_by_user.get(user_dir.name, {})
            for child in user_dir.iterdir():
                if not child.is_file():
                    continue
                name = child.name
                if name.startswith("client_"):
                    local_id = name[len("client_") :]
                    if local_id in known_client:
                        seen_client_files.add((user_dir.name, local_id))
                    else:
                        orphan_disk_files.append(str(child))
                elif name in known:
                    seen_server_files.add((user_dir.name, name))
                else:
                    orphan_disk_files.append(str(child))

    for user_id, ids in file_ids_by_user.items():
        for file_id in sorted(ids - {name for uid, name in seen_server_files if uid == user_id}):
            missing_disk_files.append({"id": file_id, "userId": user_id})
    for user_id, locals_ in client_by_user.items():
        for local_id in sorted(locals_):
            if (user_id, local_id) not in seen_client_files:
                missing_disk_files.append(
                    {"id": f"client_{local_id}", "userId": user_id, "kind": "client"}
                )

    # 2) rows whose owner no longer exists
    orphan_session_rows = [
        str(row["id"])
        for row in await db.fetch_all(
            "SELECT s.id FROM sessions s LEFT JOIN users u ON u.id = s.user_id"
            " WHERE u.id IS NULL"
        )
    ]
    orphan_summary_rows = [
        str(row["id"])
        for row in await db.fetch_all(
            "SELECT s.id FROM session_summaries s LEFT JOIN users u ON u.id = s.user_id"
            " WHERE u.id IS NULL"
        )
    ]
    orphan_file_rows = [
        str(row["id"])
        for row in await db.fetch_all(
            "SELECT f.id FROM files f LEFT JOIN users u ON u.id = f.user_id"
            " WHERE u.id IS NULL"
        )
    ]
    orphan_settings_rows = [
        str(row["user_id"])
        for row in await db.fetch_all(
            "SELECT s.user_id FROM user_settings s LEFT JOIN users u ON u.id = s.user_id"
            " WHERE u.id IS NULL"
        )
    ]
    orphan_token_rows = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM tokens t LEFT JOIN users u ON u.id = t.user_id"
            " WHERE u.id IS NULL",
            (),
            0,
        )
        or 0
    )
    stale_tombstones = int(
        await db.fetch_value(
            "SELECT COUNT(*) FROM deleted_sessions WHERE deleted_at < ?",
            (now_ms() - 90 * 24 * 3600 * 1000,),
            0,
        )
        or 0
    )

    removed_bytes = 0
    deleted_files = 0
    if not dry_run:
        for path_text in orphan_disk_files:
            path = Path(path_text)
            try:
                removed_bytes += path.stat().st_size
                path.unlink(missing_ok=True)
                deleted_files += 1
            except OSError:
                continue
        for entry in missing_disk_files:
            if entry.get("kind") == "client":
                await db.execute(
                    "DELETE FROM client_files WHERE user_id = ? AND local_id = ?",
                    (entry["userId"], entry["id"].removeprefix("client_")),
                )
            else:
                await db.execute("DELETE FROM files WHERE id = ?", (entry["id"],))
        for session_id in orphan_session_rows:
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        for session_id in orphan_summary_rows:
            await db.execute("DELETE FROM session_summaries WHERE id = ?", (session_id,))
        for file_id in orphan_file_rows:
            await db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        for user_id in orphan_settings_rows:
            await db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        await db.execute(
            "DELETE FROM tokens WHERE user_id NOT IN (SELECT id FROM users)", ()
        )
        await db.execute(
            "DELETE FROM deleted_sessions WHERE deleted_at < ?",
            (now_ms() - 90 * 24 * 3600 * 1000,),
        )
        await audit.record(
            request,
            audit.MAINTENANCE_CLEANUP,
            actor_id=admin.id,
            actor_name=admin.username,
            target="maintenance",
            detail={
                "dryRun": False,
                "diskFiles": deleted_files,
                "rows": len(missing_disk_files),
                "sessions": len(orphan_session_rows),
                "bytes": removed_bytes,
            },
            db=db,
            settings=settings,
        )

    orphan_bytes = 0
    for path_text in orphan_disk_files:
        try:
            if Path(path_text).is_file():
                orphan_bytes += Path(path_text).stat().st_size
        except OSError:
            continue
    return {
        "dryRun": bool(dry_run),
        "orphanDiskFiles": orphan_disk_files[:500],
        "orphanDiskFileCount": len(orphan_disk_files),
        "orphanDiskBytes": removed_bytes if not dry_run else orphan_bytes,
        "rowsWithoutFile": missing_disk_files[:500],
        "rowsWithoutFileCount": len(missing_disk_files),
        "sessionsWithoutOwner": orphan_session_rows[:500],
        "sessionsWithoutOwnerCount": len(orphan_session_rows),
        "summariesWithoutOwnerCount": len(orphan_summary_rows),
        "filesWithoutOwnerCount": len(orphan_file_rows),
        "settingsWithoutOwnerCount": len(orphan_settings_rows),
        "tokensWithoutOwnerCount": orphan_token_rows,
        "staleTombstones": stale_tombstones,
        "deleted": {
            "diskFiles": deleted_files,
            "rows": len(missing_disk_files) if not dry_run else 0,
            "sessions": len(orphan_session_rows) if not dry_run else 0,
            "bytes": removed_bytes if not dry_run else 0,
        },
    }


ADMIN_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DGX Chat 管理后台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; background:#0f1116; color:#e5e7ee;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid #23272f; display:flex; align-items:center; gap:14px; }
  header h1 { font-size:17px; margin:0; font-weight:600; }
  header .badge { font-size:12px; color:#7ee2a8; background:#17301f; border:1px solid #245b3a;
                  border-radius:999px; padding:2px 9px; }
  main { max-width:1080px; margin:0 auto; padding:24px; display:grid; gap:20px; }
  section { background:#15181f; border:1px solid #23272f; border-radius:12px; padding:18px; }
  h2 { font-size:14px; margin:0 0 14px; color:#aab1c0; font-weight:600; letter-spacing:.4px; }
  label { display:block; font-size:12px; color:#8b93a3; margin:0 0 6px; }
  input, select, button { font: inherit; }
  input, select { width:100%; padding:9px 11px; border-radius:8px; border:1px solid #2b3140;
                  background:#0f1116; color:#e5e7ee; }
  button { padding:9px 14px; border-radius:8px; border:1px solid #2b5fd9; background:#1e4bb8;
           color:#fff; cursor:pointer; font-weight:500; }
  button.ghost { background:#1b1f27; border-color:#2b3140; color:#c8cedb; }
  button.danger { background:#5a1f24; border-color:#81323a; color:#ffd6d9; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .row { display:flex; gap:10px; flex-wrap:wrap; }
  .row > div { flex:1 1 190px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:9px 8px; border-bottom:1px solid #23272f; }
  th { color:#8b93a3; font-weight:500; font-size:12px; }
  td.actions { display:flex; gap:6px; flex-wrap:wrap; }
  td.actions button { padding:5px 9px; font-size:12px; border-radius:6px; }
  .muted { color:#8b93a3; font-size:13px; }
  .msg { min-height:20px; font-size:13px; margin-top:10px; }
  .ok { color:#63e6a4; } .err { color:#ff8f98; }
  .kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
  .kv div { background:#11141a; border:1px solid #23272f; border-radius:9px; padding:12px; }
  .kv b { display:block; font-size:20px; margin-top:4px; }
  .kv span { color:#8b93a3; font-size:12px; }
  .hidden { display:none !important; }
  code { background:#1b1f27; border-radius:5px; padding:1px 5px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>DGX Chat 管理后台</h1>
  <span class="badge" id="statusBadge">未登录</span>
</header>
<main>
  <section id="loginSection">
    <h2>管理员登录</h2>
    <div class="row">
      <div><label>用户名</label><input id="loginUser" autocomplete="username" placeholder="admin"></div>
      <div><label>密码</label><input id="loginPass" type="password" autocomplete="current-password"
             onkeydown="if(event.key==='Enter')doLogin()"></div>
      <div style="flex:0 0 auto;align-self:end"><button id="loginBtn" onclick="doLogin()">登录</button></div>
    </div>
    <div class="msg" id="loginMsg"></div>
    <p class="muted">首次部署时，<code>POST /auth/register</code> 注册的第一个账号自动成为管理员。
      登录后 token 只保存在本机浏览器 localStorage。</p>
  </section>

  <div id="adminArea" class="hidden" style="display:grid;gap:20px">
    <section>
      <h2>用户</h2>
      <div class="row" style="align-items:end">
        <div><label>新用户名</label><input id="newUser" placeholder="username"></div>
        <div><label>密码（至少 6 位）</label><input id="newPass" type="password" placeholder="password"></div>
        <div><label>角色</label><select id="newRole"><option value="user">user</option><option value="admin">admin</option></select></div>
        <div style="flex:0 0 auto"><button onclick="createUser()">创建用户</button></div>
        <div style="flex:0 0 auto"><button class="ghost" onclick="loadAll()">刷新</button></div>
        <div style="flex:0 0 auto"><button class="ghost" onclick="logout()">退出登录</button></div>
      </div>
      <div class="msg" id="userMsg"></div>
      <div style="overflow:auto;margin-top:8px">
        <table>
          <thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>会话</th><th>文件</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody id="userRows"><tr><td colspan="7" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>我的账号</h2>
      <div class="row" style="align-items:end">
        <div><label>原密码</label><input id="oldPass" type="password" autocomplete="current-password"></div>
        <div><label>新密码（至少 6 位）</label><input id="chgPass" type="password" autocomplete="new-password"></div>
        <div><label>确认新密码</label><input id="chgPass2" type="password" autocomplete="new-password"
               onkeydown="if(event.key==='Enter')changeMyPassword()"></div>
        <div style="flex:0 0 auto"><button onclick="changeMyPassword()">修改密码</button></div>
      </div>
      <div class="msg" id="chgMsg"></div>
      <p class="muted">改完只有当前这个浏览器保持登录，其他设备的登录会失效。</p>
    </section>

    <section>
      <h2>邀请码</h2>
      <div class="row" style="align-items:end">
        <div><label>备注</label><input id="invNote" placeholder="给谁用（可空）"></div>
        <div><label>可用次数（0=不限）</label><input id="invUses" type="number" min="0" max="1000" value="1"></div>
        <div><label>有效小时数（空=永久）</label><input id="invHours" type="number" min="1" value="72"></div>
        <div style="flex:0 0 auto"><button onclick="createInvite()">生成邀请码</button></div>
      </div>
      <div class="msg" id="inviteMsg"></div>
      <div style="overflow:auto;margin-top:8px">
        <table>
          <thead><tr><th>邀请码</th><th>备注</th><th>剩余</th><th>过期</th><th>状态</th><th>操作</th></tr></thead>
          <tbody id="inviteRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>统计</h2>
      <div class="kv" id="stats"></div>
    </section>

    <section>
      <h2>诊断 <span class="muted" style="font-weight:400;font-size:12px">（上游连通性、存储、限流器）</span></h2>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="ghost" onclick="loadDiagnostics()">重新检测</button>
        <span class="muted" id="diagHint">点击“重新检测”会向上游发一次 /models 请求（3 秒超时）。</span>
      </div>
      <div class="msg" id="diagMsg"></div>
      <div class="kv" id="diagKv" style="margin-top:10px"></div>
      <div style="overflow:auto;margin-top:10px">
        <table>
          <thead><tr><th>上游</th><th>状态</th><th>延迟</th><th>方言</th><th>错误</th></tr></thead>
          <tbody id="diagUpstreams"><tr><td colspan="5" class="muted">尚未检测</td></tr></tbody>
        </table>
      </div>
    </section>

    <section>
      <h2>审计日志 <span class="muted" style="font-weight:400;font-size:12px">（最近 50 条）</span></h2>
      <div class="msg" id="auditMsg"></div>
      <div style="overflow:auto;margin-top:8px">
        <table>
          <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th><th>IP</th></tr></thead>
          <tbody id="auditRows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
        </table>
      </div>
    </section>
  </div>
</main>
<script>
const TOKEN_KEY = 'dgxchat:admin-token';
const state = { token: localStorage.getItem(TOKEN_KEY) || '' };

const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = (ms) => ms ? new Date(ms).toLocaleString() : '-';

function setMsg(id, text, ok) {
  const node = el(id);
  node.textContent = text || '';
  node.className = 'msg ' + (ok ? 'ok' : 'err');
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch (e) { body = { raw: text }; }
  if (!response.ok) {
    const message = (body && body.error && body.error.message) || (body && body.raw) || ('HTTP ' + response.status);
    throw new Error(message);
  }
  return body;
}

async function doLogin() {
  const username = el('loginUser').value.trim();
  const password = el('loginPass').value;
  if (!username || !password) { setMsg('loginMsg', '请输入用户名和密码', false); return; }
  el('loginBtn').disabled = true;
  try {
    const data = await api('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    if (!data.user || data.user.role !== 'admin') {
      setMsg('loginMsg', '该账号不是管理员（role=' + (data.user && data.user.role) + '）', false);
      return;
    }
    state.token = data.token;
    localStorage.setItem(TOKEN_KEY, state.token);
    el('loginMsg').textContent = '';
    showAdmin();
    await loadAll();
  } catch (err) {
    setMsg('loginMsg', '登录失败：' + err.message, false);
  } finally {
    el('loginBtn').disabled = false;
  }
}

function logout() {
  api('/auth/logout', { method: 'POST' }).catch(() => {});
  state.token = '';
  localStorage.removeItem(TOKEN_KEY);
  el('adminArea').classList.add('hidden');
  el('loginSection').classList.remove('hidden');
  el('statusBadge').textContent = '未登录';
}

function showAdmin() {
  el('loginSection').classList.add('hidden');
  el('adminArea').classList.remove('hidden');
  el('statusBadge').textContent = '已登录（管理员）';
}

async function loadAll() {
  await Promise.all([loadUsers(), loadStats(), loadInvites(), loadAudit()]);
}

async function loadUsers() {
  try {
    const data = await api('/admin/users');
    const rows = data.users || [];
    if (!rows.length) { el('userRows').innerHTML = '<tr><td colspan="7" class="muted">还没有用户</td></tr>'; return; }
    el('userRows').innerHTML = rows.map(u => `
      <tr>
        <td>${esc(u.username)}</td>
        <td>${esc(u.role)}</td>
        <td>${u.disabled ? '<span style="color:#ff8f98">已禁用</span>' : '正常'}</td>
        <td>${u.sessionCount}</td>
        <td>${u.fileCount}</td>
        <td>${fmtDate(u.createdAt)}</td>
        <td class="actions">
          <button class="ghost" onclick="resetPassword('${u.id}','${esc(u.username)}')">重置密码</button>
          <button class="ghost" onclick="specifyPassword('${u.id}','${esc(u.username)}')">指定密码</button>
          <button class="ghost" onclick="setDisabled('${u.id}','${esc(u.username)}', ${u.disabled ? 0 : 1})">${u.disabled ? '启用' : '禁用'}</button>
          <button class="ghost" onclick="revokeTokens('${u.id}','${esc(u.username)}')">吊销登录</button>
          <button class="danger" onclick="deleteUser('${u.id}','${esc(u.username)}')">删除</button>
        </td>
      </tr>`).join('');
  } catch (err) {
    setMsg('userMsg', '加载用户失败：' + err.message, false);
  }
}

async function loadAudit() {
  try {
    const data = await api('/admin/audit?limit=50');
    const rows = data.entries || [];
    if (!rows.length) { el('auditRows').innerHTML = '<tr><td colspan="6" class="muted">暂无记录</td></tr>'; return; }
    el('auditRows').innerHTML = rows.map(e => `
      <tr>
        <td>${fmtDate(e.createdAt)}</td>
        <td>${esc(e.actorName || '-')}</td>
        <td><code>${esc(e.action)}</code></td>
        <td>${esc(e.target || '-')}</td>
        <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis">${esc(e.detail || '')}</td>
        <td>${esc(e.ip || '-')}</td>
      </tr>`).join('');
  } catch (err) {
    setMsg('auditMsg', '加载审计日志失败：' + err.message, false);
  }
}

async function loadDiagnostics() {
  setMsg('diagMsg', '检测中…', true);
  try {
    const d = await api('/admin/diagnostics');
    const kv = [
      ['版本', d.version],
      ['运行时长', Math.round(d.uptimeSeconds / 60) + ' 分钟'],
      ['数据目录', d.dataDir],
      ['Web 目录', d.webDirPresent ? '已挂载' : '未挂载'],
      ['数据库大小', (d.database.sizeBytes / 1048576).toFixed(2) + ' MiB'],
      ['用户 / 会话', d.database.users + ' / ' + d.database.sessions],
      ['文件行 / 磁盘文件', d.database.filesRows + ' / ' + d.storage.diskFiles],
      ['磁盘占用', (d.storage.diskBytes / 1048576).toFixed(2) + ' MiB'],
      ['存储配额', d.storage.userStorageMb ? (d.storage.userStorageMb + ' MiB/用户') : '不限'],
      ['ffmpeg', d.ffmpeg.present ? (d.ffmpeg.path || '已安装') : '未安装'],
      ['注册模式', d.config.registrationMode],
      ['搜索', d.config.searchMode + ' / ' + d.config.searchProvider],
      ['TTS / ASR', d.config.ttsProvider + ' / ' + d.config.asrProvider],
      ['消息限流', d.config.messageRatePerMinute + ' 次/分钟'],
      ['每日 Token 配额', d.config.dailyTokenQuota || '不限'],
      ['每日 TTS 字符', d.config.dailyTtsChars || '不限'],
      ['内存限流器', Object.entries(d.limiters || {}).map(([k, v]) => k + '=' + v).join(', ') || '-'],
    ];
    el('diagKv').innerHTML = kv.map(([k, v]) => `<div><span>${esc(k)}</span><b style="font-size:14px;word-break:break-all">${esc(v)}</b></div>`).join('');
    const ups = d.upstreams || [];
    el('diagUpstreams').innerHTML = ups.map(u => `
      <tr>
        <td>${esc(u.name)}<br><span class="muted">${esc(u.baseUrl)}</span></td>
        <td>${u.ok ? '<span style="color:#63e6a4">OK</span>' : '<span style="color:#ff8f98">失败</span>'} ${u.status ?? ''}</td>
        <td>${u.latencyMs ?? '-'} ms</td>
        <td>${esc(u.dialect || '-')}</td>
        <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis">${esc(u.error || '')}</td>
      </tr>`).join('') || '<tr><td colspan="5" class="muted">没有配置上游</td></tr>';
    setMsg('diagMsg', '检测完成', true);
  } catch (err) {
    setMsg('diagMsg', '诊断失败：' + err.message, false);
  }
}

async function specifyPassword(id, name) {
  const value = prompt('为 ' + name + ' 设置新密码（至少 6 位，留空则随机生成）：');
  if (value === null) return;
  const body = value ? { password: value } : {};
  if (value && value.length < 6) { setMsg('userMsg', '密码至少 6 位', false); return; }
  try {
    const data = await api('/admin/users/' + encodeURIComponent(id) + '/reset-password', {
      method: 'POST', body: JSON.stringify(body),
    });
    setMsg('userMsg', '已设置 ' + name + ' 的密码：' + data.password, true);
    await loadUsers(); await loadAudit();
  } catch (err) {
    setMsg('userMsg', '设置失败：' + err.message, false);
  }
}

async function setDisabled(id, name, disabled) {
  const verb = disabled ? '禁用' : '启用';
  if (!confirm(verb + '用户 ' + name + (disabled ? '？其所有登录会立即失效。' : '？'))) return;
  try {
    await api('/admin/users/' + encodeURIComponent(id) + (disabled ? '/disable' : '/enable'), { method: 'POST' });
    setMsg('userMsg', '已' + verb + ' ' + name, true);
    await loadUsers(); await loadAudit();
  } catch (err) {
    setMsg('userMsg', verb + '失败：' + err.message, false);
  }
}

async function loadStats() {
  try {
    const data = await api('/admin/stats');
    const defs = [
      ['用户', data.users], ['会话', data.sessions], ['文件', data.files],
      ['文件占用', (data.fileBytes / 1048576).toFixed(1) + ' MiB'],
      ['本地附件', data.clientFiles], ['请求数', data.requests],
      ['输入 Token', data.promptTokens], ['输出 Token', data.completionTokens],
      ['失败请求', data.errors], ['有效邀请码', data.activeInvites],
      ['注册模式', data.registrationMode], ['版本', data.version],
    ];
    el('stats').innerHTML = defs.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  } catch (err) {
    el('stats').innerHTML = '<div><span>错误</span><b>' + esc(err.message) + '</b></div>';
  }
}

async function loadInvites() {
  try {
    const data = await api('/admin/invites');
    const rows = data.invites || [];
    if (!rows.length) { el('inviteRows').innerHTML = '<tr><td colspan="6" class="muted">还没有邀请码</td></tr>'; return; }
    el('inviteRows').innerHTML = rows.map(inv => {
      const expired = inv.expiresAt && inv.expiresAt < Date.now();
      const exhausted = inv.usesLeft !== null && inv.usesLeft <= 0;
      const state = inv.revoked ? '已吊销' : expired ? '已过期' : exhausted ? '已用完' : '可用';
      return `<tr>
        <td><code>${esc(inv.code)}</code></td>
        <td>${esc(inv.note || '-')}</td>
        <td>${inv.usesLeft === null ? '不限' : inv.usesLeft}</td>
        <td>${inv.expiresAt ? fmtDate(inv.expiresAt) : '永久'}</td>
        <td>${esc(state)}</td>
        <td class="actions">
          <button class="ghost" onclick="copyInvite('${esc(inv.code)}')">复制</button>
          <button class="danger" onclick="revokeInvite('${esc(inv.code)}')">吊销</button>
        </td>
      </tr>`;
    }).join('');
  } catch (err) {
    setMsg('inviteMsg', '加载邀请码失败：' + err.message, false);
  }
}

function copyInvite(code) {
  const done = () => setMsg('inviteMsg', '已复制 ' + code, true);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code).then(done).catch(() => setMsg('inviteMsg', code, true));
  } else {
    setMsg('inviteMsg', '邀请码：' + code, true);
  }
}

async function createInvite() {
  const note = el('invNote').value.trim();
  const maxUses = parseInt(el('invUses').value || '0', 10);
  const hoursRaw = el('invHours').value.trim();
  const body = { note: note || null, max_uses: isNaN(maxUses) ? 1 : maxUses };
  if (hoursRaw) body.expires_in_hours = parseInt(hoursRaw, 10);
  try {
    const data = await api('/admin/invites', { method: 'POST', body: JSON.stringify(body) });
    setMsg('inviteMsg', '已生成邀请码：' + data.code, true);
    el('invNote').value = '';
    await loadInvites(); await loadStats();
  } catch (err) {
    setMsg('inviteMsg', '生成失败：' + err.message, false);
  }
}

async function revokeInvite(code) {
  if (!confirm('吊销邀请码 ' + code + '？')) return;
  try {
    await api('/admin/invites/' + encodeURIComponent(code), { method: 'DELETE' });
    setMsg('inviteMsg', '已吊销 ' + code, true);
    await loadInvites(); await loadStats();
  } catch (err) {
    setMsg('inviteMsg', '吊销失败：' + err.message, false);
  }
}

async function createUser() {
  const username = el('newUser').value.trim();
  const password = el('newPass').value;
  const role = el('newRole').value;
  if (!username || !password) { setMsg('userMsg', '请填写用户名和密码', false); return; }
  try {
    await api('/admin/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
    setMsg('userMsg', '已创建用户 ' + username, true);
    el('newUser').value = ''; el('newPass').value = '';
    await loadUsers(); await loadStats();
  } catch (err) {
    setMsg('userMsg', '创建失败：' + err.message, false);
  }
}

async function deleteUser(id, name) {
  if (!confirm('确定删除用户 ' + name + '？其会话与文件会一并删除。')) return;
  try {
    await api('/admin/users/' + encodeURIComponent(id), { method: 'DELETE' });
    setMsg('userMsg', '已删除 ' + name, true);
    await loadUsers(); await loadStats();
  } catch (err) {
    setMsg('userMsg', '删除失败：' + err.message, false);
  }
}

async function resetPassword(id, name) {
  if (!confirm('重置 ' + name + ' 的密码？该用户所有已登录设备会被强制下线。')) return;
  try {
    const data = await api('/admin/users/' + encodeURIComponent(id) + '/reset-password', { method: 'POST' });
    setMsg('userMsg', '新密码：' + data.password, true);
    await loadUsers();
  } catch (err) {
    setMsg('userMsg', '重置失败：' + err.message, false);
  }
}

async function revokeTokens(id, name) {
  if (!confirm('吊销 ' + name + ' 的所有登录凭证？该用户需要重新登录。')) return;
  try {
    const data = await api('/admin/users/' + encodeURIComponent(id) + '/revoke-tokens', { method: 'POST' });
    setMsg('userMsg', '已吊销 ' + name + ' 的 ' + data.revokedTokens + ' 个 token', true);
  } catch (err) {
    setMsg('userMsg', '吊销失败：' + err.message, false);
  }
}

async function changeMyPassword() {
  const oldPassword = el('oldPass').value;
  const newPassword = el('chgPass').value;
  const confirmPassword = el('chgPass2').value;
  if (!oldPassword || !newPassword) { setMsg('chgMsg', '请填写原密码和新密码', false); return; }
  if (newPassword !== confirmPassword) { setMsg('chgMsg', '两次输入的新密码不一致', false); return; }
  if (newPassword.length < 6) { setMsg('chgMsg', '新密码至少 6 位', false); return; }
  try {
    const data = await api('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword })
    });
    el('oldPass').value = ''; el('chgPass').value = ''; el('chgPass2').value = '';
    setMsg('chgMsg', '密码已修改。其他设备上的 ' + (data.revokedOtherTokens || 0) + ' 个登录已失效。', true);
  } catch (err) {
    setMsg('chgMsg', '修改失败：' + err.message, false);
  }
}

if (state.token) {
  api('/auth/me').then(user => {
    if (user && user.role === 'admin') { showAdmin(); loadAll(); }
    else { logout(); }
  }).catch(() => logout());
}
</script>
</body>
</html>
"""
