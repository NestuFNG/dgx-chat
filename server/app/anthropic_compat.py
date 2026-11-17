"""Anthropic Messages compatibility layer (SPEC §3).

Responsibilities:
  * translate the client's Anthropic-shaped request into a DGX (OpenAI compatible)
    chat-completions request,
  * translate the upstream stream back into the exact SSE event sequence the
    chat-demo client parses (``parseEvent.ts``),
  * generate the session title concurrently and hand it back in ``message_delta``,
  * relay the official endpoint verbatim when ``SEARCH_MODE=official``.

Hard constraint (SPEC §3.0): the ``system`` string is forwarded **byte for byte**
as the single leading OpenAI ``system`` message.  Nothing is trimmed, prefixed,
appended or merged; ``latest_reminder`` is folded into the *following user turn*
instead, because vLLM chat templates require exactly one leading system message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from .auth import AuthUser, authenticate
from .config import Settings
from .db import Database, now_ms
from .files import FileStore
from .models import AnthropicMessageRequest, ApiError, user_text_only
from .search import SearchPlan, SearchService, extract_urls
from . import credentials
from .security import (
    check_token_quota,
    client_ip,
    log_usage,
    reject_oversized_content_length,
    retry_after_error,
)
from .upstream import (
    AnthropicUpstream,
    UpstreamClient,
    UpstreamError,
    thinking_params,
)
from . import search as search_module

logger = logging.getLogger("dgxchat.messages")

router = APIRouter(tags=["messages"])

SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"

TEXTUAL_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/typescript",
    "application/x-sh",
    "application/toml",
    "application/x-ndjson",
    "application/sql",
    "image/svg+xml",
}
TEXTUAL_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".log", ".tex", ".py", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".go", ".rs", ".rb", ".php", ".pl", ".lua", ".swift", ".m", ".sql", ".sh", ".bat",
    ".ps1", ".html", ".htm", ".css", ".scss", ".xml", ".srt", ".vtt", ".diff", ".patch",
}

_ASSISTANT_IGNORED = {
    "thinking",
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
    "tool_use",
    "tool_result",
}

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")
_PARTIAL_CITATION_RE = re.compile(r"\[(\d{0,2})$")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def sse_event(event: str, data: dict[str, Any]) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def sse_comment(text: str = "ping") -> bytes:
    return f": {text}\n\n".encode("utf-8")


def zero_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }


def usage_from_openai(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return zero_usage()
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
    created = int(details.get("created_cache_tokens") or 0) if isinstance(details, dict) else 0
    cached = max(0, min(cached, prompt))
    return {
        "input_tokens": max(0, prompt - cached),
        "cache_creation_input_tokens": max(0, created),
        "cache_read_input_tokens": cached,
        "output_tokens": completion,
    }


def reasoning_tokens_from_openai(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    details = raw.get("completion_tokens_details")
    if isinstance(details, dict):
        try:
            return int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def map_stop_reason(finish_reason: Any) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "refusal",
        "refusal": "refusal",
    }.get(str(finish_reason or "stop"), "end_turn")


def make_data_uri(media_type: str, data: bytes) -> str:
    import base64

    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type or 'application/octet-stream'};base64,{encoded}"


def is_textual_document(mime_type: str, filename: str) -> bool:
    mime = (mime_type or "").lower()
    if mime in TEXTUAL_MIME_EXACT or mime.startswith("text/"):
        return True
    suffix = ""
    name = (filename or "").lower()
    dot = name.rfind(".")
    if dot >= 0:
        suffix = name[dot:]
    if suffix in TEXTUAL_EXTENSIONS:
        return True
    # Unknown application/* types: allow if it decodes as UTF-8 text.
    return mime.startswith("application/") and any(
        marker in mime for marker in ("json", "xml", "yaml", "toml", "text", "csv")
    )


PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
DOCX_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/docx",
}
XLSX_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/xlsx",
}


def document_kind(mime_type: str, filename: str) -> str:
    mime = (mime_type or "").lower()
    name = (filename or "").lower()
    suffix = name[name.rfind(".") :] if "." in name else ""
    if mime in PDF_MIME_TYPES or suffix == ".pdf":
        return "pdf"
    if mime in DOCX_MIME_TYPES or suffix in (".docx",):
        return "docx"
    if mime in XLSX_MIME_TYPES or suffix in (".xlsx", ".xls"):
        return "xlsx"
    if is_textual_document(mime_type, filename):
        return "text"
    return "binary"


def extract_pdf_text(data: bytes, limit: int) -> str:
    """Extract text from a PDF with pypdf; raises ValueError on failure."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ValueError("服务器未安装 pypdf，无法解析 PDF") from exc
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - malformed PDFs raise many types
        raise ValueError(f"PDF 结构无法解析（{type(exc).__name__}）") from exc
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("PDF 已加密，无法读取内容") from exc
    pieces: list[str] = []
    total = 0
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill the doc
            logger.info("pdf page %s extract failed: %s", index, exc)
            continue
        if text.strip():
            pieces.append(text.strip())
            total += len(text)
        if total >= limit:
            break
    return "\n\n".join(pieces)


