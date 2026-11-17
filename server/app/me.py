"""Self-service account API: personal tokens, self-deletion, usage stats.

Everything here is scoped to the authenticated user; ids in URLs are validated
against the caller's ``user_id`` so one user can never touch another's rows.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from . import audit, credentials, speech
from .auth import _extract_credential, authenticate, token_hash, verify_password
from .config import Settings
from .db import Database, now_ms
from .files import FileStore
from .models import ApiError, authentication_error
from .security import reject_oversized_content_length, storage_usage, today_utc
from .speech import load_user_settings

router = APIRouter(tags=["me"])


def token_public_id(token_hash_value: str) -> str:
    """Non-secret, stable id for a token row (never the token itself)."""
    return token_hash_value[:16]


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password: str = Field(min_length=1, max_length=256)


# ---------------------------------------------------------------------------
# personal tokens / devices
# ---------------------------------------------------------------------------


@router.get("/api/me/tokens")
async def list_tokens(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    credential = _extract_credential(request)
    current_hash = token_hash(credential) if credential else ""
    rows = await db.fetch_all(
        "SELECT token_hash, label, created_at, last_used_at, revoked, expires_at"
        " FROM tokens WHERE user_id = ? AND revoked = 0"
        " AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at DESC",
        (user.id, now_ms()),
    )
    tokens = []
    for row in rows:
        hash_value = str(row["token_hash"])
        tokens.append(
            {
                "id": token_public_id(hash_value),
                "label": row["label"] or "",
                "createdAt": int(row["created_at"]),
                "lastUsedAt": int(row["last_used_at"]) if row["last_used_at"] else None,
                "expiresAt": int(row["expires_at"]) if row["expires_at"] else None,
                "revoked": bool(row["revoked"]),
                "current": bool(current_hash) and hash_value == current_hash,
            }
        )
    return {"tokens": tokens, "total": len(tokens)}


@router.delete("/api/me/tokens/{token_id}")
async def revoke_token(token_id: str, request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    clean = (token_id or "").strip()
    if len(clean) < 8 or len(clean) > 64:
        raise ApiError(400, "非法的 token id", "invalid_request_error", param="id")
    row = await db.fetch_one(
        "SELECT token_hash, label FROM tokens WHERE user_id = ? AND substr(token_hash, 1, ?) = ?",
        (user.id, len(clean), clean),
    )
    if row is None:
        raise ApiError(404, "登录凭证不存在", "not_found_error")
    await db.execute(
        "DELETE FROM tokens WHERE user_id = ? AND token_hash = ?",
        (user.id, str(row["token_hash"])),
    )
    await audit.record(
        request,
        audit.TOKENS_REVOKED,
        actor_id=user.id,
        actor_name=user.username,
        target=token_public_id(str(row["token_hash"])),
        detail={"label": row["label"] or "", "scope": "single"},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "id": token_public_id(str(row["token_hash"])), "revoked": 1}


@router.post("/api/me/tokens/revoke-others")
async def revoke_other_tokens(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    credential = _extract_credential(request)
    current_hash = token_hash(credential) if credential else ""
    counted = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM tokens WHERE user_id = ? AND token_hash != ? AND revoked = 0",
        (user.id, current_hash),
    )
    revoked = int(counted["n"]) if counted else 0
    await db.execute(
        "UPDATE tokens SET revoked = 1 WHERE user_id = ? AND token_hash != ? AND revoked = 0",
        (user.id, current_hash),
    )
    await audit.record(
        request,
        audit.TOKENS_REVOKED,
        actor_id=user.id,
        actor_name=user.username,
        target=user.id,
        detail={"scope": "others", "revoked": revoked},
        db=db,
        settings=request.app.state.settings,
    )
    return {"ok": True, "revokedOtherTokens": revoked}


# ---------------------------------------------------------------------------
# self deletion
# ---------------------------------------------------------------------------


@router.delete("/api/me")
async def delete_account(request: Request) -> dict[str, Any]:
    """Delete the caller's account and every piece of data it owns.

    Requires the current password as confirmation, and refuses to remove the last
    enabled administrator (that would lock the deployment out of /admin).
    """
    state = request.app.state
    settings: Settings = state.settings
    db: Database = state.db
    store: FileStore = state.files
    user = await authenticate(request)

    reject_oversized_content_length(request, 64 * 1024)
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, "请求体不是合法 JSON", "invalid_request_error") from exc
    password = body.get("password") if isinstance(body, dict) else None
    if not isinstance(password, str) or not password:
        raise ApiError(400, "需要提供当前密码以确认删除", "invalid_request_error", param="password")

    account = await db.fetch_one("SELECT password_hash, role FROM users WHERE id = ?", (user.id,))
    if account is None:
        raise authentication_error("账号不存在")
    if not verify_password(password, str(account["password_hash"])):
        raise authentication_error("密码不正确")

    if str(account["role"]) == "admin":
        admins = await db.fetch_value(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0", (), 0
        )
        if int(admins or 0) <= 1:
            raise ApiError(
                400,
                "你是最后一个管理员，不能删除自己的账号（请先指定另一位管理员）",
                "invalid_request_error",
            )

    # Remove payloads from disk first, then every row owned by this user.
    await store.delete_all_for_user(user.id)
    rows = await db.fetch_all(
        "SELECT (SELECT COUNT(*) FROM sessions WHERE user_id = ?) AS sessions,"
        " (SELECT COUNT(*) FROM client_files WHERE user_id = ?) AS client_files,"
        " (SELECT COUNT(*) FROM tokens WHERE user_id = ?) AS tokens",
        (user.id, user.id, user.id),
    )
    counts = dict(rows[0]) if rows else {}
    await db.execute("DELETE FROM client_files WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM deleted_sessions WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM user_settings WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM tokens WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM session_summaries WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
    await db.execute("DELETE FROM users WHERE id = ?", (user.id,))

    await audit.record(
        request,
        audit.ACCOUNT_SELF_DELETED,
        actor_id=user.id,
        actor_name=user.username,
        target=user.id,
        detail={
            "sessions": int(counts.get("sessions") or 0),
            "clientFiles": int(counts.get("client_files") or 0),
            "tokens": int(counts.get("tokens") or 0),
        },
        db=db,
        settings=settings,
    )
    return {"ok": True, "deleted": True, "userId": user.id}


# ---------------------------------------------------------------------------
# personal usage stats
# ---------------------------------------------------------------------------


@router.get("/api/me/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    """Non-secret readiness probe for the UI (never leaks key material)."""
    user = await authenticate(request)
    state = request.app.state
    settings: Settings = state.settings
    db: Database = state.db
    user_settings = await load_user_settings(db, user.id)

    user_official = credentials.user_official_key(user_settings)
    server_official = bool(settings.search.official_api_key)
    official = {
        "configured": bool(user_official or server_official),
        "source": "user" if user_official else ("server" if server_official else None),
    }

    def speech_capability(kind: str) -> dict[str, Any]:
        provider = speech.resolve_provider(settings, user_settings, kind)
        configured = speech.speech_configured(provider)
        override = credentials.user_has_speech_override(user_settings, kind)
        return {
            "configured": configured,
            "source": "user" if override else ("server" if configured else None),
        }

    search_override = credentials.user_block(user_settings, "search")
    available, _reason = state.search_service.available(
        search_override or None, user_official
    )
    return {
        "official": official,
        "tts": speech_capability("tts"),
        "asr": speech_capability("asr"),
        "search": {"available": bool(available), "requires_official": True},
    }


@router.get("/api/me/stats")
async def my_stats(request: Request) -> dict[str, Any]:
    user = await authenticate(request)
    db: Database = request.app.state.db
    settings: Settings = request.app.state.settings

    sessions = int(
        await db.fetch_value("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user.id,), 0) or 0
    )
    bytes_used, file_count = await storage_usage(db, user.id)
    usage = await db.fetch_one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS prompt,"
        " COALESCE(SUM(output_tokens), 0) AS completion FROM usage_log WHERE user_id = ?",
        (user.id,),
    )
    counters = await db.fetch_all(
        "SELECT kind, COALESCE(SUM(amount), 0) AS total FROM daily_counters"
        " WHERE user_id = ? GROUP BY kind",
        (user.id,),
    )
    totals = {str(row["kind"]): int(row["total"]) for row in counters}
    today = await db.fetch_all(
        "SELECT kind, amount FROM daily_counters WHERE user_id = ? AND day = ?",
        (user.id, today_utc()),
    )
    today_map = {str(row["kind"]): int(row["amount"]) for row in today}

    daily_rows = await db.fetch_all(
        "SELECT strftime('%Y-%m-%d', created_at / 1000, 'unixepoch') AS day,"
        " COUNT(*) AS requests, COALESCE(SUM(input_tokens), 0) AS prompt,"
        " COALESCE(SUM(output_tokens), 0) AS completion"
        " FROM usage_log WHERE user_id = ? AND created_at >= ?"
        " GROUP BY day ORDER BY day",
        (user.id, now_ms() - 14 * 86_400_000),
    )
    by_day = {
        str(row["day"]): {
            "day": str(row["day"]),
            "requests": int(row["requests"]),
            "promptTokens": int(row["prompt"]),
            "completionTokens": int(row["completion"]),
        }
        for row in daily_rows
    }
    import time as _time

    daily: list[dict[str, Any]] = []
    for offset in range(13, -1, -1):
        stamp = _time.gmtime(_time.time() - offset * 86_400)
        day = _time.strftime("%Y-%m-%d", stamp)
        daily.append(
            by_day.get(day, {"day": day, "requests": 0, "promptTokens": 0, "completionTokens": 0})
        )

    return {
        "user": {"id": user.id, "username": user.username, "role": user.role},
        "sessions": sessions,
        "files": file_count,
        "fileBytes": bytes_used,
        "storageLimitBytes": int(settings.user_storage_mb) * 1024 * 1024,
        "requests": int(usage["requests"] or 0) if usage else 0,
        "promptTokens": int(usage["prompt"] or 0) if usage else 0,
        "completionTokens": int(usage["completion"] or 0) if usage else 0,
        "ttsCharsToday": today_map.get("tts_chars", 0),
        "ttsCharsTotal": totals.get("tts_chars", 0),
        "asrCallsToday": today_map.get("asr_calls", 0),
        "asrCallsTotal": totals.get("asr_calls", 0),
        "daily": daily,
    }
