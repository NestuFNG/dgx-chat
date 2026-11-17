"""Admin audit trail (SPEC P1).

Every administrative or security-relevant action is appended to ``audit_log``.
Auditing must never break the request it is describing, so all failures are
swallowed after being logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from .config import Settings
from .db import Database, now_ms
from .security import client_ip

logger = logging.getLogger("dgxchat.audit")

#: Actions recorded by the product.  Kept as constants so typos cannot drift.
USER_CREATED = "user.created"
USER_DELETED = "user.deleted"
USER_DISABLED = "user.disabled"
USER_ENABLED = "user.enabled"
PASSWORD_RESET = "user.password_reset"
PASSWORD_CHANGED = "user.password_changed"
TOKENS_REVOKED = "user.tokens_revoked"
INVITE_CREATED = "invite.created"
INVITE_REVOKED = "invite.revoked"
ACCOUNT_SELF_DELETED = "account.self_deleted"
MAINTENANCE_CLEANUP = "maintenance.cleanup"


async def record(
    request: Request | None,
    action: str,
    *,
    actor_id: str | None = None,
    actor_name: str | None = None,
    target: str | None = None,
    detail: Any = None,
    db: Database | None = None,
    settings: Settings | None = None,
    ip: str | None = None,
) -> None:
    """Append one audit row; never raises."""
    try:
        if db is None:
            if request is None:
                return
            db = request.app.state.db
        if settings is None and request is not None:
            settings = request.app.state.settings
        if ip is None and request is not None and settings is not None:
            ip = client_ip(request, settings)
        if isinstance(detail, (dict, list)):
            detail_text = json.dumps(detail, ensure_ascii=False)[:4000]
        else:
            detail_text = str(detail)[:4000] if detail is not None else None
        await db.execute(
            "INSERT INTO audit_log (actor_id, actor_name, action, target, detail, ip, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (actor_id, actor_name, action, target, detail_text, ip, now_ms()),
        )
    except Exception:  # noqa: BLE001 - auditing is best-effort
        logger.warning("failed to write audit entry %s", action, exc_info=True)


async def count(db: Database) -> int:
    return int(await db.fetch_value("SELECT COUNT(*) FROM audit_log", (), 0) or 0)
