"""Anthropic -> OpenAI translation tests (SPEC §3.0 - §3.7).

The hard constraint is asserted first: the client's ``system`` string must reach
the upstream as the single leading system message, byte for byte.
"""

from __future__ import annotations

import json

import pytest

from app.anthropic_compat import (
    assistant_content_to_text,
    build_title_payload,
    build_translation,
    drop_prefill,
    generate_title,
    sanitize_title,
    user_content_to_openai,
)
from app.config import Settings
from app.models import AnthropicMessageRequest
from app.upstream import THINKING_PARAM_KEYS, apply_dialect, thinking_params

from .conftest import OFFICIAL_SYSTEM, SEARCH_TOOLS, message_body


async def translate(body: dict, *, settings: Settings, store, user_id: str = "u_test"):
    request = AnthropicMessageRequest.model_validate(body)
    return await build_translation(request, user_id=user_id, store=store, settings=settings)


def system_message(payload) -> dict:
    assert payload["messages"][0]["role"] == "system"
    return payload["messages"][0]


@pytest.mark.asyncio
async def test_system_prompt_forwarded_byte_for_byte(settings, harness):
    translation = await translate(message_body(), settings=settings, store=harness.app.state.files)
    message = system_message(translation.payload)
    assert message["content"] == OFFICIAL_SYSTEM
    assert json.dumps(message["content"], ensure_ascii=False) == json.dumps(
        OFFICIAL_SYSTEM, ensure_ascii=False
    )
    assert message["content"].encode("utf-8") == OFFICIAL_SYSTEM.encode("utf-8")
    # exactly one system message, at position 0, nothing else injected
    assert [m["role"] for m in translation.payload["messages"]] == ["system", "user"]
    assert translation.payload["messages"][1]["content"] == "你好"


@pytest.mark.asyncio
async def test_empty_system_is_forwarded_verbatim_not_dropped(settings, harness):
    translation = await translate(
        message_body(system=""), settings=settings, store=harness.app.state.files
    )
    assert translation.payload["messages"][0] == {"role": "system", "content": ""}


