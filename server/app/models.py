"""Wire models and error helpers.

The Anthropic side is intentionally permissive: unknown block types must be
accepted and ignored (the client re-sends ``server_tool_use``/``web_fetch_tool_result``
history that the upstream cannot use), so request bodies keep raw ``dict``s for
messages/blocks instead of a rigid discriminated union.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


class ApiError(Exception):
    """An error that renders as ``{"error": {"message": ..., "type": ...}}``."""

    def __init__(
        self,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
        *,
        headers: dict[str, str] | None = None,
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.headers = headers or {}
        self.param = param
        self.code = code

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }
        return JSONResponse(body, status_code=self.status_code, headers=self.headers)


def error_body(
    message: str,
    error_type: str = "invalid_request_error",
    *,
    param: str | None = None,
    code: str | None = None,
) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": param, "code": code}}


def authentication_error(message: str = "缺少或无效的凭证") -> ApiError:
    return ApiError(401, message, "authentication_error")


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, message, "not_found_error")


def forbidden(message: str = "没有权限") -> ApiError:
    return ApiError(403, message, "permission_error")


async def read_limited_body(request: Request, limit: int) -> bytes:
    """Read the request body, aborting with 413 once ``limit`` bytes are exceeded."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise ApiError(413, f"请求体过大（上限 {limit} 字节）", "request_too_large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, f"请求体过大（上限 {limit} 字节）", "request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    invite_code: str | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    username: str
    role: str
    createdAt: int


class LoginResponse(BaseModel):
    token: str
    user: UserOut


# ---------------------------------------------------------------------------
# Anthropic messages request
# ---------------------------------------------------------------------------


class AnthropicMessageRequest(BaseModel):
    """Permissive view of ``CreateMessageRequest`` from the chat-demo client."""

    model_config = ConfigDict(extra="allow")

    model: str
    system: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    thinking: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    safety_check: dict[str, Any] | None = None
    title: dict[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    stream: bool | None = None
    metadata: dict[str, Any] | None = None

    def thinking_enabled(self) -> bool:
        return bool(self.thinking and self.thinking.get("type") == "enabled")

    def title_enabled(self) -> bool:
        return bool(self.title and self.title.get("enabled") is True)

    def declares_search_tools(self) -> tuple[bool, bool]:
        """Return ``(wants_web_search, wants_read_url)``."""
        names = set()
        for tool in self.tools or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or tool.get("type")
            if isinstance(name, str):
                names.add(name)
        return "deepseek_web_search" in names, "deepseek_read_url" in names

    def first_user_text(self, limit: int = 800) -> str:
        for message in self.messages:
            if message.get("role") != "user":
                continue
            text = user_text_only(message.get("content"))
            if text.strip():
                return text.strip()[:limit]
        return ""


def user_text_only(content: Any) -> str:
    """Concatenate text blocks of a user message, ignoring images/documents."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


class FileObject(BaseModel):
    id: str
    type: Literal["file"] = "file"
    size_bytes: int
    created_at: str
    filename: str
    mime_type: str


class FileDeleted(BaseModel):
    id: str
    type: Literal["file_deleted"] = "file_deleted"


# ---------------------------------------------------------------------------
# speech
# ---------------------------------------------------------------------------


class TtsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str = Field(min_length=1)
    voice_id: str | None = None
    format: str = "pcm"
    filter_markdown: bool = True


# ---------------------------------------------------------------------------
# admin
# ---------------------------------------------------------------------------


class AdminUserCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: Literal["user", "admin"] = "user"


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    password: str | None = None


class ChangePasswordRequest(BaseModel):
    """Self-service password change for the signed-in user."""

    model_config = ConfigDict(extra="ignore")
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class AdminUserOut(UserOut):
    disabled: bool = False
    sessionCount: int = 0
    fileCount: int = 0
