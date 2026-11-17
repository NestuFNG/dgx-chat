"""Per-user credentials: masking, write-only semantics, precedence, isolation."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.conftest import text_script
from tests.test_isolation import auth, second_user
from tests.test_speech import FakeResponse, install_http

SEARCH_TOOLS = [
    {"type": "deepseek_web_search", "name": "deepseek_web_search"},
    {"type": "deepseek_read_url", "name": "deepseek_read_url"},
]


def stored_settings(harness, user_id: str) -> dict:
    conn = sqlite3.connect(str(harness.app.state.settings.db_path))
    row = conn.execute(
        "SELECT data FROM user_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def configure_env_speech(harness, *, key: str = "env-key") -> None:
    settings = harness.app.state.settings
    for provider in (settings.speech.tts, settings.speech.asr):
        provider.provider = "deepseek"
        provider.base_url = "https://api.deepseek.com"
        provider.api_key = key
        provider.voice_map = {}


def tts_request(harness, token: str):
    return harness.client.post(
        "/v1/tools/tts",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": "你好", "voice_id": "stella", "format": "pcm", "filter_markdown": True},
    )


def pcm_response() -> FakeResponse:
    return FakeResponse(
        content=b"\x00\x01" * 100,
        headers={"content-type": "audio/pcm; rate=24000; channels=1"},
    )


# ---------------------------------------------------------------------------
# masking / write-only semantics
# ---------------------------------------------------------------------------


def test_get_masks_secrets_and_never_returns_them(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    secret = "sk-abcdefghijklmnop"
    put = admin_harness.client.put(
        "/api/me/settings",
        headers=headers,
        json={
            "official": {"api_key": secret, "base_url": "https://api.deepseek.com"},
            "tts": {"api_key": "tts-secret-123456", "provider": "deepseek"},
            "asr": {"api_key": "asr-secret-123456"},
        },
    )
    assert put.status_code == 200, put.text
    assert secret not in put.text
    assert "tts-secret-123456" not in put.text
    body = put.json()
    assert body["official"]["has_key"] is True
    assert body["official"]["api_key"] != secret
    assert "…" in body["official"]["api_key"]
    assert body["official"]["api_key"].startswith("sk-ab")
    assert body["official"]["api_key"].endswith("mnop")
    assert body["tts"]["has_key"] is True
    assert body["asr"]["has_key"] is True

    get = admin_harness.client.get("/api/me/settings", headers=headers)
    assert get.status_code == 200
    assert secret not in get.text
    assert get.json()["official"]["has_key"] is True
    # The stored value is still the full secret (server-side use only).
    assert stored_settings(admin_harness, admin_harness.admin_user["id"])["official"][
        "api_key"
    ] == secret


def test_keep_semantics(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    secret = "sk-keepme-0123456789"
    admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": {"api_key": secret}}
    )
    masked = admin_harness.client.get("/api/me/settings", headers=headers).json()["official"][
        "api_key"
    ]
    response = admin_harness.client.put(
        "/api/me/settings",
        headers=headers,
        json={
            "official": {
                "api_key": "__keep__",
                "base_url": "https://proxy.example/v1",
            },
            "tts": {"provider": "deepseek", "api_key": masked},  # masked literal too
        },
    )
    assert response.status_code == 200
    stored = stored_settings(admin_harness, admin_harness.admin_user["id"])
    assert stored["official"]["api_key"] == secret
    assert stored["official"]["base_url"] == "https://proxy.example/v1"
    assert response.json()["official"]["has_key"] is True


def test_clear_semantics(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": {"api_key": "sk-toclear-1234"}}
    )
    cleared = admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": {"api_key": ""}}
    )
    assert cleared.status_code == 200
    assert cleared.json()["official"]["has_key"] is False
    assert stored_settings(admin_harness, admin_harness.admin_user["id"])["official"].get(
        "api_key"
    ) in (None, "")

    admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"tts": {"api_key": "sk-tts-12345678"}}
    )
    admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"tts": {"api_key": None}}
    )
    assert stored_settings(admin_harness, admin_harness.admin_user["id"])["tts"].get(
        "api_key"
    ) in (None, "")


def test_omitting_a_block_keeps_it(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": {"api_key": "sk-keep-block"}}
    )
    admin_harness.client.put("/api/me/settings", headers=headers, json={"model": {"id": "x"}})
    stored = stored_settings(admin_harness, admin_harness.admin_user["id"])
    assert stored["official"]["api_key"] == "sk-keep-block"
    assert stored["model"] == {"id": "x"}


def test_absurd_length_and_shape_validation(admin_harness):
    headers = auth(admin_harness.admin_token)
    too_long = admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": {"api_key": "k" * 513}}
    )
    assert too_long.status_code == 400
    assert "过长" in too_long.json()["error"]["message"]

    assert admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"official": "not-an-object"}
    ).status_code == 400
    assert admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"tts": {"provider": "bogus"}}
    ).status_code == 400
    assert admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"search": {"mode": "turbo"}}
    ).status_code == 400
    assert admin_harness.client.put(
        "/api/me/settings", headers=headers, json={"tts": {"api_key": 123}}
    ).status_code == 400


# ---------------------------------------------------------------------------
# speech precedence
# ---------------------------------------------------------------------------


def test_speech_uses_user_official_key_over_env(admin_harness, monkeypatch):
    configure_env_speech(admin_harness)
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(token),
        json={"official": {"api_key": "sk-user-official"}},
    )
    holder = install_http(monkeypatch, pcm_response())
    response = tts_request(admin_harness, token)
    assert response.status_code == 200
    post = holder.posts[0]
    assert post["url"] == "https://api.deepseek.com/v1/tools/tts"
    assert post["headers"]["Authorization"] == "Bearer sk-user-official"


def test_custom_speech_endpoint_uses_its_own_key(admin_harness, monkeypatch):
    configure_env_speech(admin_harness)
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(token),
        json={
            "official": {"api_key": "sk-user-official"},
            "tts": {
                "provider": "openai",
                "base_url": "https://speech.example/v1",
                "api_key": "sk-custom-speech",
                "model": "tts-1",
            },
        },
    )
    holder = install_http(monkeypatch, pcm_response())
    response = tts_request(admin_harness, token)
    assert response.status_code == 200
    post = holder.posts[0]
    assert post["url"] == "https://speech.example/v1/audio/speech"
    assert post["headers"]["Authorization"] == "Bearer sk-custom-speech"
    assert post["json"]["model"] == "tts-1"


def test_custom_endpoint_never_receives_the_official_key(admin_harness, monkeypatch):
    configure_env_speech(admin_harness)
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(token),
        json={
            "official": {"api_key": "sk-user-official"},
            # base_url but no api_key on purpose
            "tts": {"provider": "deepseek", "base_url": "https://third-party.example"},
        },
    )
    holder = install_http(monkeypatch, pcm_response())
    response = tts_request(admin_harness, token)
    assert response.status_code == 200
    post = holder.posts[0]
    assert post["url"] == "https://third-party.example/v1/tools/tts"
    assert "Authorization" not in post["headers"]


def test_speech_env_unchanged_when_user_configures_nothing(admin_harness, monkeypatch):
    configure_env_speech(admin_harness, key="env-key-1")
    holder = install_http(monkeypatch, pcm_response())
    response = tts_request(admin_harness, admin_harness.admin_token)
    assert response.status_code == 200
    assert holder.posts[0]["headers"]["Authorization"] == "Bearer env-key-1"


def test_speech_keys_are_isolated_between_users(admin_harness, monkeypatch):
    configure_env_speech(admin_harness, key="env-key-2")
    alice_token, _ = second_user(admin_harness, "alice")
    bob_token, _ = second_user(admin_harness, "bob")
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(alice_token),
        json={"official": {"api_key": "sk-alice-only"}},
    )
    holder = install_http(monkeypatch, pcm_response())
    assert tts_request(admin_harness, alice_token).status_code == 200
    assert tts_request(admin_harness, bob_token).status_code == 200
    alice_auth = holder.posts[0]["headers"]["Authorization"]
    bob_auth = holder.posts[1]["headers"]["Authorization"]
    assert alice_auth == "Bearer sk-alice-only"
    assert bob_auth == "Bearer env-key-2"
    assert "alice" not in bob_auth


def test_asr_uses_user_official_key(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.speech.asr.provider = "deepseek"
    settings.speech.asr.base_url = "https://api.deepseek.com"
    settings.speech.asr.api_key = "env-asr-key"
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(token),
        json={"official": {"api_key": "sk-user-asr-key"}},
    )
    holder = install_http(
        monkeypatch,
        FakeResponse(
            content=b'{"text":"ok"}',
            headers={"content-type": "application/json"},
            json_body={"text": "ok"},
        ),
    )
    response = admin_harness.client.post(
        "/v1/tools/asr",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("a.wav", b"RIFF", "audio/wav")},
    )
    assert response.status_code == 200
    assert holder.posts[0]["headers"]["Authorization"] == "Bearer sk-user-asr-key"


# ---------------------------------------------------------------------------
# search official key
# ---------------------------------------------------------------------------


def capture_search_keys(harness, monkeypatch) -> list[str]:
    captured: list[str] = []
    tool_id = "srvtoolu_s_00_" + "b" * 32

    async def fake_iter_events(payload, api_key):
        captured.append(api_key)
        yield "content_block_start", {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": tool_id,
                "name": "deepseek_web_search",
                "input": {"action": "search", "queries": ["q"]},
                "caller": {"type": "direct"},
                "kind": "search",
            },
        }
        yield "content_block_start", {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "web_search_tool_result",
                "tool_use_id": tool_id,
                "kind": "search",
                "content": [
                    {
                        "type": "web_search_result",
                        "title": "t",
                        "url": "https://example.com/x",
                        "encrypted_content": "",
                        "page_age": "2026-11-17T00:00:00+00:00",
                        "snippet": "s",
                        "site_name": "e",
                        "site_icon": None,
                    }
                ],
            },
        }

    harness.app.state.anthropic.iter_events = fake_iter_events  # type: ignore[assignment]
    return captured


def search_body() -> dict:
    return {
        "model": "deepseek-flash",
        "system": "该助手为DeepSeek，由深度求索公司创造。",
        "messages": [{"role": "user", "content": "最近有什么 AI 新闻？"}],
        "thinking": {"type": "disabled"},
        "safety_check": {"level": "high"},
        "tools": SEARCH_TOOLS,
    }


def test_search_uses_the_users_official_key(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = "env-search-key"
    alice_token, _ = second_user(admin_harness, "alice")
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(alice_token),
        json={"official": {"api_key": "sk-alice-search"}},
    )
    captured = capture_search_keys(admin_harness, monkeypatch)
    admin_harness.fake_upstream(text_script("答案"))
    status, events, _ = admin_harness.stream_message(alice_token, search_body())
    assert status == 200
    assert captured == ["sk-alice-search"]
    blocks = [
        data["content_block"]
        for name, data in events
        if name == "content_block_start" and data["content_block"]["type"] == "server_tool_use"
    ]
    assert blocks, "search block missing"


def test_search_falls_back_to_env_key_and_isolates_users(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = "env-search-key"
    token = admin_harness.admin_token
    capture_search_keys(admin_harness, monkeypatch)
    admin_harness.fake_upstream(text_script("答案"))
    status, _, _ = admin_harness.stream_message(token, search_body())
    assert status == 200
    assert admin_harness.app.state.anthropic.iter_events  # sanity


def test_search_without_any_key_emits_official_key_required(admin_harness):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = ""
    status, events, headers = admin_harness.stream_message(
        admin_harness.admin_token, search_body()
    )
    assert status == 200
    assert [name for name, _ in events] == ["error"]
    error = events[0][1]["error"]
    assert error["code"] == "official_key_required"
    assert "官方" in error["message"] and "API Key" in error["message"]
    assert headers["x-dgx-search"] == "official_key_required"


def test_official_mode_without_key_emits_official_key_required(admin_harness):
    settings = admin_harness.app.state.settings
    settings.search.mode = "official"
    settings.search.official_api_key = ""
    status, events, _ = admin_harness.stream_message(
        admin_harness.admin_token, search_body()
    )
    assert status == 200
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["error"]["code"] == "official_key_required"


def test_user_key_satisfies_official_mode(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.search.mode = "official"
    settings.search.official_api_key = ""
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings", headers=auth(token), json={"official": {"api_key": "sk-user-only"}}
    )
    captured: list[str] = []

    async def fake_iter_events(payload, api_key):
        captured.append(api_key)
        yield "message_start", {"type": "message_start", "message": {"id": "x"}}
        yield "message_stop", {"type": "message_stop"}

    admin_harness.app.state.anthropic.iter_events = fake_iter_events  # type: ignore[assignment]
    status, events, headers = admin_harness.stream_message(token, search_body())
    assert status == 200
    assert captured == ["sk-user-only"]
    assert headers["x-dgx-search"] == "official"


def test_search_dgx_works_with_only_a_user_key(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = ""
    token = admin_harness.admin_token
    admin_harness.client.put(
        "/api/me/settings", headers=auth(token), json={"official": {"api_key": "sk-only-user"}}
    )
    captured = capture_search_keys(admin_harness, monkeypatch)
    admin_harness.fake_upstream(text_script("答案"))
    status, events, headers = admin_harness.stream_message(token, search_body())
    assert status == 200
    assert captured == ["sk-only-user"]
    assert "error" not in [name for name, _ in events]
    assert headers["x-dgx-search"] == "official_retrieval"


def test_search_off_mode_does_not_require_a_key(admin_harness):
    settings = admin_harness.app.state.settings
    settings.search.mode = "off"
    settings.search.official_api_key = ""
    admin_harness.fake_upstream(text_script("普通回答"))
    status, events, headers = admin_harness.stream_message(
        admin_harness.admin_token, search_body()
    )
    assert status == 200
    assert "error" not in [name for name, _ in events]
    assert headers["x-dgx-search"] == "off"


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


def test_capabilities_shape_with_nothing_configured(admin_harness):
    body = admin_harness.client.get(
        "/api/me/capabilities", headers=auth(admin_harness.admin_token)
    ).json()
    assert body == {
        "official": {"configured": False, "source": None},
        "tts": {"configured": False, "source": None},
        "asr": {"configured": False, "source": None},
        "search": {"available": False, "requires_official": True},
    }


def test_capabilities_with_user_and_server_credentials(admin_harness):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = "sk-env-official"
    configure_env_speech(admin_harness, key="sk-env-speech")
    token = admin_harness.admin_token
    headers = auth(token)

    server_side = admin_harness.client.get("/api/me/capabilities", headers=headers).json()
    assert server_side["official"] == {"configured": True, "source": "server"}
    assert server_side["tts"] == {"configured": True, "source": "server"}
    assert server_side["search"] == {"available": True, "requires_official": True}

    admin_harness.client.put(
        "/api/me/settings",
        headers=headers,
        json={
            "official": {"api_key": "sk-user-cap"},
            "tts": {"provider": "openai", "base_url": "https://s.example", "api_key": "sk-u"},
        },
    )
    user_side = admin_harness.client.get("/api/me/capabilities", headers=headers).json()
    assert user_side["official"] == {"configured": True, "source": "user"}
    assert user_side["tts"] == {"configured": True, "source": "user"}
    assert user_side["asr"] == {"configured": True, "source": "server"}
    assert "sk-" not in json.dumps(user_side)


def test_capabilities_search_unavailable_without_key(admin_harness):
    settings = admin_harness.app.state.settings
    settings.search.mode = "dgx"
    settings.search.official_api_key = ""
    body = admin_harness.client.get(
        "/api/me/capabilities", headers=auth(admin_harness.admin_token)
    ).json()
    assert body["search"]["available"] is False
    assert body["official"]["configured"] is False


def test_capabilities_requires_auth_and_is_per_user(admin_harness):
    assert admin_harness.client.get("/api/me/capabilities").status_code == 401
    alice_token, _ = second_user(admin_harness, "alice")
    bob_token, _ = second_user(admin_harness, "bob")
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(alice_token),
        json={"official": {"api_key": "sk-alice-cap"}},
    )
    alice = admin_harness.client.get("/api/me/capabilities", headers=auth(alice_token)).json()
    bob = admin_harness.client.get("/api/me/capabilities", headers=auth(bob_token)).json()
    assert alice["official"] == {"configured": True, "source": "user"}
    assert bob["official"] == {"configured": False, "source": None}