def extract_docx_text(data: bytes, limit: int) -> str:
    """Extract paragraphs and tables from a .docx with python-docx."""
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ValueError("服务器未安装 python-docx，无法解析 DOCX") from exc
    import io

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"DOCX 结构无法解析（{type(exc).__name__}）") from exc
    parts: list[str] = []
    total = 0
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text)
        if total >= limit:
            return "\n".join(parts)
    for table in document.tables:
        for row in table.rows:
            cells = [(cell.text or "").strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                line = " | ".join(cells)
                parts.append(line)
                total += len(line)
                if total >= limit:
                    return "\n".join(parts)
    return "\n".join(parts)


def sanitize_title(raw: Any, limit: int = 30) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    text = text.splitlines()[0].strip()
    text = re.sub(r"^(?:标题|title)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" \t\"'“”‘’《》〈〉「」『』【】[]（）()。.,，、；;!！?？")
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit].rstrip(" 　。.,，、；;!！?？")
    return text or None


# ---------------------------------------------------------------------------
# translation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Translation:
    payload: dict[str, Any]
    notices: list[str] = field(default_factory=list)
    thinking_enabled: bool = False
    prefill: bool = False
    upstream_model: str = ""
    system: str | None = None
    user_query: str = ""
    urls: list[str] = field(default_factory=list)
    wants_search: bool = False
    wants_read_url: bool = False


def thinking_params_for_tests(enabled: bool, settings: Settings) -> dict[str, Any]:
    """Backwards-compatible helper (dialect lives in :mod:`app.upstream`)."""
    return thinking_params(enabled, settings.thinking_disable_mode)


async def _image_part(
    source: Any,
    *,
    user_id: str,
    store: FileStore,
    settings: Settings,
    notices: list[str],
) -> dict[str, Any] | None:
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type") or "image/png"
        data = source.get("data")
        if not isinstance(data, str):
            return None
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
    if source_type == "url":
        url = source.get("url")
        if isinstance(url, str) and url:
            return {"type": "image_url", "image_url": {"url": url}}
        return None
    if source_type == "file":
        file_id = source.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            notices.append("ignored_image_without_file_id")
            return None
        try:
            data, record = await store.read_limited(
                user_id, file_id, settings.max_image_bytes
            )
        except ApiError as exc:
            logger.info("image file %s unavailable: %s", file_id, exc.message)
            notices.append("image_file_missing")
            return None
        return {
            "type": "image_url",
            "image_url": {"url": make_data_uri(record.mime_type, data)},
        }
    return None


async def _document_text(
    source: Any,
    *,
    user_id: str,
    store: FileStore,
    settings: Settings,
    notices: list[str],
) -> str | None:
    if not isinstance(source, dict):
        return None
    file_id = source.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        notices.append("ignored_document_without_file_id")
        return None

    metadata = await store.metadata(user_id, file_id)
    kind = document_kind(
        metadata.mime_type if metadata else "", metadata.filename if metadata else ""
    )
    if kind == "xlsx":
        # openpyxl is not a dependency: skip spreadsheets but say so via x-dgx-notice.
        notices.append("xlsx_not_supported")
        return None
    if kind == "binary":
        notices.append("unsupported_document_skipped")
        return None

    try:
        data, record = await store.read_limited(
            user_id, file_id, settings.max_document_bytes
        )
    except ApiError as exc:
        if exc.status_code == 404:
            logger.info("document %s unavailable: %s", file_id, exc.message)
            notices.append("document_file_missing")
            return None
        raise  # 413 (too large) and friends must reach the client unchanged

    limit = max(100, int(getattr(settings, "max_document_chars", 120_000) or 120_000))
    name = record.filename or file_id
    if kind == "pdf":
        try:
            text = extract_pdf_text(data, limit)
        except ValueError as exc:
            raise ApiError(400, f"无法读取 PDF「{name}」：{exc}", "invalid_request_error") from exc
        if not text.strip():
            raise ApiError(
                400,
                f"PDF「{name}」里没有可提取的文本（可能是扫描件，需要 OCR）",
                "invalid_request_error",
            )
    elif kind == "docx":
        try:
            text = extract_docx_text(data, limit)
        except ValueError as exc:
            raise ApiError(400, f"无法读取 DOCX「{name}」：{exc}", "invalid_request_error") from exc
        if not text.strip():
            raise ApiError(400, f"DOCX「{name}」里没有可提取的文本", "invalid_request_error")
    else:
        # Plain text path: unchanged from the original implementation.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")
        if "\x00" in text[:4096]:
            notices.append("binary_document_skipped")
            return None

    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…[truncated]"
        notices.append("document_truncated")
    return f'<document name="{name}">\n{text}\n</document>'


