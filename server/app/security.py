"""Abuse control: request body caps, in-process rate limiting, daily quotas.

Everything is bounded by environment variables (see ``.env.example``); the
defaults are deliberately conservative.  Limits are in-memory and therefore
per-process, which matches the single-container deployment model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from .config import Settings
from .db import Database
from .models import ApiError


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    client = request.client
    return client.host if client else "unknown"


def retry_after_error(message: str, seconds: int) -> ApiError:
    return ApiError(
        429,
        message,
        "rate_limit_error",
        headers={"Retry-After": str(max(1, int(seconds)))},
    )


class SlidingWindowLimiter:
    """Count events per key within a rolling time window."""

    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 20000) -> None:
        self.limit = max(0, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max_keys
        self._events: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        events = self._events.get(key)
        if not events:
            return []
        cutoff = now - self.window
        pruned = [stamp for stamp in events if stamp > cutoff]
        if pruned:
            self._events[key] = pruned
        else:
            self._events.pop(key, None)
        return pruned

    def _evict(self, now: float) -> None:
        if len(self._events) <= self.max_keys:
            return
        # Drop the oldest half to bound memory without a background task.
        ordered = sorted(self._events.items(), key=lambda item: item[1][-1] if item[1] else 0)
        for key, _ in ordered[: len(ordered) // 2]:
            self._events.pop(key, None)

    def check(self, key: str) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now = time.monotonic()
        events = self._prune(key, now)
        if len(events) >= self.limit:
            retry_after = max(0.0, self.window - (now - events[0]))
            return False, retry_after
        return True, 0.0

    def hit(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        events = self._prune(key, now)
        events.append(now)
        self._events[key] = events
        self._evict(now)

    def reset(self, key: str) -> None:
        self._events.pop(key, None)

    def snapshot(self) -> int:
        return len(self._events)


@dataclass(slots=True)
class Limiters:
    """All in-memory limiters used by the app."""

    login_by_ip_user: SlidingWindowLimiter | None = None
    login_by_ip: SlidingWindowLimiter | None = None
    register_by_ip: SlidingWindowLimiter | None = None
    messages_by_user: SlidingWindowLimiter | None = None
    extra: dict[str, SlidingWindowLimiter] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings) -> "Limiters":
        return cls(
            login_by_ip_user=SlidingWindowLimiter(
                settings.login_max_failures, settings.login_window_seconds
            ),
            login_by_ip=SlidingWindowLimiter(
                settings.login_max_failures, settings.login_window_seconds
            ),
            register_by_ip=SlidingWindowLimiter(settings.register_max_per_hour, 3600),
            messages_by_user=SlidingWindowLimiter(settings.message_rate_per_minute, 60),
        )


# ---------------------------------------------------------------------------
# request guards
# ---------------------------------------------------------------------------


def reject_oversized_content_length(request: Request, limit: int) -> None:
    """Fail fast on an oversized declared body, before reading anything."""
    declared = request.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if length > limit:
        raise ApiError(413, "请求体过大", "request_too_large")


async def read_json_body(request: Request, settings: Settings, *, limit: int | None = None) -> Any:
    max_bytes = limit or settings.max_json_body_bytes
    reject_oversized_content_length(request, max_bytes)
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ApiError(413, "请求体过大", "request_too_large")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "请求体不是合法 JSON", "invalid_request_error") from exc


# ---------------------------------------------------------------------------
# quotas
# ---------------------------------------------------------------------------


def today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def day_start_ms() -> int:
    """Start of the current UTC day in epoch milliseconds."""
    now = time.time()
    return int((now - (now % 86400)) * 1000)


def seconds_until_utc_midnight() -> int:
    moment = time.gmtime()
    elapsed = moment.tm_hour * 3600 + moment.tm_min * 60 + moment.tm_sec
    return max(1, 86400 - elapsed)


async def check_token_quota(db: Database, settings: Settings, user_id: str) -> None:
    if settings.daily_token_quota <= 0:
        return
    row = await db.fetch_one(
        "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS used FROM usage_log"
        " WHERE user_id = ? AND created_at >= ?",
        (user_id, day_start_ms()),
    )
    used = int(row["used"] or 0) if row else 0
    if used >= settings.daily_token_quota:
        raise retry_after_error(
            f"今日 Token 配额已用完（{used}/{settings.daily_token_quota}），"
            f"请在 {seconds_until_utc_midnight() // 60} 分钟后重试",
            seconds_until_utc_midnight(),
        )


async def counter_value(db: Database, user_id: str, kind: str) -> int:
    row = await db.fetch_one(
        "SELECT amount FROM daily_counters WHERE user_id = ? AND day = ? AND kind = ?",
        (user_id, today_utc(), kind),
    )
    return int(row["amount"] or 0) if row else 0


async def add_counter(db: Database, user_id: str, kind: str, amount: int) -> int:
    await db.execute(
        "INSERT INTO daily_counters (user_id, day, kind, amount) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(user_id, day, kind) DO UPDATE SET amount = amount + excluded.amount",
        (user_id, today_utc(), kind, int(amount)),
    )
    return await counter_value(db, user_id, kind)


async def check_daily_quota(
    db: Database, user_id: str, kind: str, limit: int, label: str
) -> None:
    if limit <= 0:
        return
    used = await counter_value(db, user_id, kind)
    if used >= limit:
        raise retry_after_error(
            f"今日{label}配额已用完（{used}/{limit}），请明天再试",
            seconds_until_utc_midnight(),
        )


async def log_usage(
    db: Database,
    *,
    user_id: str,
    endpoint: str,
    model: str | None = None,
    upstream_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
    status: str = "ok",
    detail: str | None = None,
) -> None:
    try:
        await db.execute(
            "INSERT INTO usage_log (user_id, created_at, endpoint, model, upstream_model,"
            " input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, status, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                int(time.time() * 1000),
                endpoint,
                model,
                upstream_model,
                int(input_tokens),
                int(output_tokens),
                int(cache_read_tokens),
                int(reasoning_tokens),
                status,
                detail,
            ),
        )
    except Exception:  # noqa: BLE001 - accounting must never fail a request
        pass


# ---------------------------------------------------------------------------
# per-user storage quota (USER_STORAGE_MB, 0 = unlimited)
# ---------------------------------------------------------------------------


async def storage_usage(db: Database, user_id: str) -> tuple[int, int]:
    """Return ``(bytes, file_count)`` for server files plus client attachments."""
    server = await db.fetch_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s FROM files"
        " WHERE user_id = ? AND deleted = 0",
        (user_id,),
    )
    client = await db.fetch_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s FROM client_files"
        " WHERE user_id = ?",
        (user_id,),
    )
    bytes_total = int((server["s"] if server else 0) or 0) + int(
        (client["s"] if client else 0) or 0
    )
    count = int((server["c"] if server else 0) or 0) + int((client["c"] if client else 0) or 0)
    return bytes_total, count


async def check_storage_quota(
    db: Database, settings: Settings, user_id: str, additional_bytes: int
) -> None:
    limit_mb = int(getattr(settings, "user_storage_mb", 0) or 0)
    if limit_mb <= 0:
        return
    limit = limit_mb * 1024 * 1024
    used, _ = await storage_usage(db, user_id)
    if used + max(0, int(additional_bytes)) > limit:
        raise ApiError(
            413,
            f"存储配额已满：已用 {used / 1048576:.2f} MiB / 上限 {limit_mb} MiB"
            f"（本次还需要 {additional_bytes / 1048576:.2f} MiB）",
            "request_too_large",
            param="storage",
        )
