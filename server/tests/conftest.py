"""Shared pytest fixtures.

Tests run fully offline: the DGX gateway is replaced by an async-generator stub
that records the outgoing payload, so translation and SSE framing can be
asserted exactly.  Real-gateway coverage lives in ``tests/live_smoke.py``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, AsyncIterator, Callable

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_CORS_ORIGINS, SearchConfig, Settings, SpeechConfig
from app.main import create_app
from app.upstream import UpstreamError

OFFICIAL_SYSTEM = "该助手为DeepSeek，由深度求索公司创造。"
SEARCH_TOOLS = [
    {"type": "deepseek_web_search", "name": "deepseek_web_search"},
    {"type": "deepseek_read_url", "name": "deepseek_read_url"},
]


def build_settings(tmp_path, **overrides: Any) -> Settings:
    base = Settings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "test.db",
        files_dir=tmp_path / "data" / "files",
        web_dir=tmp_path / "web",
        host="127.0.0.1",
        port=0,
        registration_mode="invite",
        allow_open_registration=False,
        invite_code="",
        docs_enabled=False,
        log_upstream=False,
        login_max_failures=10,
        login_window_seconds=300,
        register_max_per_hour=5,
        message_rate_per_minute=20,
        daily_token_quota=0,
        daily_tts_chars=0,
        token_ttl_days=0,
        cors_origins=list(DEFAULT_CORS_ORIGINS),
        search=SearchConfig(mode="off", official_api_key="", provider_api_key=""),
        speech=SpeechConfig(
            tts=SpeechConfig().tts,
            asr=SpeechConfig().asr,
            ffmpeg_bin=None,
            max_asr_bytes=8 * 1024 * 1024,
        ),
    )
    if overrides:
        base = replace(base, **overrides)
    return base


class AppHarness:
    """TestClient plus helpers to stub the upstream and read its payload."""

    def __init__(self, app, client: TestClient) -> None:
        self.app = app
        self.client = client
        self.captured: dict[str, Any] = {}

    # -- upstream stubs ----------------------------------------------------
    def fake_upstream(self, script: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        captured: dict[str, Any] = {"payloads": [], "calls": 0}
        chunks = script if script is not None else [text_script("你好")]

        async def fake_iter_chat(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
            captured["calls"] += 1
            captured["payloads"].append(payload)
            captured["thinking_enabled"] = thinking_enabled
            for chunk in chunks:
                yield chunk

        self.app.state.upstream.iter_chat = fake_iter_chat  # type: ignore[assignment]
        self.captured = captured
        return captured

    def fake_upstream_error(self, message: str, *, status: int = 400) -> None:
        captured: dict[str, Any] = {"payloads": [], "calls": 0}

        async def failing(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
            captured["calls"] += 1
            captured["payloads"].append(payload)
            raise UpstreamError(message, status_code=status)
            yield {}  # pragma: no cover - generator marker

        self.app.state.upstream.iter_chat = failing  # type: ignore[assignment]
        self.captured = captured

    def fake_title(self, title: str | None) -> None:
        calls: list[dict[str, Any]] = []

        async def fake_complete(payload, *, thinking_enabled=False):  # type: ignore[no-untyped-def]
            calls.append({"payload": payload, "thinking_enabled": thinking_enabled})
            return {
                "choices": [
                    {"message": {"content": title, "reasoning": None}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }

        self.app.state.upstream.complete_chat = fake_complete  # type: ignore[assignment]
        self.title_calls = calls

    # -- auth helpers ------------------------------------------------------
    def register(self, username: str = "admin1", password: str = "passw0rd") -> dict[str, Any]:
        response = self.client.post(
            "/auth/register", json={"username": username, "password": password}
        )
        return response

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self.client.post(
            "/auth/login", json={"username": username, "password": password}
        )

    def token_for(self, username: str, password: str = "passw0rd") -> str:
        response = self.login(username, password)
        assert response.status_code == 200, response.text
        return response.json()["token"]

    def create_user(self, admin_token: str, username: str, password: str = "passw0rd",
                    role: str = "user") -> dict[str, Any]:
        return self.client.post(
            "/admin/users",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"username": username, "password": password, "role": role},
        )

    def create_invite(self, admin_token: str, **body: Any) -> dict[str, Any]:
        return self.client.post(
            "/admin/invites",
            headers={"Authorization": f"Bearer {admin_token}"},
            json=body,
        )

    def stream_message(self, token: str, body: dict[str, Any]) -> tuple[int, list[tuple[str, dict]], dict]:
        """POST /anthropic/v1/messages and return ``(status, events, headers)``."""
        events: list[tuple[str, dict]] = []
        headers: dict[str, str] = {}
        with self.client.stream(
            "POST",
            "/anthropic/v1/messages",
            headers={"x-api-key": token, "content-type": "application/json"},
            json=body,
        ) as response:
            headers = dict(response.headers)
            if response.status_code != 200:
                return response.status_code, [], headers
            event_name = "message"
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    events.append((event_name, json.loads(line[6:])))
        return 200, events, headers


@pytest.fixture
def settings(tmp_path):
    return build_settings(tmp_path)


@pytest.fixture
def harness(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        yield AppHarness(app, client)


@pytest.fixture
def admin_harness(harness):
    response = harness.register("admin1", "passw0rd")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["user"]["role"] == "admin"
    harness.admin_token = data["token"]
    harness.admin_user = data["user"]
    return harness


# ---------------------------------------------------------------------------
# script helpers
# ---------------------------------------------------------------------------


def text_script(text: str, *, reasoning: str | None = None,
                usage: dict[str, Any] | None = None,
                finish_reason: str | None = "stop") -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    if reasoning:
        chunks.append({"choices": [{"index": 0, "delta": {"reasoning": reasoning},
                                    "finish_reason": None}]})
    chunks.append({"choices": [{"index": 0, "delta": {"content": text},
                                "finish_reason": finish_reason}]})
    chunks.append(
        {
            "choices": [],
            "usage": usage
            or {
                "prompt_tokens": 30,
                "completion_tokens": 12,
                "total_tokens": 42,
                "prompt_tokens_details": {"cached_tokens": 4, "created_cache_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
    )
    return chunks


def message_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "model": "deepseek-flash",
        "system": OFFICIAL_SYSTEM,
        "messages": [{"role": "user", "content": "你好"}],
        "thinking": {"type": "disabled"},
        "safety_check": {"level": "high"},
        "tools": [],
        "stream": True,
    }
    body.update(overrides)
    return body


def event_types(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


def blocks_of(events: list[tuple[str, dict]]) -> list[dict[str, Any]]:
    return [data["content_block"] for name, data in events if name == "content_block_start"]
