"""SSE event sequencing and framing tests (SPEC §3 event contract)."""

from __future__ import annotations

import json

import pytest

from app.anthropic_compat import sse_event, usage_from_openai, map_stop_reason

from tests.conftest import (
    OFFICIAL_SYSTEM,
    SEARCH_TOOLS,
    blocks_of,
    event_types,
    message_body,
    text_script,
)


def test_sse_frame_format():
    frame = sse_event("message_stop", {"type": "message_stop"}).decode("utf-8")
    assert frame == 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    assert frame.endswith("\n\n")
    assert "event: message_stop" in frame.splitlines()[0]


def test_usage_mapping():
    usage = usage_from_openai(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40, "created_cache_tokens": 5},
        }
    )
    assert usage == {
        "input_tokens": 60,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 40,
        "output_tokens": 20,
    }
    assert usage_from_openai(None)["input_tokens"] == 0


def test_stop_reason_mapping():
    assert map_stop_reason("stop") == "end_turn"
    assert map_stop_reason("length") == "max_tokens"
    assert map_stop_reason("tool_calls") == "tool_use"
    assert map_stop_reason("content_filter") == "refusal"
    assert map_stop_reason(None) == "end_turn"


def test_basic_sequence_without_thinking(admin_harness):
    admin_harness.fake_upstream(text_script("快速排序是分治算法。"))
    status, events, headers = admin_harness.stream_message(
        admin_harness.admin_token, message_body(thinking={"type": "disabled"})
    )
    assert status == 200
    names = event_types(events)
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    assert "error" not in names
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    blocks = blocks_of(events)
    assert [b["type"] for b in blocks] == ["text"]
    deltas = [d for n, d in events if n == "content_block_delta"]
    assert deltas[0]["delta"] == {"type": "text_delta", "text": "快速排序是分治算法。"}

    start = events[0][1]
    assert start["message"]["model"] == "deepseek-flash"
    assert start["message"]["id"].count("-") == 4  # plain UUID, no prefix
    assert start["message"]["usage"] == {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    final = [d for n, d in events if n == "message_delta"][0]
    assert final["delta"]["stop_reason"] == "end_turn"
    assert final["delta"]["stop_sequence"] is None
    assert final["usage"]["output_tokens"] == 12
    assert final["usage"]["cache_read_input_tokens"] == 4
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache, no-transform"
    assert headers["x-accel-buffering"] == "no"


def test_thinking_enabled_block_ordering(admin_harness):
    admin_harness.fake_upstream(
        text_script("答案正文", reasoning="先想一想")
    )
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body(thinking={"type": "enabled"})
    )
    assert status == 200
    blocks = blocks_of(events)
    assert [b["type"] for b in blocks] == ["thinking", "text"]
    starts = [(d["index"], d["content_block"]["type"]) for n, d in events if n == "content_block_start"]
    assert starts == [(0, "thinking"), (1, "text")]
    thinking_deltas = [
        d for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "thinking_delta"
    ]
    assert thinking_deltas[0]["delta"]["thinking"] == "先想一想"
    assert thinking_deltas[0]["index"] == 0


def test_thinking_disabled_drops_upstream_reasoning(admin_harness):
    """A misbehaving upstream that still reasons must not leak a thinking block."""
    admin_harness.fake_upstream(text_script("正文", reasoning="不该出现的思考"))
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body(thinking={"type": "disabled"})
    )
    assert status == 200
    assert [b["type"] for b in blocks_of(events)] == ["text"]
    assert not any(
        d["delta"]["type"] == "thinking_delta"
        for n, d in events
        if n == "content_block_delta"
    )


def test_empty_upstream_becomes_an_error(admin_harness):
    """An upstream turn with no text and no thinking is an error, not an answer."""
    admin_harness.fake_upstream([])
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body()
    )
    assert status == 200
    names = event_types(events)
    assert names[-1] == "error"
    payload = events[-1][1]
    assert payload["error"]["message"]
    assert "没有返回任何内容" in payload["error"]["message"]
    assert blocks_of(events) == []


def test_upstream_error_becomes_error_event(admin_harness):
    admin_harness.fake_upstream_error("上游炸了")
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body()
    )
    assert status == 200
    assert event_types(events) == ["message_start", "error"]
    payload = events[-1][1]
    assert payload["error"]["message"] == "上游炸了"
    assert "type" in payload["error"]


def test_upstream_error_after_content_keeps_events(admin_harness):
    async def failing_after_one(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
        yield {"choices": [{"index": 0, "delta": {"content": "部分"}, "finish_reason": None}]}
        from app.upstream import UpstreamError

        raise UpstreamError("流中断了")

    admin_harness.app.state.upstream.iter_chat = failing_after_one  # type: ignore[assignment]
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body()
    )
    names = event_types(events)
    assert names[-1] == "error"
    assert "content_block_delta" in names


def test_usage_omitted_upstream_reports_zeros(admin_harness):
    admin_harness.fake_upstream(
        [{"choices": [{"index": 0, "delta": {"content": "字"}, "finish_reason": "stop"}]}]
    )
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body()
    )
    final = [d for n, d in events if n == "message_delta"][0]
    assert final["usage"] == {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }


def test_length_finish_maps_to_max_tokens(admin_harness):
    admin_harness.fake_upstream(text_script("被截断", finish_reason="length"))
    _, events, _ = admin_harness.stream_message(admin_harness.admin_token, message_body())
    final = [d for n, d in events if n == "message_delta"][0]
    assert final["delta"]["stop_reason"] == "max_tokens"