async def user_content_to_openai(
    content: Any,
    *,
    user_id: str,
    store: FileStore,
    settings: Settings,
    notices: list[str],
) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif block_type == "image":
            part = await _image_part(
                block.get("source"),
                user_id=user_id,
                store=store,
                settings=settings,
                notices=notices,
            )
            if part is not None:
                images.append(part)
        elif block_type == "document":
            text = await _document_text(
                block.get("source"),
                user_id=user_id,
                store=store,
                settings=settings,
                notices=notices,
            )
            if text:
                texts.append(text)
        else:
            notices.append(f"ignored_user_block:{block_type}")
    joined = "\n\n".join(part for part in texts if part != "")
    if images:
        parts: list[dict[str, Any]] = []
        if joined:
            parts.append({"type": "text", "text": joined})
        parts.extend(images)
        return parts
    return joined


def assistant_content_to_text(content: Any) -> str:
    """Keep only assistant ``text`` blocks; thinking/tool history is dropped."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)


def _merge_message(messages: list[dict[str, Any]], message: dict[str, Any]) -> None:
    """Append, merging same-role neighbours so chat templates stay valid."""
    if messages and messages[-1]["role"] == message["role"] and message["role"] != "system":
        previous = messages[-1]
        previous_content = previous.get("content")
        new_content = message.get("content")
        if isinstance(previous_content, str) and isinstance(new_content, str):
            previous["content"] = (
                f"{previous_content}\n\n{new_content}" if previous_content else new_content
            )
            return
        if isinstance(previous_content, list):
            if isinstance(new_content, list):
                previous_content.extend(new_content)
            elif isinstance(new_content, str):
                previous_content.append({"type": "text", "text": new_content})
            return
        if isinstance(previous_content, str) and isinstance(new_content, list):
            merged: list[dict[str, Any]] = []
            if previous_content:
                merged.append({"type": "text", "text": previous_content})
            merged.extend(new_content)
            previous["content"] = merged
            return
    messages.append(message)


def fold_reminder(text: str, reminder: str) -> str:
    """``latest_reminder`` becomes a prefix on the following user turn."""
    reminder = reminder.strip()
    if not reminder:
        return text
    if not text:
        return reminder
    return f"reminder: {reminder}\n\n{text}"


async def build_translation(
    request: AnthropicMessageRequest,
    *,
    user_id: str,
    store: FileStore,
    settings: Settings,
) -> Translation:
    notices: list[str] = []
    thinking_enabled = request.thinking_enabled()
    messages: list[dict[str, Any]] = []

    system = request.system
    if system is not None:
        # §3.0 hard constraint: forwarded byte for byte, never rewritten.
        messages.append({"role": "system", "content": system})

    pending_reminder: str | None = None
    last_user_text = ""
    for raw_message in request.messages:
        if not isinstance(raw_message, dict):
            continue
        role = raw_message.get("role")
        content = raw_message.get("content")
        if role == "latest_reminder":
            text = content if isinstance(content, str) else user_text_only(content)
            if text and text.strip():
                pending_reminder = text
            continue
        if role == "user":
            converted = await user_content_to_openai(
                content,
                user_id=user_id,
                store=store,
                settings=settings,
                notices=notices,
            )
            text_only = user_text_only(content)
            if isinstance(converted, str):
                if pending_reminder:
                    converted = fold_reminder(converted, pending_reminder)
                elif not converted and pending_reminder:
                    converted = pending_reminder.strip()
                pending_reminder = None
                _merge_message(messages, {"role": "user", "content": converted})
            else:
                if pending_reminder:
                    converted = [{"type": "text", "text": pending_reminder.strip()}] + converted
                    pending_reminder = None
                _merge_message(messages, {"role": "user", "content": converted})
            if text_only.strip():
                last_user_text = text_only.strip()
            continue
        if role == "assistant":
            text = assistant_content_to_text(content)
            if text.strip():
                _merge_message(messages, {"role": "assistant", "content": text})
            continue
        notices.append(f"ignored_role:{role}")

    if pending_reminder:
        # Trailing reminder with no following user turn: keep it as user context.
        _merge_message(messages, {"role": "user", "content": pending_reminder.strip()})

    prefill = bool(messages) and messages[-1]["role"] == "assistant"

    wants_search, wants_read_url = request.declares_search_tools()
    upstream_model = settings.upstream_model(request.model)
    max_tokens = request.max_tokens or settings.default_max_tokens
    payload: dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": int(max_tokens),
    }
    if request.temperature is not None:
        payload["temperature"] = float(request.temperature)
    elif settings.temperature is not None:
        payload["temperature"] = settings.temperature
    if request.top_p is not None:
        payload["top_p"] = float(request.top_p)
    elif settings.top_p is not None:
        payload["top_p"] = settings.top_p
    if request.stop_sequences:
        payload["stop"] = list(request.stop_sequences)
    # NB: thinking parameters are NOT baked in here.  Each upstream endpoint has
    # its own dialect (app/upstream.py), applied at request time.
    if prefill:
        payload["continue_final_message"] = True
        payload["add_generation_prompt"] = False

    urls = extract_urls(last_user_text, settings.search.max_pages_per_turn)
    return Translation(
        payload=payload,
        notices=notices,
        thinking_enabled=thinking_enabled,
        prefill=prefill,
        upstream_model=upstream_model,
        system=system,
        user_query=last_user_text or request.first_user_text(),
        urls=urls,
        wants_search=wants_search,
        wants_read_url=wants_read_url,
    )


def drop_prefill(payload: dict[str, Any], prompt: str = "继续：") -> dict[str, Any]:
    """Fallback when the upstream rejects ``continue_final_message``."""
    cloned = json.loads(json.dumps(payload))
    cloned.pop("continue_final_message", None)
    cloned.pop("add_generation_prompt", None)
    messages = cloned.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{content}\n\n{prompt}" if content else prompt
        elif isinstance(content, list):
            content.append({"type": "text", "text": prompt})
        return cloned
    messages.append({"role": "user", "content": prompt})
    cloned["messages"] = messages
    return cloned


def append_user_context(payload: dict[str, Any], context: str) -> dict[str, Any]:
    """Append retrieval context to the last user turn (never to the system message)."""
    if not context.strip():
        return payload
    cloned = json.loads(json.dumps(payload))
    messages = cloned.get("messages") or []
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = f"{content}\n\n{context}" if content else context
        elif isinstance(content, list):
            content.append({"type": "text", "text": context})
        return cloned
    messages.append({"role": "user", "content": context})
    cloned["messages"] = messages
    return cloned


# ---------------------------------------------------------------------------
# title generation
# ---------------------------------------------------------------------------

TITLE_INSTRUCTION = (
    "请为下面这段对话起一个简短标题，不超过 {limit} 个字符，"
    "只输出标题本身，不要引号、不要句号、不要解释。\n\n"
    "用户：{user}\n助手：{assistant}"
)


def build_title_payload(
    translation: Translation,
    assistant_text: str,
    settings: Settings,
) -> dict[str, Any]:
    user = translation.user_query.strip()[:600]
    reply = re.sub(r"\s+", " ", assistant_text).strip()[:800]
    return {
        "model": translation.upstream_model,
        "messages": [
            {
                "role": "user",
                "content": TITLE_INSTRUCTION.format(
                    limit=settings.title_max_chars, user=user, assistant=reply
                ),
            }
        ],
        "max_tokens": 64,
        "temperature": 0.3,
    }


async def generate_title(
    upstream: UpstreamClient,
    translation: Translation,
    assistant_text: str,
    settings: Settings,
) -> str | None:
    if not translation.user_query.strip() and not assistant_text.strip():
        return None
    payload = build_title_payload(translation, assistant_text, settings)
    # Title generation must never think: otherwise the model can spend the whole
    # token budget in the reasoning channel and return empty content.
    data = await upstream.complete_chat(payload, thinking_enabled=False)
    choices = data.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    content = message.get("content")
    title = sanitize_title(content, settings.title_max_chars)
    if title is None:
        # Some upstreams leave the answer in the reasoning channel when the
        # thinking switch was not honoured; salvage it rather than lose the title.
        title = sanitize_title(message.get("reasoning"), settings.title_max_chars)
    return title


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


class MessageStreamer:
    def __init__(
        self,
        *,
        settings: Settings,
        user: AuthUser,
        request: AnthropicMessageRequest,
        translation: Translation,
        plan: SearchPlan,
        upstream: UpstreamClient,
        db: Database,
        client_headers: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        self.user = user
        self.request = request
        self.translation = translation
        self.plan = plan
        self.upstream = upstream
        self.db = db
        self.message_id = str(uuid.uuid4())
        self.next_index = 0
        self.thinking_index: int | None = None
        self.thinking_open = False
        self.text_index: int | None = None
        self.text_open = False
        self.text_accum = ""
        self.pending_text = ""
        self.reasoning_chars = 0
        self.usage: dict[str, int] = zero_usage()
        self.reasoning_tokens = 0
        self.stop_reason = "end_turn"
        self.started_at = now_ms()
        self.title_task: asyncio.Task[str | None] | None = None
        self.any_block_started = False
        self.assistant_parts: list[str] = []

    # -- block machinery ----------------------------------------------------
    def _open_block(self, content_block: dict[str, Any]) -> list[bytes]:
        index = self.next_index
        self.next_index += 1
        self.any_block_started = True
        return [
            sse_event(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": content_block},
            )
        ]

    def _close_block(self, index: int) -> bytes:
        return sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": index}
        )

    def _start_thinking(self) -> list[bytes]:
        out = self._open_block(
            {"type": "thinking", "thinking": "", "signature": self.message_id}
        )
        self.thinking_index = self.next_index - 1
        self.thinking_open = True
        return out

    def _stop_thinking(self) -> list[bytes]:
        if not self.thinking_open or self.thinking_index is None:
            return []
        self.thinking_open = False
        return [self._close_block(self.thinking_index)]

    def _start_text(self) -> list[bytes]:
        out = self._open_block({"type": "text", "text": ""})
        self.text_index = self.next_index - 1
        self.text_open = True
        return out

    def _stop_text(self) -> list[bytes]:
        if not self.text_open or self.text_index is None:
            return []
        out: list[bytes] = []
        if self.pending_text:
            out.append(self._text_delta(self.pending_text))
            self.pending_text = ""
        self.text_open = False
        out.append(self._close_block(self.text_index))
        return out

    def _text_delta(self, text: str) -> bytes:
        self.text_accum += text
        return sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.text_index,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def _citation_delta(self, source: Any) -> bytes:
        citation = {
            "type": "web_search_result_location",
            "url": source.url,
            "title": source.title or None,
            "cited_text": self.text_accum.strip(),
            "encrypted_index": "",
        }
        return sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": self.text_index,
                "delta": {"type": "citations_delta", "citation": citation},
            },
        )

    # -- text with citation markers ----------------------------------------
    def _emit_text(self, text: str) -> list[bytes]:
        """Emit text, turning ``[n]`` markers into citations when we have sources."""
        if not text:
            return []
        if not self.plan.sources:
            return [self._text_delta(text)]
        out: list[bytes] = []
        buffer = self.pending_text + text
        self.pending_text = ""
        position = 0
        for match in _CITATION_RE.finditer(buffer):
            number = int(match.group(1))
            if not 1 <= number <= len(self.plan.sources):
                continue
            chunk = buffer[position : match.start()]
            if chunk:
                out.append(self._text_delta(chunk))
            out.append(self._citation_delta(self.plan.sources[number - 1]))
            position = match.end()
        remainder = buffer[position:]
        partial = _PARTIAL_CITATION_RE.search(remainder)
        if partial is not None:
            tail = remainder[partial.start() :]
            head = remainder[: partial.start()]
            if head:
                out.append(self._text_delta(head))
            self.pending_text = tail
        elif remainder:
            out.append(self._text_delta(remainder))
        return out

    # -- search blocks ------------------------------------------------------
    def _search_blocks(self) -> list[bytes]:
        out: list[bytes] = []
        for block in self.plan.blocks:
            out.extend(self._open_block(block))
            out.append(self._close_block(self.next_index - 1))
        return out

    # -- title --------------------------------------------------------------
    def _maybe_start_title(self, force: bool = False) -> None:
        if not self.request.title_enabled() or self.title_task is not None:
            return
        text = "".join(self.assistant_parts)
        if not force and len(text) < 120:
            return
        if not text.strip() and not force:
            return
        self.title_task = asyncio.create_task(
            generate_title(self.upstream, self.translation, text, self.settings)
        )

    async def _collect_title(self) -> str | None:
        self._maybe_start_title(force=True)
        task = self.title_task
        if task is None:
            return None
        try:
            return await asyncio.wait_for(task, timeout=self.settings.title_timeout)
        except (asyncio.TimeoutError, UpstreamError, Exception) as exc:  # noqa: BLE001
            if not isinstance(task, asyncio.CancelledError):
                logger.info("title generation failed: %s", exc)
            task.cancel()
            return None

    # -- upstream chunks ----------------------------------------------------
    async def _chunks(self) -> AsyncIterator[dict[str, Any]]:
        payload = self.translation.payload
        try:
            async for chunk in self.upstream.iter_chat(
                payload, thinking_enabled=self.translation.thinking_enabled
            ):
                yield chunk
        except UpstreamError:
            if not self.translation.prefill or self.any_block_started:
                raise
            logger.warning(
                "upstream rejected continue_final_message; retrying with a 继续： prompt"
            )
            payload = drop_prefill(payload)
            self.translation.payload = payload
            async for chunk in self.upstream.iter_chat(
                payload, thinking_enabled=self.translation.thinking_enabled
            ):
                yield chunk

    def _consume_chunk(self, chunk: dict[str, Any]) -> list[bytes]:
        out: list[bytes] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = usage_from_openai(usage)
            self.reasoning_tokens = reasoning_tokens_from_openai(usage)
        choices = chunk.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning")
            content = delta.get("content")
            if isinstance(reasoning, str) and reasoning:
                if self.translation.thinking_enabled and not self.text_open:
                    if not self.thinking_open:
                        out.extend(self._start_thinking())
                    self.reasoning_chars += len(reasoning)
                    out.append(
                        sse_event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": self.thinking_index,
                                "delta": {"type": "thinking_delta", "thinking": reasoning},
                            },
                        )
                    )
            if isinstance(content, str) and content:
                if not self.text_open:
                    out.extend(self._stop_thinking())
                    out.extend(self._start_text())
                out.extend(self._emit_text(content))
                self.assistant_parts.append(content)
                self._maybe_start_title()
            finish = choice.get("finish_reason")
            if finish:
                self.stop_reason = map_stop_reason(finish)
        return out

    # -- main generator -----------------------------------------------------
    async def events(self) -> AsyncIterator[bytes]:
        client_model = self.request.model
        yield sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": client_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": zero_usage(),
                },
            },
        )
        for frame in self._search_blocks():
            yield frame

        error: UpstreamError | None = None
        try:
            async for chunk in self._chunks():
                for frame in self._consume_chunk(chunk):
                    yield frame
        except UpstreamError as exc:
            error = exc
        except asyncio.CancelledError:
            self._cancel_title()
            raise
        except Exception as exc:  # noqa: BLE001 - never leak a traceback into the stream
            logger.exception("unexpected error while streaming")
            error = UpstreamError(f"服务端处理流式响应时出错：{exc}")

        if error is not None:
            self._cancel_title()
            await self._log_usage("error", error.message)
            yield sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "message": error.message,
                        "type": error.error_type,
                        "param": None,
                        "code": None,
                    },
                },
            )
            return

        # An upstream turn that produced neither text nor thinking is an error, not
        # an empty answer: surface a readable error frame instead of a blank reply
        # (a public gateway was observed returning empty content occasionally).
        if not self.text_accum.strip() and self.reasoning_chars == 0:
            self._cancel_title()
            message = "上游没有返回任何内容，请重试（empty upstream response）"
            await self._log_usage("error", message)
            yield sse_event(
                "error",
                {
                    "type": "error",
                    "error": {
                        "message": message,
                        "type": "upstream_error",
                        "param": None,
                        "code": "empty_response",
                    },
                },
            )
            return

        for frame in self._stop_thinking():
            yield frame
        for frame in self._stop_text():
            yield frame

        title = await self._collect_title() if self.request.title_enabled() else None
        delta: dict[str, Any] = {
            "stop_reason": self.stop_reason,
            "stop_sequence": None,
        }
        if title:
            delta["title"] = title
        usage = dict(self.usage)
        if self.plan.web_search_requests or self.plan.web_fetch_requests:
            usage["server_tool_use"] = {
                "web_search_requests": self.plan.web_search_requests,
                "tool_search": self.plan.web_search_requests,
                "web_fetch_requests": self.plan.web_fetch_requests,
                "tool_fetch": self.plan.web_fetch_requests,
            }
        yield sse_event(
            "message_delta",
            {"type": "message_delta", "delta": delta, "usage": usage},
        )
        yield sse_event("message_stop", {"type": "message_stop"})
        await self._log_usage("ok", None, title=title)

    def _cancel_title(self) -> None:
        if self.title_task is not None and not self.title_task.done():
            self.title_task.cancel()

    async def _log_usage(self, status: str, detail: str | None, title: str | None = None) -> None:
        try:
            await self.db.execute(
                "INSERT INTO usage_log (user_id, created_at, endpoint, model, upstream_model,"
                " input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, status, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.user.id,
                    self.started_at,
                    "anthropic.messages",
                    self.request.model,
                    self.translation.upstream_model,
                    self.usage.get("input_tokens", 0),
                    self.usage.get("output_tokens", 0),
                    self.usage.get("cache_read_input_tokens", 0),
                    self.reasoning_tokens,
                    status,
                    detail,
                ),
            )
        except Exception:  # noqa: BLE001 - logging must never break a response
            logger.debug("usage log insert failed", exc_info=True)


async def relay_official(
    *,
    request: AnthropicMessageRequest,
    raw_body: dict[str, Any],
    upstream: AnthropicUpstream,
    settings: Settings,
    api_key: str,
    user_id: str | None = None,
    db: Database | None = None,
) -> AsyncIterator[bytes]:
    """SEARCH_MODE=official: forward the whole turn and relay its SSE stream."""
    payload = dict(raw_body)
    payload["stream"] = True
    payload.setdefault("model", request.model)
    status = "ok"
    detail: str | None = None
    try:
        async for event_name, data in upstream.iter_events(payload, api_key):
            yield sse_event(event_name, data)
    except UpstreamError as exc:
        status = "error"
        detail = exc.message
        yield sse_event(
            "error",
            {"type": "error", "error": {"message": exc.message, "type": exc.error_type}},
        )
    finally:
        if db is not None and user_id is not None:
            await log_usage(
                db,
                user_id=user_id,
                endpoint="anthropic.messages.relay",
                model=request.model,
                upstream_model=settings.search.official_model,
                status=status,
                detail=detail,
            )


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


def _notice_header(notices: list[str], plan: SearchPlan | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    values: list[str] = []
    if notices:
        values.extend(notices)
    if plan is not None and plan.notice:
        values.append(f"search:{plan.notice}")
    if values:
        headers["x-dgx-notice"] = ",".join(dict.fromkeys(values))
    headers["x-dgx-search"] = plan.notice if plan is not None else "off"
    return headers


OFFICIAL_KEY_REQUIRED_CODE = "official_key_required"
OFFICIAL_KEY_REQUIRED_MESSAGE = (
    "联网搜索需要 DeepSeek 官方 API Key。请在设置里填写「官方 API Key」"
    "（PUT /api/me/settings 的 official.api_key），或让管理员在服务器上配置 "
    "DEEPSEEK_API_KEY 后重试。"
)


def _official_key_required_response(
    *,
    user_id: str,
    db: Database,
    model: str,
    settings: Settings,
    mode: str,
) -> StreamingResponse:
    """SSE error frame telling the client to configure an official API key."""

    async def stream() -> AsyncIterator[bytes]:
        await log_usage(
            db,
            user_id=user_id,
            endpoint="anthropic.messages",
            model=model,
            status="error",
            detail=f"{OFFICIAL_KEY_REQUIRED_CODE}:{mode}",
        )
        yield sse_event(
            "error",
            {
                "type": "error",
                "error": {
                    "message": OFFICIAL_KEY_REQUIRED_MESSAGE,
                    "type": "authentication_error",
                    "param": None,
                    "code": OFFICIAL_KEY_REQUIRED_CODE,
                },
            },
        )

    return StreamingResponse(
        stream(),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Content-Type": SSE_MEDIA_TYPE,
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-dgx-search": "official_key_required",
            "x-dgx-notice": f"search:{OFFICIAL_KEY_REQUIRED_CODE}",
        },
    )


@router.post("/anthropic/v1/messages")
async def create_message(request: Request) -> StreamingResponse:
    state = request.app.state
    settings: Settings = state.settings
    db: Database = state.db
    store: FileStore = state.files
    upstream: UpstreamClient = state.upstream
    anthropic: AnthropicUpstream = state.anthropic
    search_service: SearchService = state.search_service

    user = await authenticate(request)
    reject_oversized_content_length(request, settings.max_json_body_bytes)
    raw = await request.body()
    if len(raw) > settings.max_json_body_bytes:
        raise ApiError(413, "请求体过大", "request_too_large")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"请求体不是合法 JSON：{exc.msg}", "invalid_request_error") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "请求体必须是 JSON 对象", "invalid_request_error")
    try:
        message_request = AnthropicMessageRequest.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise ApiError(
            400, f"请求参数错误：{location or 'body'} {first.get('msg', '')}", "invalid_request_error"
        ) from exc

    if not message_request.messages:
        raise ApiError(400, "messages 不能为空", "invalid_request_error", param="messages")

    # Abuse control: per-user request rate and (optional) daily token quota.
    limiters = getattr(state, "limiters", None)
    if limiters is not None and limiters.messages_by_user is not None:
        allowed, retry_after = limiters.messages_by_user.check(user.id)
        if not allowed:
            raise retry_after_error(
                "请求过于频繁，请稍后再试", int(retry_after) + 1
            )
        limiters.messages_by_user.hit(user.id)
    await check_token_quota(db, settings, user.id)

    user_settings = await _load_user_settings(db, user.id)
    search_override = user_settings.get("search") if isinstance(user_settings, dict) else None
    search_override = search_override if isinstance(search_override, dict) else None
    user_official_key = credentials.user_official_key(user_settings)
    effective_official_key = user_official_key or settings.search.official_api_key
    search_mode, _search_provider, _cfg = search_service._config(  # noqa: SLF001 - internal
        search_override, user_official_key
    )

    wants_search, wants_read_url = message_request.declares_search_tools()
    search_available = False
    if wants_search or wants_read_url:
        search_available, _reason = search_service.available(
            search_override, user_official_key
        )
        # Rule: web search (and the declared search tools) require an official
        # key.  Never silently answer a search turn without searching — unless the
        # operator turned the feature off entirely.
        if search_mode != "off" and not search_available:
            return _official_key_required_response(
                user_id=user.id,
                db=db,
                model=message_request.model,
                settings=settings,
                mode=search_mode,
            )

    if search_mode == "official" and wants_search and effective_official_key:
        headers = {
            "Content-Type": SSE_MEDIA_TYPE,
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-dgx-search": "official",
            "x-dgx-notice": "official_relay",
        }
        return StreamingResponse(
            relay_official(
                request=message_request,
                raw_body=body,
                upstream=anthropic,
                settings=settings,
                api_key=effective_official_key,
                user_id=user.id,
                db=db,
            ),
            media_type=SSE_MEDIA_TYPE,
            headers=headers,
        )

    # 1) translate ------------------------------------------------------
    translation = await build_translation(
        message_request, user_id=user.id, store=store, settings=settings
    )

    # 2) retrieval (SEARCH_MODE=dgx) ------------------------------------
    plan = SearchPlan(notice="off")
    if wants_search or (wants_read_url and translation.urls):
        if not search_available:
            # mode=off is a deliberate operator choice, not a missing credential.
            plan.notice = "off" if search_mode == "off" else "unavailable"
        else:
            try:
                plan = await search_service.plan(
                    query=translation.user_query,
                    urls=translation.urls,
                    want_search=wants_search,
                    want_read_url=wants_read_url,
                    user_settings=search_override,
                    official_api_key=user_official_key,
                )
            except Exception as exc:  # noqa: BLE001 - retrieval must never break chat
                logger.warning("search plan failed: %s", exc)
                plan = SearchPlan(notice="error")
    elif not translation.wants_search and not translation.wants_read_url:
        plan.notice = "off"

    if plan.context_text:
        translation.payload = append_user_context(translation.payload, plan.context_text)

    # 3) stream ---------------------------------------------------------
    streamer = MessageStreamer(
        settings=settings,
        user=user,
        request=message_request,
        translation=translation,
        plan=plan,
        upstream=upstream,
        db=db,
    )
    headers = _notice_header(translation.notices, plan)
    headers.update(
        {
            "Content-Type": SSE_MEDIA_TYPE,
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "x-ds-request-id": streamer.message_id,
            "x-dgx-request-id": streamer.message_id,
        }
    )
    return StreamingResponse(
        streamer.events(), media_type=SSE_MEDIA_TYPE, headers=headers
    )


async def _load_user_settings(db: Database, user_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT data FROM user_settings WHERE user_id = ?", (user_id,))
    if row is None:
        return {}
    try:
        parsed = json.loads(row["data"])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def stream_non_streaming_message(
    *, streamer: MessageStreamer
) -> dict[str, Any]:
    """Collect a stream into a single Anthropic message object (``stream:false``)."""
    blocks: list[dict[str, Any]] = []
    usage = zero_usage()
    stop_reason = "end_turn"
    async for frame in streamer.events():
        text = frame.decode("utf-8")
        for chunk in text.split("\n\n"):
            if not chunk.strip() or chunk.startswith(":"):
                continue
            event_name = None
            data_line = []
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    data_line.append(line[6:])
            if not data_line:
                continue
            try:
                payload = json.loads("\n".join(data_line))
            except json.JSONDecodeError:
                continue
            kind = payload.get("type")
            if event_name == "content_block_start":
                blocks.append(payload.get("content_block"))
            elif event_name == "content_block_delta":
                delta = payload.get("delta") or {}
                index = payload.get("index") or 0
                if delta.get("type") == "text_delta" and index < len(blocks):
                    blocks[index]["text"] = blocks[index].get("text", "") + delta["text"]
                elif delta.get("type") == "thinking_delta" and index < len(blocks):
                    blocks[index]["thinking"] = (
                        blocks[index].get("thinking", "") + delta["thinking"]
                    )
                elif delta.get("type") == "citations_delta" and index < len(blocks):
                    blocks[index].setdefault("citations", []).append(delta["citation"])
            elif event_name == "message_delta":
                usage = payload.get("usage") or usage
                stop_reason = (payload.get("delta") or {}).get("stop_reason") or stop_reason
            elif event_name == "error":
                raise ApiError(502, (payload.get("error") or {}).get("message", "上游错误"), "upstream_error")
    return {
        "id": streamer.message_id,
        "type": "message",
        "role": "assistant",
        "model": streamer.request.model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }
