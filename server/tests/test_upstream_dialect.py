"""Per-endpoint thinking dialects and ordered upstream fallback.

Uses httpx.MockTransport so the real request construction / retry logic runs
without any network.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from app.config import UpstreamEndpoint
from app.upstream import (
    THINKING_PARAM_KEYS,
    UpstreamClient,
    UpstreamError,
    apply_dialect,
    classify_upstream_error,
    thinking_params,
)

BASE = {"model": "deepseek-v4.1-flash-official", "messages": [{"role": "user", "content": "hi"}]}


def sse_body(text: str = "你好") -> str:
    chunk = {
        "choices": [
            {"index": 0, "delta": {"content": text}, "finish_reason": "stop"}
        ]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"


def make_client(settings) -> UpstreamClient:
    client = UpstreamClient(settings)
    return client


async def collect(client: UpstreamClient, payload: dict, thinking_enabled: bool) -> list[dict]:
    return [chunk async for chunk in client.iter_chat(payload, thinking_enabled=thinking_enabled)]


def handler_factory(records: list, *, reject_thinking: bool = False,
                    unreachable: set[str] | None = None):
    unreachable = unreachable or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        records.append({"url": url, "body": body, "headers": dict(request.headers)})
        host = request.url.host
        if host in unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        if reject_thinking and any(key in body for key in THINKING_PARAM_KEYS):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "1 validation error: extra fields not permitted: body.thinking"
                    }
                },
            )
        return httpx.Response(
            200, text=sse_body(), headers={"content-type": "text/event-stream"}
        )

    return handler


@pytest.mark.asyncio
async def test_unsupported_thinking_param_retried_without_it(settings, monkeypatch):
    settings = replace(
        settings, upstream_base_url="http://primary.test/v1", thinking_disable_mode="thinking"
    )
    records: list = []
    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler_factory(records, reject_thinking=True))
    )
    chunks = await collect(client, BASE, thinking_enabled=False)
    assert chunks and chunks[0]["choices"][0]["delta"]["content"] == "你好"
    assert len(records) == 2, records
    assert records[0]["body"]["thinking"] == {"type": "disabled"}
    assert not any(key in records[1]["body"] for key in THINKING_PARAM_KEYS)


@pytest.mark.asyncio
async def test_default_dialect_sends_reasoning_effort(settings, monkeypatch):
    settings = replace(settings, upstream_base_url="http://primary.test/v1")
    records: list = []
    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler_factory(records)))
    await collect(client, BASE, thinking_enabled=False)
    assert len(records) == 1
    assert records[0]["body"]["reasoning_effort"] == "none"
    assert "thinking" not in records[0]["body"]


@pytest.mark.asyncio
async def test_thinking_enabled_sends_enable_hint(settings):
    settings = replace(settings, upstream_base_url="http://primary.test/v1")
    records: list = []
    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler_factory(records)))
    await collect(client, BASE, thinking_enabled=True)
    assert len(records) == 1
    assert records[0]["body"]["enable_thinking"] is True
    assert "thinking" not in records[0]["body"]


@pytest.mark.asyncio
async def test_fallback_endpoint_used_when_primary_unreachable(settings):
    settings = replace(
        settings,
        upstream_base_url="http://primary.test/v1",
        upstream_api_key="",
        upstream_fallbacks=[
            UpstreamEndpoint(
                name="public",
                base_url="http://gateway.test/v1",
                api_key="sk-test",
                thinking_mode="reasoning_effort",
            )
        ],
    )
    records: list = []
    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler_factory(records, unreachable={"primary.test"})
        )
    )
    chunks = await collect(client, BASE, thinking_enabled=False)
    assert chunks
    assert records[0]["url"].startswith("http://primary.test/v1/chat/completions")
    assert records[-1]["url"].startswith("http://gateway.test/v1/chat/completions")
    assert records[-1]["headers"]["authorization"] == "Bearer sk-test"
    assert records[-1]["body"]["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_fallback_on_retryable_http_error(settings):
    settings = replace(
        settings,
        upstream_base_url="http://primary.test/v1",
        upstream_fallbacks=[
            UpstreamEndpoint(name="public", base_url="http://gateway.test/v1", api_key="k")
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.test":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, text=sse_body("fallback answer"))

    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    chunks = await collect(client, BASE, thinking_enabled=False)
    assert chunks[0]["choices"][0]["delta"]["content"] == "fallback answer"


@pytest.mark.asyncio
async def test_all_endpoints_failing_raises(settings):
    settings = replace(
        settings,
        upstream_base_url="http://primary.test/v1",
        upstream_fallbacks=[
            UpstreamEndpoint(name="public", base_url="http://gateway.test/v1", api_key="k")
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UpstreamError) as excinfo:
        await collect(client, BASE, thinking_enabled=False)
    assert "503" in excinfo.value.message or "overloaded" in excinfo.value.message


@pytest.mark.asyncio
async def test_complete_chat_strips_thinking_and_retries(settings):
    settings = replace(
        settings, upstream_base_url="http://primary.test/v1", thinking_disable_mode="thinking"
    )
    records: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        records.append(body)
        if "thinking" in body:
            return httpx.Response(400, json={"error": {"message": "unknown parameter: thinking"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "标题"}}]})

    client = UpstreamClient(settings)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = await client.complete_chat(dict(BASE, stream=True), thinking_enabled=False)
    assert data["choices"][0]["message"]["content"] == "标题"
    assert len(records) == 2
    for body in records:
        assert "stream" not in body
        assert "stream_options" not in body


def test_classify_upstream_error():
    unsupported = classify_upstream_error(
        400, '{"error":{"message":"1 validation error: extra fields not permitted"}}'
    )
    assert unsupported.unsupported_param is True
    assert unsupported.retryable is False

    plain_400 = classify_upstream_error(400, '{"error":{"message":"bad request"}}')
    assert plain_400.unsupported_param is True  # retried once with params stripped

    server_error = classify_upstream_error(503, "overloaded")
    assert server_error.retryable is True

    auth_error = classify_upstream_error(401, '{"error":{"message":"invalid key"}}')
    assert auth_error.retryable is True

    not_found = classify_upstream_error(404, '{"error":{"message":"model not found"}}')
    assert not_found.retryable is False
    assert not_found.unsupported_param is False


def test_apply_dialect_never_mutates_input():
    payload = {"model": "m", "thinking": {"type": "disabled"}, "messages": []}
    applied = apply_dialect(payload, enabled=False, mode="reasoning_effort")
    assert payload["thinking"] == {"type": "disabled"}  # untouched
    assert "thinking" not in applied
    assert applied["reasoning_effort"] == "none"


def test_thinking_params_unknown_mode_is_noop():
    assert thinking_params(False, "unknown-mode") == {}