@pytest.mark.asyncio
async def test_missing_system_adds_nothing(settings, harness):
    translation = await translate(
        message_body(system=None), settings=settings, store=harness.app.state.files
    )
    assert translation.payload["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_latest_reminder_folded_into_following_user_turn(settings, harness):
    body = message_body(
        messages=[
            {"role": "latest_reminder", "content": "当前时间：2026-11-17 13:00"},
            {"role": "user", "content": "现在几点？"},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    messages = translation.payload["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == OFFICIAL_SYSTEM  # reminder NOT merged into system
    assert messages[1]["content"] == "reminder: 当前时间：2026-11-17 13:00\n\n现在几点？"


@pytest.mark.asyncio
async def test_reminder_between_history_turns_stays_local(settings, harness):
    body = message_body(
        messages=[
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": [{"type": "text", "text": "第一答"}]},
            {"role": "latest_reminder", "content": "时间戳 A"},
            {"role": "user", "content": "第二问"},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    messages = translation.payload["messages"]
    assert messages[0]["content"] == OFFICIAL_SYSTEM
    assert messages[1]["content"] == "第一问"
    assert messages[2] == {"role": "assistant", "content": "第一答"}
    assert messages[3]["content"] == "reminder: 时间戳 A\n\n第二问"


@pytest.mark.asyncio
async def test_assistant_history_keeps_only_text(settings, harness):
    body = message_body(
        messages=[
            {"role": "user", "content": "问"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "内部推理", "signature": "sig"},
                    {"type": "text", "text": "答案第一段"},
                    {"type": "server_tool_use", "id": "srvtoolu_rl_00_x",
                     "name": "deepseek_read_url", "kind": "read_link",
                     "input": {"action": "open_page", "url": "https://example.com"},
                     "caller": {"type": "direct"}},
                    {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_rl_00_x",
                     "kind": "read_link",
                     "content": {"type": "web_fetch_tool_result_error",
                                 "error_code": "url_not_accessible"}},
                    {"type": "text", "text": "答案第二段"},
                ],
            },
            {"role": "user", "content": "继续"},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    assert translation.payload["messages"][2] == {
        "role": "assistant",
        "content": "答案第一段答案第二段",
    }


@pytest.mark.asyncio
async def test_assistant_with_only_thinking_is_dropped(settings, harness):
    body = message_body(
        messages=[
            {"role": "user", "content": "问"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "只有思考"}]},
            {"role": "user", "content": "再问"},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    assert [m["role"] for m in translation.payload["messages"]] == ["system", "user"]
    assert translation.payload["messages"][1]["content"] == "问\n\n再问"  # neighbours merged


@pytest.mark.asyncio
async def test_unknown_blocks_do_not_crash(settings, harness):
    body = message_body(
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": "你好"},
                {"type": "brand_new_block", "payload": {"a": 1}},
            ]},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    assert translation.payload["messages"][1]["content"] == "你好"


@pytest.mark.asyncio
async def test_thinking_params_are_applied_per_endpoint(settings, harness):
    """The translation payload stays dialect-free; endpoints apply the switch."""
    translation = await translate(
        message_body(thinking={"type": "disabled"}), settings=settings, store=harness.app.state.files
    )
    payload = translation.payload
    for key in THINKING_PARAM_KEYS:
        assert key not in payload, key
    assert translation.thinking_enabled is False

    applied = apply_dialect(payload, enabled=False, mode="reasoning_effort")
    assert applied["reasoning_effort"] == "none"
    assert "thinking" not in applied

    applied = apply_dialect(payload, enabled=False, mode="thinking")
    assert applied["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in applied

    applied = apply_dialect(payload, enabled=False, mode="both")
    assert applied["reasoning_effort"] == "none"
    assert applied["thinking"] == {"type": "disabled"}

    stripped = apply_dialect(payload, enabled=False, mode="both", strip=True)
    for key in THINKING_PARAM_KEYS:
        assert key not in stripped, key


@pytest.mark.asyncio
async def test_thinking_enabled_uses_the_portable_enable_hint(settings, harness):
    """enable_thinking:true is accepted by both gateways (LAN enforces it)."""
    translation = await translate(
        message_body(thinking={"type": "enabled"}), settings=settings, store=harness.app.state.files
    )
    assert translation.thinking_enabled is True
    for key in ("thinking", "reasoning_effort", "chat_template_kwargs"):
        assert key not in translation.payload, key
    applied = apply_dialect(translation.payload, enabled=True, mode="reasoning_effort")
    assert applied["enable_thinking"] is True
    assert applied["messages"] == translation.payload["messages"]
    assert applied["model"] == translation.payload["model"]


@pytest.mark.asyncio
async def test_thinking_dialect_modes_are_selectable(settings, harness):
    body = message_body(thinking={"type": "disabled"})
    expectations = {
        "reasoning_effort": {"reasoning_effort": "none"},
        "thinking": {"thinking": {"type": "disabled"}},
        "enable_thinking": {"enable_thinking": False},
        "chat_template_kwargs": {"chat_template_kwargs": {"thinking": False}},
        "both": {"reasoning_effort": "none", "thinking": {"type": "disabled"}},
        "none": {},
    }
    for mode, expected in expectations.items():
        from dataclasses import replace

        translation = await translate(
            body, settings=replace(settings, thinking_disable_mode=mode),
            store=harness.app.state.files,
        )
        applied = apply_dialect(translation.payload, enabled=False, mode=mode)
        for key in THINKING_PARAM_KEYS:
            if key in expected:
                assert applied[key] == expected[key], (mode, key)
            else:
                assert key not in applied, (mode, key)


@pytest.mark.asyncio
async def test_prefill_uses_continue_final_message(settings, harness):
    body = message_body(
        messages=[
            {"role": "user", "content": "用一句话介绍杭州。开头必须是：杭州是"},
            {"role": "assistant", "content": [{"type": "text", "text": "杭州是"}]},
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    payload = translation.payload
    assert translation.prefill is True
    assert payload["continue_final_message"] is True
    assert payload["add_generation_prompt"] is False
    assert payload["messages"][-1] == {"role": "assistant", "content": "杭州是"}


def test_drop_prefill_fallback_appends_continuation_prompt():
    payload = {
        "messages": [
            {"role": "system", "content": OFFICIAL_SYSTEM},
            {"role": "user", "content": "开头"},
            {"role": "assistant", "content": "开头"},
        ],
        "continue_final_message": True,
        "add_generation_prompt": False,
    }
    fallback = drop_prefill(payload)
    assert "continue_final_message" not in fallback
    assert "add_generation_prompt" not in fallback
    assert fallback["messages"][1]["content"] == "开头\n\n继续："
    assert payload["messages"][1]["content"] == "开头"  # original untouched


@pytest.mark.asyncio
async def test_image_file_id_becomes_data_uri(settings, admin_harness):
    store = admin_harness.app.state.files
    uploaded = admin_harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": admin_harness.admin_token},
        files={"file": ("dot.png", b"\x89PNG\r\n\x1a\nFAKE", "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["id"]
    body = message_body(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image", "source": {"type": "file", "file_id": file_id}},
                ],
            }
        ]
    )
    translation = await translate(
        body, settings=settings, store=store, user_id=admin_harness.admin_user["id"]
    )
    parts = translation.payload["messages"][1]["content"]
    assert isinstance(parts, list)
    text_part = next(p for p in parts if p["type"] == "text")
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert text_part["text"] == "看图"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_image_base64_source_is_passed_through(settings, harness):
    body = message_body(
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"},
                    }
                ],
            }
        ]
    )
    translation = await translate(body, settings=settings, store=harness.app.state.files)
    parts = translation.payload["messages"][1]["content"]
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}]


@pytest.mark.asyncio
async def test_document_text_is_inlined_and_binary_skipped(settings, admin_harness):
    store = admin_harness.app.state.files
    token = admin_harness.admin_token
    user_id = admin_harness.admin_user["id"]
    text_file = admin_harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": token},
        files={"file": ("notes.md", "# 标题\n正文".encode(), "text/markdown")},
    ).json()["id"]
    binary_file = admin_harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": token},
        files={"file": ("blob.bin", b"\x00\x01\x02\x03", "application/octet-stream")},
    ).json()["id"]
    body = message_body(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看一下文档"},
                    {"type": "document", "source": {"type": "file", "file_id": text_file}},
                    {"type": "document", "source": {"type": "file", "file_id": binary_file}},
                ],
            }
        ]
    )
    translation = await translate(body, settings=settings, store=store, user_id=user_id)
    content = translation.payload["messages"][1]["content"]
    assert '<document name="notes.md">' in content
    assert "# 标题\n正文" in content
    assert "ignored_document" not in " ".join(translation.notices)
    assert "unsupported_document_skipped" in translation.notices


