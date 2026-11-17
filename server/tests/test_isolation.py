"""Multi-user isolation and cloud-sync API tests (SPEC §2.3)."""

from __future__ import annotations

import json

import pytest


def make_session(session_id: str, title: str, updated_at: int, messages=None) -> dict:
    return {
        "id": session_id,
        "title": title,
        "createdAt": updated_at - 100,
        "updatedAt": updated_at,
        "messages": messages or [],
    }


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def second_user(admin_harness, username: str = "alice", password: str = "passw0rd"):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    response = admin_harness.client.post(
        "/auth/register",
        json={"username": username, "password": password, "invite_code": invite},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data["token"], data["user"]


def test_session_roundtrip(admin_harness):
    headers = auth(admin_harness.admin_token)
    session = make_session("sess-1", "标题", 1000)
    put = admin_harness.client.put("/api/sessions/sess-1", headers=headers, json=session)
    assert put.status_code == 200
    assert put.json()["id"] == "sess-1"
    assert put.json()["title"] == "标题"

    listing = admin_harness.client.get("/api/sessions", headers=headers).json()
    assert listing == [
        {"id": "sess-1", "title": "标题", "createdAt": 900, "updatedAt": 1000}
    ]

    fetched = admin_harness.client.get("/api/sessions/sess-1", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json() == session  # stored verbatim

    deleted = admin_harness.client.delete("/api/sessions/sess-1", headers=headers)
    assert deleted.status_code == 200
    assert admin_harness.client.get("/api/sessions/sess-1", headers=headers).status_code == 404


def test_session_last_write_wins(admin_harness):
    headers = auth(admin_harness.admin_token)
    newer = make_session("sess-lww", "新标题", 5000)
    older = make_session("sess-lww", "旧标题", 4000)
    assert admin_harness.client.put(
        "/api/sessions/sess-lww", headers=headers, json=newer
    ).status_code == 200
    conflict = admin_harness.client.put("/api/sessions/sess-lww", headers=headers, json=older)
    assert conflict.status_code == 200
    assert conflict.json()["conflict"] is True
    assert conflict.json()["title"] == "新标题"
    stored = admin_harness.client.get("/api/sessions/sess-lww", headers=headers).json()
    assert stored["title"] == "新标题"


def test_sessions_are_isolated_between_users(admin_harness):
    alice_token, _ = second_user(admin_harness)
    bob_token, _ = second_user(admin_harness, "bob")

    admin_harness.client.put(
        "/api/sessions/shared-id",
        headers=auth(alice_token),
        json=make_session("shared-id", "alice 的会话", 1000),
    )
    # Bob uses the SAME id: composite primary key must keep both rows apart.
    admin_harness.client.put(
        "/api/sessions/shared-id",
        headers=auth(bob_token),
        json=make_session("shared-id", "bob 的会话", 2000),
    )
    alice_list = admin_harness.client.get("/api/sessions", headers=auth(alice_token)).json()
    bob_list = admin_harness.client.get("/api/sessions", headers=auth(bob_token)).json()
    assert [item["title"] for item in alice_list] == ["alice 的会话"]
    assert [item["title"] for item in bob_list] == ["bob 的会话"]

    alice_session = admin_harness.client.get(
        "/api/sessions/shared-id", headers=auth(alice_token)
    ).json()
    assert alice_session["title"] == "alice 的会话"

    # Bob deleting his own row must not touch Alice's.
    admin_harness.client.delete("/api/sessions/shared-id", headers=auth(bob_token))
    assert admin_harness.client.get(
        "/api/sessions/shared-id", headers=auth(alice_token)
    ).status_code == 200


def test_session_id_mismatch_rejected(admin_harness):
    headers = auth(admin_harness.admin_token)
    response = admin_harness.client.put(
        "/api/sessions/path-id",
        headers=headers,
        json=make_session("other-id", "标题", 1000),
    )
    assert response.status_code == 400


def test_client_file_roundtrip_and_isolation(admin_harness):
    alice_token, _ = second_user(admin_harness)
    bob_token, _ = second_user(admin_harness, "bob")
    payload = b"\x89PNG\r\n\x1a\nhello-bytes"
    put = admin_harness.client.put(
        "/api/files/local-att-1",
        headers={**auth(alice_token), "content-type": "image/png"},
        content=payload,
    )
    assert put.status_code == 200
    assert put.json()["size"] == len(payload)

    fetched = admin_harness.client.get("/api/files/local-att-1", headers=auth(alice_token))
    assert fetched.status_code == 200
    assert fetched.content == payload
    assert fetched.headers["content-type"] == "image/png"

    missing = admin_harness.client.get("/api/files/local-att-1", headers=auth(bob_token))
    assert missing.status_code == 404

    # Idempotent overwrite
    admin_harness.client.put(
        "/api/files/local-att-1",
        headers={**auth(alice_token), "content-type": "image/png"},
        content=b"replacement",
    )
    assert admin_harness.client.get(
        "/api/files/local-att-1", headers=auth(alice_token)
    ).content == b"replacement"


def test_client_file_id_traversal_rejected(admin_harness):
    headers = auth(admin_harness.admin_token)
    for bad in ("..", "a..b", "with space", "slash/inside"):
        response = admin_harness.client.put(
            f"/api/files/{bad}", headers=headers, content=b"x"
        )
        assert response.status_code in (400, 404, 405), bad


def test_user_settings_roundtrip_and_isolation(admin_harness):
    alice_token, _ = second_user(admin_harness)
    bob_token, _ = second_user(admin_harness, "bob")
    settings = {
        "tts": {"provider": "deepseek", "voice": "tide"},
        "search": {"mode": "off", "provider": "none"},
        "model": {"id": "deepseek-v4-pro"},
    }
    put = admin_harness.client.put(
        "/api/me/settings", headers=auth(alice_token), json=settings
    )
    assert put.status_code == 200
    stored = admin_harness.client.get("/api/me/settings", headers=auth(alice_token)).json()
    # Non-secret values round-trip exactly; credential blocks are always present
    # in masked form (api_key null + has_key false when unset).
    assert stored["model"] == settings["model"]
    assert stored["tts"]["provider"] == "deepseek"
    assert stored["tts"]["voice"] == "tide"
    assert stored["tts"]["api_key"] is None
    assert stored["tts"]["has_key"] is False
    assert stored["search"]["mode"] == "off"
    assert stored["search"]["provider"] == "none"
    assert stored["official"] == {"api_key": None, "base_url": None, "has_key": False}
    # Another user sees an empty (not shared) settings document.
    other = admin_harness.client.get("/api/me/settings", headers=auth(bob_token)).json()
    assert "model" not in other
    assert other["tts"]["has_key"] is False
    assert other["official"]["has_key"] is False


def test_admin_stats_and_user_listing(admin_harness):
    token, _ = second_user(admin_harness)
    admin_harness.client.put(
        "/api/sessions/x", headers=auth(token), json=make_session("x", "t", 1000)
    )
    stats = admin_harness.client.get(
        "/admin/stats", headers=auth(admin_harness.admin_token)
    )
    assert stats.status_code == 200
    body = stats.json()
    assert body["users"] >= 2
    assert body["sessions"] >= 1
    users = admin_harness.client.get(
        "/admin/users", headers=auth(admin_harness.admin_token)
    ).json()["users"]
    alice = next(user for user in users if user["username"] == "alice")
    assert alice["sessionCount"] == 1


def test_admin_delete_user_cascades(admin_harness):
    token, user = second_user(admin_harness, "todelete")
    admin_harness.client.put(
        "/api/sessions/cascade", headers=auth(token), json=make_session("cascade", "t", 1)
    )
    admin_harness.client.put(
        "/api/files/cascade-file", headers={**auth(token), "content-type": "text/plain"},
        content=b"data",
    )
    response = admin_harness.client.delete(
        f"/admin/users/{user['id']}", headers=auth(admin_harness.admin_token)
    )
    assert response.status_code == 200
    assert admin_harness.client.get("/auth/me", headers=auth(token)).status_code == 401
    assert admin_harness.client.get("/api/sessions", headers=auth(token)).status_code == 401


def test_admin_cannot_delete_self_or_last_admin(admin_harness):
    response = admin_harness.client.delete(
        f"/admin/users/{admin_harness.admin_user['id']}",
        headers=auth(admin_harness.admin_token),
    )
    assert response.status_code == 400


def test_storage_isolation_on_disk(admin_harness, settings):
    """A user's files live in their own directory and cannot collide."""
    alice_token, alice_user = second_user(admin_harness)
    bob_token, bob_user = second_user(admin_harness, "bob")
    upload = admin_harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": alice_token},
        files={"file": ("a.txt", b"alice", "text/plain")},
    ).json()
    alice_dir = settings.files_dir / alice_user["id"]
    bob_dir = settings.files_dir / bob_user["id"]
    assert (alice_dir / upload["id"]).is_file()
    assert not bob_dir.exists() or not (bob_dir / upload["id"]).exists()
    # Bob cannot read Alice's server file id
    assert admin_harness.client.get(
        f"/anthropic/v1/files/{upload['id']}", headers={"x-api-key": bob_token}
    ).status_code == 404
