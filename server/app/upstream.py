"""Upstream HTTP clients: DGX/OpenAI-compatible gateways and the DeepSeek
official Anthropic-compatible endpoint.

Two gateways are in play and they do NOT share a thinking-parameter dialect:

* LAN gateway (no auth): accepts ``thinking:{"type":"disabled"}`` and
  ``reasoning_effort:"none"``.
* Public gateway: rejects ``thinking:{...}`` with HTTP 400, but accepts
  ``reasoning_effort:"none"``, ``enable_thinking:false`` and
  ``chat_template_kwargs:{"thinking":false}``.

``reasoning_effort:"none"`` is therefore the default; each endpoint can override
the dialect, and if an endpoint rejects a request as an invalid/unknown
parameter the client retries the same request once with all thinking parameters
stripped, then moves on to the next configured endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .config import Settings, UpstreamEndpoint

logger = logging.getLogger("dgxchat.upstream")
upstream_logger = logging.getLogger("dgxchat.upstream.wire")

#: Every request field that influences the thinking behaviour of the gateway.
THINKING_PARAM_KEYS = (
    "thinking",
    "reasoning_effort",
    "enable_thinking",
    "chat_template_kwargs",
)

_PARAM_REJECTION_HINTS = (
    "unknown",
    "extra",
    "unexpected",
    "not permitted",
    "not support",
    "unsupported",
    "unrecognized",
    "invalid",
    "validation",
    "extra_forbidden",
    "field required",
)


class UpstreamError(Exception):
    """A failure talking to an upstream model service."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        error_type: str = "upstream_error",
        unsupported_param: bool = False,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.unsupported_param = unsupported_param
        self.retryable = retryable


def classify_upstream_error(status: int, body: str) -> UpstreamError:
    message = _describe_http_error(status, body)
    lowered = f"{message} {body[:2000]}".lower()
    hinted = any(hint in lowered for hint in _PARAM_REJECTION_HINTS)
    unsupported = status in (400, 422) and hinted
    retryable = status in (401, 403, 408, 409, 425, 429) or status >= 500
    if status in (400, 422) and not hinted:
        # A plain 400 could still be a parameter-shape rejection; strip and retry
        # once rather than failing the turn outright.
        unsupported = True
    return UpstreamError(
        message,
        status_code=status,
        unsupported_param=unsupported,
        retryable=retryable,
    )


def _describe_http_error(status: int, body: str) -> str:
    message = body.strip()
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            if isinstance(error.get("message"), str):
                message = error["message"]
            detail = error.get("detail")
            if message == body.strip() and isinstance(detail, str):
                message = detail
        elif isinstance(parsed.get("message"), str):
            message = parsed["message"]
    if len(message) > 2000:
        message = message[:2000] + "…"
    return f"上游返回 HTTP {status}: {message or '（无错误信息）'}"


# ---------------------------------------------------------------------------
# thinking dialect
# ---------------------------------------------------------------------------


def thinking_params(enabled: bool, mode: str) -> dict[str, Any]:
    """Request fields implementing the requested thinking state for a dialect.

    * disabled: only ``reasoning_effort:"none"`` is accepted by BOTH gateways
      (the public litellm proxy rejects ``thinking:{...}`` with HTTP 400).
    * enabled: ``enable_thinking:true`` is accepted by both (the LAN gateway
      really enables reasoning; the public proxy accepts and ignores it — it
      never surfaces a reasoning channel at all).
    """
    mode = (mode or "reasoning_effort").strip().lower()
    if enabled:
        if mode == "none":
            return {}
        return {"enable_thinking": True}
    if mode == "reasoning_effort":
        return {"reasoning_effort": "none"}
    if mode == "thinking":
        return {"thinking": {"type": "disabled"}}
    if mode == "both":
        return {"reasoning_effort": "none", "thinking": {"type": "disabled"}}
    if mode == "enable_thinking":
        return {"enable_thinking": False}
    if mode == "chat_template_kwargs":
        return {"chat_template_kwargs": {"thinking": False}}
    return {}  # "none": never send anything


def apply_dialect(
    payload: dict[str, Any],
    *,
    enabled: bool,
    mode: str,
    strip: bool = False,
) -> dict[str, Any]:
    """Return a copy of ``payload`` with this endpoint's thinking dialect."""
    body = {key: value for key, value in payload.items() if key not in THINKING_PARAM_KEYS}
    if not strip:
        body.update(thinking_params(enabled, mode))
    return body


def _log_body(label: str, payload: dict[str, Any], url: str) -> None:
    """DGX_LOG_UPSTREAM=1: dump the exact outgoing body for protocol diffing."""
    if not upstream_logger.isEnabledFor(logging.INFO):
        return
    try:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        rendered = repr(payload)
    upstream_logger.info("UPSTREAM %s url=%s body=\n%s", label, url, rendered)
    messages = payload.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], dict):
        if messages[0].get("role") == "system":
            upstream_logger.info(
                "UPSTREAM_SYSTEM_JSON=%s",
                json.dumps(messages[0].get("content"), ensure_ascii=False),
            )


class UpstreamClient:
    """Shared httpx client with per-endpoint dialects and ordered fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        timeout = httpx.Timeout(
            connect=self.settings.upstream_connect_timeout,
            read=self.settings.upstream_timeout,
            write=120.0,
            pool=None,
        )
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
        self._client = httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("upstream client is not started")
        return self._client

    def endpoints(self) -> list[UpstreamEndpoint]:
        return self.settings.upstream_endpoints()

    def log_payload(self, label: str, payload: dict[str, Any], url: str | None = None) -> None:
        if self.settings.log_upstream:
            _log_body(label, payload, url or self.settings.upstream_chat_url)

    @staticmethod
    def _headers(endpoint: UpstreamEndpoint) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        return headers

    # -- low level ---------------------------------------------------------
    async def iter_chat_chunks(
        self, payload: dict[str, Any], endpoint: UpstreamEndpoint | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded streaming chunks from one endpoint."""
        target = endpoint or self.endpoints()[0]
        url = target.chat_url
        self.log_payload("chat.completions", payload, url)
        try:
            response = await self.client.send(
                self.client.build_request(
                    "POST", url, json=payload, headers=self._headers(target)
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"无法连接上游 {target.name}（{url}）：{exc}", retryable=True
            ) from exc
        try:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                raise classify_upstream_error(response.status_code, body)
            async for line in response.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug("skipping non-JSON upstream frame: %.200s", data)
                    continue
                if isinstance(chunk, dict):
                    yield chunk
        finally:
            await response.aclose()

    # -- dialect + fallback aware streaming --------------------------------
    async def iter_chat(
        self, payload: dict[str, Any], *, thinking_enabled: bool
    ) -> AsyncIterator[dict[str, Any]]:
        endpoints = self.endpoints()
        last_error: UpstreamError | None = None
        for endpoint in endpoints:
            mode = endpoint.thinking_mode
            for strip in (False, True):
                body = apply_dialect(
                    payload, enabled=thinking_enabled, mode=mode, strip=strip
                )
                if strip and not thinking_params(thinking_enabled, mode):
                    break  # this dialect sent nothing, nothing to strip
                emitted = False
                try:
                    logger.info(
                        "upstream request -> %s (dialect=%s thinking=%s strip=%s)",
                        endpoint.name,
                        mode,
                        "on" if thinking_enabled else "off",
                        strip,
                    )
                    async for chunk in self.iter_chat_chunks(body, endpoint=endpoint):
                        emitted = True
                        yield chunk
                    return
                except UpstreamError as exc:
                    if emitted:
                        raise
                    last_error = exc
                    if not strip and exc.unsupported_param:
                        logger.warning(
                            "endpoint %s rejected thinking parameters (%s); "
                            "retrying without them",
                            endpoint.name,
                            exc.message[:200],
                        )
                        continue
                    if exc.retryable or endpoint is not endpoints[-1]:
                        logger.warning(
                            "endpoint %s failed (%s); trying next endpoint",
                            endpoint.name,
                            exc.message[:200],
                        )
                        break
                    raise
                except httpx.HTTPError as exc:
                    last_error = UpstreamError(
                        f"上游 {endpoint.name} 传输错误：{exc}", retryable=True
                    )
                    if emitted:
                        raise last_error from exc
                    break
        raise last_error or UpstreamError("没有可用的上游端点")

    # -- non streaming -----------------------------------------------------
    async def complete_chat(
        self, payload: dict[str, Any], *, thinking_enabled: bool = False
    ) -> dict[str, Any]:
        """Non-streaming completion (never carries stream_options)."""
        base = dict(payload)
        base.pop("stream", None)
        base.pop("stream_options", None)
        endpoints = self.endpoints()
        last_error: UpstreamError | None = None
        for endpoint in endpoints:
            mode = endpoint.thinking_mode
            for strip in (False, True):
                body = apply_dialect(
                    base, enabled=thinking_enabled, mode=mode, strip=strip
                )
                if strip and not thinking_params(thinking_enabled, mode):
                    break
                url = endpoint.chat_url
                self.log_payload("chat.completions(non-stream)", body, url)
                try:
                    response = await self.client.post(
                        url, json=body, headers=self._headers(endpoint)
                    )
                except httpx.HTTPError as exc:
                    last_error = UpstreamError(
                        f"无法连接上游 {endpoint.name}（{url}）：{exc}", retryable=True
                    )
                    break
                if response.status_code != 200:
                    error = classify_upstream_error(response.status_code, response.text)
                    last_error = error
                    if not strip and error.unsupported_param:
                        logger.warning(
                            "endpoint %s rejected thinking parameters; retrying without them",
                            endpoint.name,
                        )
                        continue
                    break
                try:
                    data = response.json()
                except json.JSONDecodeError as exc:
                    last_error = UpstreamError("上游返回了无法解析的 JSON")
                    break
                if not isinstance(data, dict):
                    last_error = UpstreamError("上游返回了意外的响应结构")
                    break
                return data
        raise last_error or UpstreamError("没有可用的上游端点")


class AnthropicUpstream:
    """DeepSeek official Anthropic-compatible Messages endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=None)
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("anthropic upstream client is not started")
        return self._client

    def messages_url(self) -> str:
        return f"{self.settings.search.official_base_url.rstrip('/')}/messages"

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

    async def iter_events(
        self, payload: dict[str, Any], api_key: str
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yield ``(event_name, parsed_data)`` pairs from the official endpoint."""
        url = self.messages_url()
        if self.settings.log_upstream:
            _log_body("anthropic.messages", payload, url)
        response = await self.client.send(
            self.client.build_request(
                "POST", url, json=payload, headers=self._headers(api_key)
            ),
            stream=True,
        )
        try:
            if response.status_code != 200:
                body = (await response.aread()).decode("utf-8", "replace")
                raise UpstreamError(
                    _describe_http_error(response.status_code, body),
                    status_code=502,
                )
            event_name = "message"
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip() or "message"
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                kind = parsed.get("type")
                if isinstance(kind, str):
                    event_name = kind
                yield event_name, parsed
        finally:
            await response.aclose()

    async def complete(
        self, payload: dict[str, Any], api_key: str
    ) -> tuple[int, str]:
        """Non-streaming POST used for retrieval-only requests."""
        url = self.messages_url()
        response = await self.client.post(
            url, json=payload, headers=self._headers(api_key)
        )
        return response.status_code, response.text