@pytest.mark.asyncio
async def test_missing_image_file_degrades_to_text_only(settings, harness):
    body = message_body(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看图"},
                    {"type": "image", "source": {"type": "file", "file_id": "file_missing"}},
                ],
            }
        ]
    )
    translation = await translate(
        body, settings=settings, store=harness.app.state.files, user_id="u_x"
    )
    assert translation.payload["messages"][1]["content"] == "看图"
    assert "image_file_missing" in translation.notices


@pytest.mark.asyncio
async def test_search_tools_detected(settings, harness):
    translation = await translate(
        message_body(tools=SEARCH_TOOLS), settings=settings, store=harness.app.state.files
    )
    assert translation.wants_search is True
    assert translation.wants_read_url is True


@pytest.mark.asyncio
async def test_urls_extracted_from_last_user_message(settings, harness):
    translation = await translate(
        message_body(
            messages=[
                {"role": "user", "content": "旧问题 https://old.example.com"},
                {"role": "assistant", "content": [{"type": "text", "text": "旧回答"}]},
                {"role": "user", "content": "读一下 https://example.com/a 和 https://b.test/x"},
            ]
        ),
        settings=settings,
        store=harness.app.state.files,
    )
    assert translation.urls == ["https://example.com/a", "https://b.test/x"]


@pytest.mark.asyncio
async def test_stop_sequences_and_max_tokens(settings, harness):
    translation = await translate(
        message_body(stop_sequences=["\n\nHuman:"], max_tokens=123),
        settings=settings,
        store=harness.app.state.files,
    )
    assert translation.payload["stop"] == ["\n\nHuman:"]
    assert translation.payload["max_tokens"] == 123