def test_title_emitted_in_message_delta(admin_harness):
    admin_harness.fake_upstream(
        text_script("快速排序是一种分治算法。" * 20)
    )
    admin_harness.fake_title("快速排序入门")
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body(title={"enabled": True})
    )
    assert status == 200
    final = [d for n, d in events if n == "message_delta"][0]
    assert final["delta"]["title"] == "快速排序入门"
    assert admin_harness.title_calls, "title request was not issued"
    title_call = admin_harness.title_calls[0]
    assert "stream_options" not in title_call["payload"]
    assert "stream" not in title_call["payload"]
    assert title_call["thinking_enabled"] is False


def test_no_title_when_not_requested(admin_harness):
    admin_harness.fake_upstream(text_script("正文"))
    admin_harness.fake_title("不该出现")
    _, events, _ = admin_harness.stream_message(admin_harness.admin_token, message_body())
    final = [d for n, d in events if n == "message_delta"][0]
    assert "title" not in final["delta"]


def test_title_failure_does_not_break_stream(admin_harness):
    admin_harness.fake_upstream(text_script("正文"))

    async def failing_complete(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
        from app.upstream import UpstreamError

        raise UpstreamError("标题接口挂了")

    admin_harness.app.state.upstream.complete_chat = failing_complete  # type: ignore[assignment]
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, message_body(title={"enabled": True})
    )
    assert status == 200
    assert event_types(events)[-1] == "message_stop"
    final = [d for n, d in events if n == "message_delta"][0]
    assert "title" not in final["delta"]


def test_prefill_rejected_falls_back(admin_harness):
    """The first attempt fails (continue_final_message unsupported), retry succeeds."""
    attempts: list[dict] = []

    async def fake(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
        from app.upstream import UpstreamError

        attempts.append(payload)
        if len(attempts) == 1:
            raise UpstreamError("continue_final_message is not supported")
        yield {"choices": [{"index": 0, "delta": {"content": "继续生成的内容"},
                            "finish_reason": "stop"}]}

    admin_harness.app.state.upstream.iter_chat = fake  # type: ignore[assignment]
    body = message_body(
        messages=[
            {"role": "user", "content": "开头是"},
            {"role": "assistant", "content": [{"type": "text", "text": "开头是"}]},
        ]
    )
    status, events, _ = admin_harness.stream_message(admin_harness.admin_token, body)
    assert status == 200
    assert len(attempts) == 2
    assert "continue_final_message" not in attempts[1]
    assert attempts[1]["messages"][1]["content"].endswith("继续：")
    text = "".join(
        d["delta"]["text"] for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "text_delta"
    )
    assert text == "继续生成的内容"


def test_citations_from_search_markers(admin_harness, monkeypatch):
    sources = [
        __import__("app.search", fromlist=["Source"]).Source(
            title="来源一", url="https://a.example/1", snippet="摘要一"
        ),
        __import__("app.search", fromlist=["Source"]).Source(
            title="来源二", url="https://b.example/2", snippet="摘要二"
        ),
    ]

    from app.search import SearchPlan

    plan = SearchPlan(notice="deepseek", sources=sources)

    async def fake_plan(self, **kwargs):  # type: ignore[no-untyped-def]
        return plan

    monkeypatch.setattr("app.search.SearchService.plan", fake_plan)
    monkeypatch.setattr(
        "app.search.SearchService.available", lambda self, override=None: (True, "deepseek")
    )
    admin_harness.fake_upstream(
        text_script("第一个结论[1]，第二个结论[2]。")
    )
    status, events, headers = admin_harness.stream_message(
        admin_harness.admin_token, message_body(tools=SEARCH_TOOLS)
    )
    assert status == 200
    citations = [
        d["delta"]["citation"] for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "citations_delta"
    ]
    assert [c["url"] for c in citations] == ["https://a.example/1", "https://b.example/2"]
    for citation in citations:
        assert citation["type"] == "web_search_result_location"
        assert citation["encrypted_index"] == ""
        assert isinstance(citation["cited_text"], str)
    text = "".join(
        d["delta"]["text"] for n, d in events
        if n == "content_block_delta" and d["delta"]["type"] == "text_delta"
    )
    assert "[" not in text


def test_request_validation_error_is_json(admin_harness):
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token},
        json={"system": "x", "messages": []},
    )
    assert response.status_code == 400
    body = response.json()
    assert "error" in body and isinstance(body["error"]["message"], str)


def test_auth_required(harness):
    response = harness.client.post("/anthropic/v1/messages", json=message_body())
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


def test_request_rate_limit(admin_harness, settings):
    admin_harness.app.state.limiters.messages_by_user = type(
        admin_harness.app.state.limiters.messages_by_user
    )(1, 60)
    admin_harness.fake_upstream(text_script("一次"))
    status, _, _ = admin_harness.stream_message(admin_harness.admin_token, message_body())
    assert status == 200
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token},
        json=message_body(),
    )
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_daily_token_quota(admin_harness, settings):
    admin_harness.app.state.settings.daily_token_quota = 1
    admin_harness.fake_upstream(text_script("配额"))
    status, _, _ = admin_harness.stream_message(admin_harness.admin_token, message_body())
    assert status == 200
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token},
        json=message_body(),
    )
    assert response.status_code == 429
    assert "配额" in response.json()["error"]["message"]
