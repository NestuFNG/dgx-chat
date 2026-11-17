"""Speech tools: ``/v1/tools/asr`` and ``/v1/tools/tts`` (SPEC §2.2).

TTS must answer with raw ``s16le`` 24 kHz mono PCM.  With ``provider=deepseek``
the official endpoint already returns exactly that (measured, FACTS_UPSTREAM.md
§B3) so the bytes are passed through untouched.  Any other provider/format is
transcoded with ffmpeg, and if ffmpeg is missing the client gets a clear error
instead of corrupt audio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from .auth import authenticate
from .config import Settings, SpeechProviderConfig
from .db import Database
from .models import ApiError, TtsRequest
from .security import (
    add_counter,
    check_daily_quota,
    log_usage,
    reject_oversized_content_length,
)

logger = logging.getLogger("dgxchat.speech")

router = APIRouter(tags=["speech"])

PCM_MEDIA_TYPE = "audio/pcm; rate=24000; encoding=signed-integer; bits=16; channels=1"
PCM_CONTENT_TYPE = "audio/pcm"


# ---------------------------------------------------------------------------
# markdown filtering (TTS should never read code fences or tables aloud)
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_LATEX_BLOCK_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]{3,}\$|\\\[.*?\\\]|\\\(.*?\\\)", re.DOTALL)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_HR_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_FOOTNOTE_RE = re.compile(r"\[\^[^\]]*\]")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t\u00a0]+")


def filter_markdown_for_speech(text: str) -> str:
    """Strip code fences/tables/quotes/markdown/LaTeX before synthesis."""
    if not text:
        return ""
    cleaned = _FENCE_RE.sub("\n", text)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _IMAGE_RE.sub("", cleaned)
    cleaned = _LINK_RE.sub(r"\1", cleaned)
    cleaned = _LATEX_BLOCK_RE.sub(" ", cleaned)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _FOOTNOTE_RE.sub("", cleaned)
    cleaned = _HR_RE.sub("", cleaned)

    lines: list[str] = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.count("|") >= 2:  # table row
            continue
        if _TABLE_SEP_RE.match(stripped):
            continue
        if re.match(r"^[-*=]{2,}$", stripped):
            continue
        if stripped.startswith(">"):
            stripped = stripped.lstrip("> ").strip()
            if not stripped:
                lines.append("")
                continue
        stripped = _HEADING_RE.sub("", stripped)
        stripped = _LIST_MARK_RE.sub("", stripped)
        stripped = _EMPHASIS_RE.sub("", stripped)
        lines.append(stripped)
    cleaned = "\n".join(lines)
    cleaned = _MULTI_NEWLINE_RE.sub("\n\n", cleaned)
    cleaned = _MULTI_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def pcm_duration_bytes(payload: bytes) -> int:
    return len(payload)


# ---------------------------------------------------------------------------
# audio format detection / transcoding
# ---------------------------------------------------------------------------


def _parse_pcm_params(content_type: str) -> tuple[int | None, int | None]:
    rate = None
    channels = None
    for part in (content_type or "").split(";")[1:]:
        key, _, value = part.strip().partition("=")
        key = key.strip().lower()
        value = value.strip()
        try:
            if key == "rate":
                rate = int(value)
            elif key == "channels":
                channels = int(value)
        except ValueError:
            continue
    return rate, channels


def sniff_audio(payload: bytes, content_type: str) -> tuple[str, int | None, int | None]:
    """Return ``(kind, rate, channels)``; kind is pcm|wav|mp3|ogg|unknown."""
    mime = (content_type or "").split(";")[0].strip().lower()
    head = payload[:16]
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav", None, None
    if head.startswith(b"OggS"):
        return "ogg", None, None
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf9"):
        return "mp3", None, None
    if mime in ("audio/wav", "audio/x-wav", "audio/wave"):
        return "wav", None, None
    if mime in ("audio/mpeg", "audio/mp3"):
        return "mp3", None, None
    if mime in ("audio/ogg", "audio/opus", "application/ogg"):
        return "ogg", None, None
    if mime in (PCM_CONTENT_TYPE, "audio/l16", "audio/raw"):
        rate, channels = _parse_pcm_params(content_type)
        return "pcm", rate, channels
    if mime.startswith("audio/"):
        # Some upstreams report audio/* for container formats; look at the magic.
        return "unknown", None, None
    return "unknown", None, None


async def transcode_to_pcm(
    settings: Settings,
    payload: bytes,
    *,
    kind: str,
    rate: int | None,
    channels: int | None,
) -> bytes:
    ffmpeg = settings.speech.ffmpeg_bin
    if not ffmpeg:
        raise ApiError(
            502,
            "上游返回的音频不是 24kHz 单声道 s16le PCM，需要 ffmpeg 转码，"
            "但服务器未安装 ffmpeg（请安装或在 Dockerfile 中启用 ffmpeg）",
            "upstream_error",
        )
    args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if kind in ("pcm", "unknown"):
        args += ["-f", "s16le", "-ar", str(rate or 24000), "-ac", str(channels or 1)]
    args += [
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(settings.speech.output_sample_rate),
        "-ac",
        str(settings.speech.output_channels),
        "pipe:1",
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ApiError(
            502, f"无法执行 ffmpeg（{ffmpeg}）：音频转码不可用", "upstream_error"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload), timeout=settings.speech.transcode_timeout
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        raise ApiError(504, "音频转码超时", "timeout_error") from exc
    if process.returncode != 0 or not stdout:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[:400]
        raise ApiError(502, f"音频转码失败：{detail or 'ffmpeg 返回非零状态'}", "upstream_error")
    return stdout


async def ensure_pcm(
    settings: Settings, payload: bytes, content_type: str
) -> bytes:
    kind, rate, channels = sniff_audio(payload, content_type)
    target_rate = settings.speech.output_sample_rate
    target_channels = settings.speech.output_channels
    if kind == "pcm":
        if (rate or target_rate) == target_rate and (channels or target_channels) == target_channels:
            return payload
        return await transcode_to_pcm(
            settings, payload, kind="pcm", rate=rate, channels=channels
        )
    return await transcode_to_pcm(settings, payload, kind=kind, rate=rate, channels=channels)


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def tools_path(base: str, name: str) -> str:
    base = (base or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/tools/{name}"
    return f"{base}/v1/tools/{name}"


def openai_path(base: str, name: str) -> str:
    base = (base or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/audio/{name}"
    return f"{base}/v1/audio/{name}"


def resolve_provider(
    settings: Settings, override: dict[str, Any] | None, kind: str
) -> SpeechProviderConfig:
    base = settings.speech.tts if kind == "tts" else settings.speech.asr
    if isinstance(override, dict):
        return base.merged(override)
    return base


async def load_user_settings(db: Database, user_id: str) -> dict[str, Any]:
    row = await db.fetch_one("SELECT data FROM user_settings WHERE user_id = ?", (user_id,))
    if row is None:
        return {}
    try:
        parsed = json.loads(row["data"])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_error(status: int, body: str) -> ApiError:
    message = body.strip()[:800]
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            elif isinstance(parsed.get("message"), str):
                message = parsed["message"]
    except json.JSONDecodeError:
        pass
    return ApiError(502, f"语音上游返回 HTTP {status}: {message or '（无错误信息）'}", "upstream_error")


def _auth_headers(provider: SpeechProviderConfig) -> dict[str, str]:
    headers: dict[str, str] = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
        headers["x-api-key"] = provider.api_key
    return headers


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.post("/v1/tools/asr")
async def transcribe(request: Request) -> JSONResponse:
    user = await authenticate(request)
    state = request.app.state
    settings: Settings = state.settings
    db: Database = state.db

    reject_oversized_content_length(request, settings.speech.max_asr_bytes + 1024 * 1024)
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile):
        await form.close()
        raise ApiError(400, "缺少 multipart 字段 file", "invalid_request_error", param="file")
    data = await upload.read()
    filename = upload.filename or "recording.wav"
    content_type = upload.content_type or "audio/wav"
    await form.close()
    if len(data) > settings.speech.max_asr_bytes:
        raise ApiError(413, "音频过大（上限 8 MiB）", "request_too_large")
    if not data:
        raise ApiError(400, "音频内容为空", "invalid_request_error")

    user_settings = await load_user_settings(db, user.id)
    override = user_settings.get("asr") if isinstance(user_settings, dict) else None
    provider = resolve_provider(settings, override if isinstance(override, dict) else None, "asr")
    if provider.provider == "disabled":
        raise ApiError(
            503,
            "ASR 未配置：请设置 ASR_PROVIDER（deepseek / openai / deepseek_tools）或 DEEPSEEK_API_KEY",
            "service_unavailable",
        )

    timeout = httpx.Timeout(connect=10.0, read=provider.timeout, write=120.0, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider.provider in ("deepseek", "deepseek_tools"):
                url = tools_path(provider.base_url, "asr")
                response = await client.post(
                    url,
                    headers=_auth_headers(provider),
                    files={"file": (filename, data, content_type)},
                )
            elif provider.provider == "openai":
                url = openai_path(provider.base_url, "transcriptions")
                form_data = {"model": provider.model or "whisper-1", "response_format": "json"}
                response = await client.post(
                    url,
                    headers=_auth_headers(provider),
                    data=form_data,
                    files={"file": (filename, data, content_type)},
                )
            else:  # pragma: no cover - config validated upstream
                raise ApiError(503, f"未知的 ASR provider：{provider.provider}", "service_unavailable")
    except httpx.HTTPError as exc:
        raise ApiError(502, f"无法连接语音上游：{exc}", "upstream_error") from exc

    if response.status_code >= 400:
        raise _provider_error(response.status_code, response.text)
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ApiError(502, "ASR 上游返回了非 JSON 响应", "upstream_error") from exc
    if not isinstance(body, dict):
        raise ApiError(502, "ASR 上游返回了意外的响应结构", "upstream_error")
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(502, "ASR 上游响应缺少 text 字段", "upstream_error")
    result: dict[str, Any] = {"text": text}
    if isinstance(body.get("request_id"), str):
        result["request_id"] = body["request_id"]
    await add_counter(db, user.id, "asr_calls", 1)
    await log_usage(
        db,
        user_id=user.id,
        endpoint="tools.asr",
        model=provider.model or provider.provider,
        status="ok",
        detail=f"bytes={len(data)}",
    )
    return JSONResponse(result)


@router.post("/v1/tools/tts")
async def synthesize(request: Request) -> Response:
    user = await authenticate(request)
    state = request.app.state
    settings: Settings = state.settings
    db: Database = state.db

    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ApiError(400, f"请求体不是合法 JSON：{exc.msg}", "invalid_request_error") from exc
    try:
        tts_request = TtsRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - pydantic message is good enough
        raise ApiError(400, f"TTS 请求参数错误：{exc}", "invalid_request_error") from exc

    text = tts_request.text
    if tts_request.filter_markdown:
        text = filter_markdown_for_speech(text)
    if not text.strip():
        raise ApiError(400, "过滤 markdown 后没有可朗读的文本", "invalid_request_error", param="text")
    if len(text) > settings.speech.max_tts_chars:
        text = text[: settings.speech.max_tts_chars]

    # Per-user daily TTS character quota: this is the feature that costs money.
    await check_daily_quota(
        db, user.id, "tts_chars", settings.daily_tts_chars, "语音合成字符"
    )

    user_settings = await load_user_settings(db, user.id)
    override = user_settings.get("tts") if isinstance(user_settings, dict) else None
    provider = resolve_provider(settings, override if isinstance(override, dict) else None, "tts")
    if provider.provider == "disabled":
        raise ApiError(
            503,
            "TTS 未配置：请设置 TTS_PROVIDER（deepseek / openai / deepseek_tools）或 DEEPSEEK_API_KEY",
            "service_unavailable",
        )

    voice = tts_request.voice_id or "stella"
    resolved_voice = provider.voice_map.get(voice) or provider.voice_map.get("*") or voice
    if provider.provider == "openai" and not resolved_voice:
        resolved_voice = "alloy"

    timeout = httpx.Timeout(connect=10.0, read=provider.timeout, write=120.0, pool=None)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider.provider in ("deepseek", "deepseek_tools"):
                url = tools_path(provider.base_url, "tts")
                body: dict[str, Any] = {
                    "text": text,
                    "voice_id": resolved_voice,
                    "format": "pcm",
                    "filter_markdown": False,  # we already filtered
                }
                if provider.model:
                    body["model"] = provider.model
                response = await client.post(url, headers=_auth_headers(provider), json=body)
            elif provider.provider == "openai":
                url = openai_path(provider.base_url, "speech")
                body = {
                    "model": provider.model or "gpt-4o-mini-tts",
                    "input": text,
                    "voice": resolved_voice,
                    "response_format": "pcm",
                }
                response = await client.post(url, headers=_auth_headers(provider), json=body)
            else:  # pragma: no cover
                raise ApiError(503, f"未知的 TTS provider：{provider.provider}", "service_unavailable")
    except httpx.HTTPError as exc:
        raise ApiError(502, f"无法连接语音上游：{exc}", "upstream_error") from exc

    if response.status_code >= 400:
        raise _provider_error(response.status_code, response.text)
    audio = response.content
    if not audio:
        raise ApiError(502, "TTS 上游返回了空音频", "upstream_error")
    content_type = response.headers.get("content-type", "")
    pcm = await ensure_pcm(settings, audio, content_type)
    await add_counter(db, user.id, "tts_chars", len(text))
    await log_usage(
        db,
        user_id=user.id,
        endpoint="tools.tts",
        model=provider.model or provider.provider,
        status="ok",
        detail=f"chars={len(text)}",
    )
    return Response(
        content=pcm,
        media_type=PCM_CONTENT_TYPE,
        headers={
            "Content-Type": PCM_MEDIA_TYPE,
            "Content-Length": str(len(pcm)),
            "Cache-Control": "no-store",
            "x-dgx-audio-bytes": str(len(pcm)),
            "x-dgx-tts-provider": provider.provider,
            "x-dgx-tts-voice": resolved_voice,
        },
    )
