"""Authentication: scrypt password hashing, opaque tokens, FastAPI dependencies.

Tokens are 32 random bytes rendered as hex; only their SHA-256 digest is stored,
so a database leak cannot be replayed.  Lookups are constant-time compared after
the row is fetched.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .config import Settings
from .db import Database, now_ms
from . import audit
from .models import (
    ApiError,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    UserOut,
    authentication_error,
    forbidden,
)
from .security import client_ip, retry_after_error

router = APIRouter(tags=["auth"])

SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
_SALT_BYTES = 16


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, *, n: int = 2**14) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=96 * 1024 * 1024,
    )
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected) or SCRYPT_DKLEN,
        maxmem=96 * 1024 * 1024,
    )
    return hmac.compare_digest(digest, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# user records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuthUser:
    id: str
    username: str
    role: str
    created_at: int
    disabled: bool = False

    def to_out(self) -> UserOut:
        return UserOut(
            id=self.id, username=self.username, role=self.role, createdAt=self.created_at
        )


def _row_to_user(row: Any) -> AuthUser:
    return AuthUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        created_at=int(row["created_at"]),
        disabled=bool(row["disabled"]),
    )


def normalise_username(username: str) -> str:
    value = (username or "").strip()
    if not value:
        raise ApiError(400, "用户名不能为空", "invalid_request_error", param="username")
    if len(value) > 64:
        raise ApiError(400, "用户名过长（最多 64 字符）", "invalid_request_error", param="username")
    if any(ord(ch) < 32 for ch in value):
        raise ApiError(400, "用户名包含控制字符", "invalid_request_error", param="username")
    return value


def validate_password(password: str, settings: Settings) -> None:
    if len(password) < settings.password_min_length:
        raise ApiError(
            400,
            f"密码至少 {settings.password_min_length} 个字符",
            "invalid_request_error",
            param="password",
        )
    if len(password) > 256:
        raise ApiError(400, "密码过长（最多 256 字符）", "invalid_request_error", param="password")


async def create_user(
    db: Database,
    settings: Settings,
    username: str,
    password: str,
    role: str = "user",
) -> AuthUser:
    username = normalise_username(username)
    validate_password(password, settings)
    user_id = f"u_{secrets.token_hex(12)}"
    stamp = now_ms()
    try:
        await db.execute(
            "INSERT INTO users (id, username, username_key, password_hash, role, disabled,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (
                user_id,
                username,
                username.lower(),
                hash_password(password, n=settings.scrypt_n),
                role,
                stamp,
                stamp,
            ),
        )
    except Exception as exc:  # sqlite3.IntegrityError for duplicate username
        if "UNIQUE" in str(exc).upper():
            raise ApiError(409, "用户名已被占用", "conflict_error", param="username") from exc
        raise
    return AuthUser(id=user_id, username=username, role=role, created_at=stamp)


async def find_user_by_username(db: Database, username: str) -> Any:
    return await db.fetch_one(
        "SELECT * FROM users WHERE username_key = ?", ((username or "").strip().lower(),)
    )


async def find_user_by_id(db: Database, user_id: str) -> AuthUser | None:
    row = await db.fetch_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return _row_to_user(row) if row else None


async def issue_token(
    db: Database,
    user_id: str,
    label: str | None = None,
    *,
    ttl_days: int = 0,
) -> str:
    token = secrets.token_hex(32)
    expires_at = now_ms() + ttl_days * 86_400_000 if ttl_days > 0 else None
    await db.execute(
        "INSERT INTO tokens (token_hash, user_id, created_at, revoked, label, expires_at)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (token_hash(token), user_id, now_ms(), label, expires_at),
    )
    return token


async def count_users(db: Database) -> int:
    value = await db.fetch_value("SELECT COUNT(*) FROM users", (), 0)
    return int(value or 0)


# ---------------------------------------------------------------------------
# request authentication
# ---------------------------------------------------------------------------


def _extract_credential(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        if len(parts) == 2 and parts[0].lower() == "basic":
            import base64

            try:
                decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
                return decoded
            except Exception:  # noqa: BLE001 - malformed basic header
                return ""
        # A bare token in Authorization is tolerated.
        if len(parts) == 1:
            return parts[0].strip()
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key.strip()
    query_token = request.query_params.get("access_token") or request.query_params.get("api_key")
    return (query_token or "").strip()


async def _user_from_token(db: Database, token: str) -> AuthUser | None:
    if not token:
        return None
    digest = token_hash(token)
    row = await db.fetch_one("SELECT * FROM tokens WHERE token_hash = ?", (digest,))
    if row is None or int(row["revoked"] or 0) != 0:
        return None
    if not hmac.compare_digest(str(row["token_hash"]), digest):
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and now_ms() > int(expires_at):
        await db.execute(
            "UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (digest,)
        )
        return None
    user = await find_user_by_id(db, str(row["user_id"]))
    if user is None or user.disabled:
        return None
    last_used = row["last_used_at"]
    if last_used is None or now_ms() - int(last_used) > 60_000:
        # At most one write per minute per token: keeps auth cheap under load.
        await db.execute(
            "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", (now_ms(), digest)
        )
    return user


async def _user_from_credentials(db: Database, credential: str) -> AuthUser | None:
    """``username:password`` in the Authorization header (convenience for hand entry)."""
    if ":" not in credential:
        return None
    username, _, password = credential.partition(":")
    if not username or not password:
        return None
    row = await find_user_by_username(db, username)
    if row is None or int(row["disabled"] or 0):
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    return _row_to_user(row)


async def authenticate(request: Request) -> AuthUser:
    """Resolve the caller or raise a 401 :class:`ApiError`."""
    state = request.app.state
    db: Database = state.db
    credential = _extract_credential(request)
    if not credential:
        raise authentication_error("缺少凭证：请使用 Authorization: Bearer <token> 或 x-api-key")
    user = await _user_from_token(db, credential)
    if user is None:
        user = await _user_from_credentials(db, credential)
    if user is None:
        raise authentication_error("凭证无效或已被吊销")
    return user


async def optional_user(request: Request) -> AuthUser | None:
    try:
        return await authenticate(request)
    except ApiError:
        return None


async def require_admin(request: Request) -> AuthUser:
    user = await authenticate(request)
    if user.role != "admin":
        raise forbidden("需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


GENERIC_INVITE_ERROR = "邀请码无效或不可用"
GENERIC_LOGIN_ERROR = "用户名或密码错误"


async def consume_invite_locked(db: Database, code: str) -> None:
    """Validate and atomically consume one invite use (inside a transaction)."""
    candidate = (code or "").strip()
    if not candidate:
        raise forbidden(GENERIC_INVITE_ERROR)
    row = await db.fetch_one("SELECT * FROM invites WHERE code = ?", (candidate,))
    if row is None or int(row["revoked"] or 0):
        raise forbidden(GENERIC_INVITE_ERROR)
    expires_at = row["expires_at"]
    if expires_at is not None and now_ms() > int(expires_at):
        raise forbidden(GENERIC_INVITE_ERROR)
    max_uses = int(row["max_uses"] or 0)
    used = int(row["used_count"] or 0)
    if max_uses > 0 and used >= max_uses:
        raise forbidden(GENERIC_INVITE_ERROR)
    cursor = await db.conn.execute(  # type: ignore[union-attr]
        "UPDATE invites SET used_count = used_count + 1"
        " WHERE code = ? AND revoked = 0 AND (max_uses = 0 OR used_count < max_uses)",
        (candidate,),
    )
    if cursor.rowcount != 1:
        raise forbidden(GENERIC_INVITE_ERROR)


@router.post("/auth/register", response_model=LoginResponse)
async def register(payload: RegisterRequest, request: Request) -> LoginResponse:
    state = request.app.state
    db: Database = state.db
    settings: Settings = state.settings
    limiters = getattr(state, "limiters", None)
    ip = client_ip(request, settings)
    if limiters is not None:
        allowed, retry_after = limiters.register_by_ip.check(ip)
        if not allowed:
            raise retry_after_error("注册尝试过于频繁，请稍后再试", int(retry_after) + 1)
        limiters.register_by_ip.hit(ip)

    username = normalise_username(payload.username)
    validate_password(payload.password, settings)

    # The whole "first user?" decision + inserts run in one real transaction.
    try:
        async with db.transaction():
            existing = await count_users(db)
            if existing == 0:
                # Bootstrap: the first account becomes admin and needs no invite.
                role = "admin"
            else:
                mode = settings.registration_mode
                if mode == "closed":
                    raise forbidden("注册暂未开放")
                if mode == "invite":
                    code = (payload.invite_code or "").strip()
                    if settings.invite_code and code and hmac.compare_digest(
                        code, settings.invite_code
                    ):
                        pass  # legacy shared code from env
                    else:
                        await consume_invite_locked(db, code)
                role = "user"

            user_id = f"u_{secrets.token_hex(12)}"
            stamp = now_ms()
            await db.execute(
                "INSERT INTO users (id, username, username_key, password_hash, role, disabled,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    user_id,
                    username,
                    username.lower(),
                    hash_password(payload.password, n=settings.scrypt_n),
                    role,
                    stamp,
                    stamp,
                ),
                commit=False,
            )
            token = secrets.token_hex(32)
            expires_at = (
                stamp + settings.token_ttl_days * 86_400_000
                if settings.token_ttl_days > 0
                else None
            )
            await db.execute(
                "INSERT INTO tokens (token_hash, user_id, created_at, revoked, label, expires_at)"
                " VALUES (?, ?, ?, 0, 'register', ?)",
                (token_hash(token), user_id, stamp, expires_at),
                commit=False,
            )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise ApiError(409, "用户名不可用", "conflict_error", param="username") from exc
        raise
    await audit.record(
        request,
        audit.USER_CREATED,
        actor_id=user_id,
        actor_name=username,
        target=user_id,
        detail={
            "role": role,
            "bootstrap": existing == 0,
            "invite": bool((payload.invite_code or "").strip()) and existing != 0,
        },
        db=db,
        settings=settings,
    )
    return LoginResponse(
        token=token, user=UserOut(id=user_id, username=username, role=role, createdAt=stamp)
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request) -> LoginResponse:
    state = request.app.state
    db: Database = state.db
    settings: Settings = state.settings
    limiters = getattr(state, "limiters", None)
    ip = client_ip(request, settings)
    user_key = f"{ip}|{(payload.username or '').strip().lower()}"

    if limiters is not None:
        for limiter, key in (
            (limiters.login_by_ip, ip),
            (limiters.login_by_ip_user, user_key),
        ):
            allowed, retry_after = limiter.check(key)
            if not allowed:
                raise retry_after_error(
                    "登录失败次数过多，请稍后再试", int(retry_after) + 1
                )

    row = await find_user_by_username(db, payload.username)
    valid = (
        row is not None
        and not int(row["disabled"] or 0)
        and verify_password(payload.password, str(row["password_hash"]))
    )
    if not valid:
        if limiters is not None:
            limiters.login_by_ip.hit(ip)
            limiters.login_by_ip_user.hit(user_key)
        raise authentication_error(GENERIC_LOGIN_ERROR)
    user = _row_to_user(row)
    if limiters is not None:
        limiters.login_by_ip_user.reset(user_key)
    token = await issue_token(db, user.id, "login", ttl_days=settings.token_ttl_days)
    return LoginResponse(token=token, user=user.to_out())


@router.get("/auth/me", response_model=UserOut)
async def me(request: Request) -> UserOut:
    user = await authenticate(request)
    return user.to_out()


class LogoutResponse(BaseModel):
    ok: bool = True


class ChangePasswordResponse(BaseModel):
    ok: bool = True
    revokedOtherTokens: int = 0


@router.post("/auth/change-password", response_model=ChangePasswordResponse)
async def change_password(payload: ChangePasswordRequest, request: Request) -> ChangePasswordResponse:
    """Change the signed-in user's own password.

    The current token stays valid; every other token of this user is revoked so a
    lost device stops working as soon as the password is rotated.
    """
    state = request.app.state
    db: Database = state.db
    settings: Settings = state.settings
    user = await authenticate(request)
    row = await db.fetch_one("SELECT password_hash FROM users WHERE id = ?", (user.id,))
    if row is None:
        raise authentication_error("账号不存在")
    if not verify_password(payload.old_password, str(row["password_hash"])):
        raise authentication_error("原密码不正确")
    validate_password(payload.new_password, settings)
    if hmac.compare_digest(payload.old_password, payload.new_password):
        raise ApiError(400, "新密码不能与原密码相同", "invalid_request_error", param="new_password")

    await db.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(payload.new_password, n=settings.scrypt_n), now_ms(), user.id),
    )
    current = _extract_credential(request)
    current_hash = token_hash(current) if current else ""
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
        audit.PASSWORD_CHANGED,
        actor_id=user.id,
        actor_name=user.username,
        target=user.id,
        detail={"revokedOtherTokens": revoked},
        db=db,
        settings=settings,
    )
    return ChangePasswordResponse(ok=True, revokedOtherTokens=revoked)


@router.post("/auth/logout", response_model=LogoutResponse)
async def logout(request: Request) -> LogoutResponse:
    state = request.app.state
    db: Database = state.db
    credential = _extract_credential(request)
    if credential:
        await db.execute(
            "UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (token_hash(credential),)
        )
    return LogoutResponse(ok=True)


def _ts() -> int:
    return int(time.time() * 1000)