@pytest.mark.asyncio
async def test_default_max_tokens_applied(settings, harness):
    translation = await translate(
        message_body(max_tokens=None), settings=settings, store=harness.app.state.files
    )
    assert translation.payload["max_tokens"] == settings.default_max_tokens


@pytest.mark.asyncio
async def test_upstream_model_mapping(settings, harness):
    for client_model in ("deepseek-flash", "deepseek-v4-pro"):
        translation = await translate(
            message_body(model=client_model), settings=settings, store=harness.app.state.files
        )
        assert translation.upstream_model == settings.model_map[client_model]
        assert translation.upstream_model == "deepseek-v4.1-flash-official"


@pytest.mark.asyncio
async def test_user_content_helper_concatenates_text_blocks(settings, harness):
    content = [
        {"type": "text", "text": "第一段"},
        {"type": "text", "text": "第二段"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}},
    ]
    converted = await user_content_to_openai(
        content,
        user_id="u1",
        store=harness.app.state.files,
        settings=settings,
        notices=[],
    )
    assert isinstance(converted, list)
    assert converted[0] == {"type": "text", "text": "第一段\n\n第二段"}
    assert converted[1]["type"] == "image_url"


def test_assistant_content_helper():
    assert assistant_content_to_text("纯字符串") == "纯字符串"
    assert assistant_content_to_text(
        [{"type": "text", "text": "A"}, {"type": "thinking", "thinking": "T"}]
    ) == "A"
    assert assistant_content_to_text([{"type": "tool_use", "id": "x"}]) == ""


def test_sanitize_title_rules():
    assert sanitize_title("  快速排序 的解释 ") == "快速排序 的解释"
    assert sanitize_title("标题：量子计算入门。") == "量子计算入门"
    assert sanitize_title("“带引号的标题”") == "带引号的标题"
    assert sanitize_title("多行\n第二行") == "多行"
    assert sanitize_title("一" * 40, limit=30) == "一" * 30
    assert sanitize_title("   ") is None
    assert sanitize_title(None) is None


def test_title_payload_has_no_stream_options(settings):
    from app.anthropic_compat import Translation

    translation = Translation(
        payload={}, upstream_model="deepseek-v4.1-flash-official", user_query="什么是快速排序"
    )
    payload = build_title_payload(translation, "快速排序是一种分治算法。", settings)
    assert "stream" not in payload
    assert "stream_options" not in payload
    for key in THINKING_PARAM_KEYS:
        assert key not in payload
    assert payload["max_tokens"] <= 128


@pytest.mark.asyncio
async def test_generate_title_salvages_reasoning(settings):
    class FakeUpstream:
        async def complete_chat(self, payload, *, thinking_enabled=False):
            assert thinking_enabled is False
            return {"choices": [{"message": {"content": None, "reasoning": "  思考出来的标题 "}}]}

    from app.anthropic_compat import Translation

    translation = Translation(payload={}, upstream_model="m", user_query="问题")
    title = await generate_title(FakeUpstream(), translation, "回答", settings)  # type: ignore[arg-type]
    assert title == "思考出来的标题"


def test_thinking_params_defaults_to_reasoning_effort():
    """reasoning_effort:"none" is the only switch verified on both gateways."""
    assert thinking_params(False, "reasoning_effort") == {"reasoning_effort": "none"}
    assert thinking_params(False, "thinking") == {"thinking": {"type": "disabled"}}
    assert thinking_params(False, "both") == {
        "reasoning_effort": "none",
        "thinking": {"type": "disabled"},
    }
    assert thinking_params(False, "enable_thinking") == {"enable_thinking": False}
    assert thinking_params(False, "chat_template_kwargs") == {
        "chat_template_kwargs": {"thinking": False}
    }
    assert thinking_params(False, "none") == {}
    assert thinking_params(True, "reasoning_effort") == {"enable_thinking": True}
    assert thinking_params(True, "both") == {"enable_thinking": True}
    assert thinking_params(True, "none") == {}
