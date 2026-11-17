"""Per-user credentials: masking, merge semantics and capability helpers.

Secrets live in ``user_settings.data`` (SQLite, plaintext at rest — documented in
the README).  They are **write-only** over the API: reads return a mask such as
``sk-ab…12cd`` plus ``has_key``, never the full value.

PUT semantics for every credential field:
  * a non-empty string  → set
  * ``""`` or ``null``  → clear
  * ``"__keep__"``      → leave the stored value untouched (UI submits a masked
                          field unchanged)
  * field omitted       → leave untouched
"""

from __future__ import annotations

from typing import Any

from .config import SEARCH_PROVIDERS
from .models import ApiError

KEEP = "__keep__"
MAX_FIELD_LENGTH = 512

PROVIDERS = ("deepseek", "openai", "deepseek_tools", "disabled")
CREDENTIAL_KINDS = ("official", "tts", "asr", "search")

#: Fields accepted per block.  Anything else is ignored (shape validation only).
BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "official": ("api_key", "base_url"),
    "tts": ("provider", "base_url", "api_key", "model", "voice"),
    "asr": ("provider", "base_url", "api_key", "model"),
    "search": ("mode", "provider", "base_url", "api_key", "max_results"),
}

#: Fields that must never be echoed back.
SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "official": ("api_key",),
    "tts": ("api_key",),
    "asr": ("api_key",),
    "search": ("api_key",),
}


def mask_secret(value: Any) -> str | None:
    """Return a display mask that never reveals the full secret."""
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 8:
        if len(value) <= 2:
            return "…"
        return f"{value[0]}…{value[-1]}"
    return f"{value[:5]}…{value[-4:]}"


def _validate_string(value: Any, field_name: str, kind: str) -> str:
    if not isinstance(value, str):
        raise ApiError(
            400,
            f"{kind}.{field_name} 必须是字符串",
            "invalid_request_error",
            param=f"{kind}.{field_name}",
        )
    if len(value) > MAX_FIELD_LENGTH:
        raise ApiError(
            400,
            f"{kind}.{field_name} 过长（最多 {MAX_FIELD_LENGTH} 字符）",
            "invalid_request_error",
            param=f"{kind}.{field_name}",
        )
    if any(ord(ch) < 32 for ch in value):
        raise ApiError(
            400,
            f"{kind}.{field_name} 含有控制字符",
            "invalid_request_error",
            param=f"{kind}.{field_name}",
        )
    return value


def merge_block(kind: str, existing: Any, incoming: Any) -> dict[str, Any]:
    """Apply write-only semantics and return the block to persist."""
    merged: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    if incoming is None:
        return {}  # explicit null clears the whole block
    if not isinstance(incoming, dict):
        raise ApiError(
            400,
            f"settings.{kind} 必须是对象",
            "invalid_request_error",
            param=kind,
        )
    for field_name, raw in incoming.items():
        if field_name not in BLOCK_FIELDS[kind]:
            continue  # ignore unknown fields (shape validation only)
        if raw == KEEP:
            continue
        if raw is None or raw == "":
            merged.pop(field_name, None)
            continue
        if field_name == "max_results":
            if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 20:
                raise ApiError(
                    400,
                    "search.max_results 必须是 1~20 的整数",
                    "invalid_request_error",
                    param="search.max_results",
                )
            merged[field_name] = raw
            continue
        value = _validate_string(raw, field_name, kind)
        if field_name == "provider":
            allowed = SEARCH_PROVIDERS if kind == "search" else PROVIDERS
            if value not in allowed:
                raise ApiError(
                    400,
                    f"{kind}.provider 不支持：{value}"
                    f"（可选：{', '.join(allowed)}）",
                    "invalid_request_error",
                    param=f"{kind}.provider",
                )
        elif field_name == "mode":
            if value not in ("dgx", "official", "off"):
                raise ApiError(
                    400,
                    f"search.mode 不支持：{value}（可选：dgx, official, off）",
                    "invalid_request_error",
                    param="search.mode",
                )
        merged[field_name] = value
    return merged


def public_block(kind: str, stored: Any) -> dict[str, Any]:
    """Masked, API-safe view of one credential block."""
    block = dict(stored) if isinstance(stored, dict) else {}
    out: dict[str, Any] = {}
    for field_name in BLOCK_FIELDS[kind]:
        if field_name in SECRET_FIELDS[kind]:
            continue
        out[field_name] = block.get(field_name)
    secrets = SECRET_FIELDS[kind]
    for field_name in secrets:
        out[field_name] = mask_secret(block.get(field_name))
    out["has_key"] = any(bool(block.get(field_name)) for field_name in secrets)
    return out


def public_settings(stored: Any) -> dict[str, Any]:
    """Full settings document with every credential masked."""
    source = dict(stored) if isinstance(stored, dict) else {}
    out = {key: value for key, value in source.items() if key not in CREDENTIAL_KINDS}
    for kind in CREDENTIAL_KINDS:
        out[kind] = public_block(kind, source.get(kind))
    return out


def merge_settings(existing: Any, incoming: Any) -> dict[str, Any]:
    """Return the settings document to persist.

    Non-credential keys keep the old replace-wholesale behaviour; credential
    blocks are merged (and are never dropped by omission, so a UI that submits
    masked fields cannot silently forget a key).
    """
    if not isinstance(incoming, dict):
        raise ApiError(400, "设置必须是 JSON 对象", "invalid_request_error")
    previous = dict(existing) if isinstance(existing, dict) else {}
    merged: dict[str, Any] = {
        key: value for key, value in incoming.items() if key not in CREDENTIAL_KINDS
    }
    for kind in CREDENTIAL_KINDS:
        if kind in incoming:
            merged[kind] = merge_block(kind, previous.get(kind), incoming[kind])
        elif isinstance(previous.get(kind), dict):
            merged[kind] = previous[kind]
    return merged


# ---------------------------------------------------------------------------
# resolution helpers shared by speech / search / capabilities
# ---------------------------------------------------------------------------


def user_official_key(user_settings: Any) -> str:
    if not isinstance(user_settings, dict):
        return ""
    official = user_settings.get("official")
    if isinstance(official, dict):
        key = official.get("api_key")
        if isinstance(key, str):
            return key
    return ""


def user_official_base_url(user_settings: Any) -> str:
    if not isinstance(user_settings, dict):
        return ""
    official = user_settings.get("official")
    if isinstance(official, dict):
        base = official.get("base_url")
        if isinstance(base, str):
            return base
    return ""


def user_block(user_settings: Any, kind: str) -> dict[str, Any]:
    if not isinstance(user_settings, dict):
        return {}
    block = user_settings.get(kind)
    return block if isinstance(block, dict) else {}


def user_has_speech_override(user_settings: Any, kind: str) -> bool:
    block = user_block(user_settings, kind)
    return any(
        isinstance(block.get(field), str) and block.get(field)
        for field in ("provider", "base_url", "api_key", "model")
    )
