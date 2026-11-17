"""Self-service account APIs: personal tokens, self-deletion, usage stats."""

from __future__ import annotations

import sqlite3
import time

import pytest

from tests.conftest import message_body


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def db_path(harness) -> str:
    return str(harness.app.state.settings.db_path)


def second_user(harness, username: str = "alice", password: str = "passw0rd"):
    invite = harness.create_invite(harness.admin_token, max_uses=1).json()["code"]
    response = harness.client.post(
        "/auth/register",
        json={"username": username, "password": password, "invite_code": invite},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return data["token"], data["user"]


def test_tokens_listing_never_leaks_raw_tokens(admin_harness):
    login = admin_harness.login("admin1", "passw0rd").json()
    other_token = login["token"]
    response = admin_harness.client.get("/api/me/tokens", headers=auth(other_token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 2
    ids = [item["id"] for item in body["tokens"]]
    assert len(ids) == len(set(ids))
    assert all(isinstance(i, str) and 8 <= len(i) <= 64 for i in ids)
    assert other_token not in response.text
    current = [item for item in body["tokens"] if item["current"]]
    assert len(current) == 1
    assert set(body["tokens"][0]) >= {
        "id", "label", "createdAt", "lastUsedAt", "expiresAt", "revoked", "current"
    }


def test_last_used_at_is_refreshed_and_throttled(admin_harness, settings):
    token = admin_harness.admin_token
    headers = auth(token)
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 200

    conn = sqlite3.connect(db_path(admin_harness))
    last_used = conn.execute(
        "SELECT last_used_at FROM tokens WHERE revoked = 0 ORDER BY created_at DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert last_used is not None

    # Fresh value: the next auth must NOT write again (throttled to 1/minute).
    stamp = int(time.time() * 1000)
    conn = sqlite3.connect(db_path(admin_harness))
    conn.execute("UPDATE tokens SET last_used_at = ?", (stamp,))
    conn.commit()
    conn.close()
    admin_harness.client.get("/auth/me", headers=headers)
    conn = sqlite3.connect(db_path(admin_harness))
    after = conn.execute(
        "SELECT MAX(last_used_at) FROM tokens WHERE revoked = 0"
    ).fetchone()[0]
    conn.close()
    assert after == stamp

    # Older than a minute: it refreshes.
    old = stamp - 120_000
    conn = sqlite3.connect(db_path(admin_harness))
    conn.execute("UPDATE tokens SET last_used_at = ?", (old,))
    conn.commit()
    conn.close()
    admin_harness.client.get("/auth/me", headers=headers)
    conn = sqlite3.connect(db_path(admin_harness))
    refreshed = conn.execute("SELECT MAX(last_used_at) FROM tokens WHERE revoked = 0").fetchone()[0]
    conn.close()
    assert refreshed > old


def test_revoke_a_single_token(admin_harness):
    other = admin_harness.login("admin1", "passw0rd").json()["token"]
    tokens = admin_harness.client.get("/api/me/tokens", headers=auth(other)).json()["tokens"]
    target = next(item for item in tokens if not item["current"])
    response = admin_harness.client.delete(
        f"/api/me/tokens/{target['id']}", headers=auth(other)
    )
    assert response.status_code == 200
    assert response.json()["revoked"] == 1
    assert admin_harness.client.get("/auth/me", headers=auth(other)).status_code == 200
    # Deleting it again is a 404
    assert admin_harness.client.delete(
        f"/api/me/tokens/{target['id']}", headers=auth(other)
    ).status_code == 404


def test_revoke_other_tokens(admin_harness):
    other = admin_harness.login("admin1", "passw0rd").json()["token"]
    response = admin_harness.client.post("/api/me/tokens/revoke-others", headers=auth(other))
    assert response.status_code == 200
    assert response.json()["revokedOtherTokens"] >= 1
    assert admin_harness.client.get("/auth/me", headers=auth(other)).status_code == 200
    assert admin_harness.client.get(
        "/auth/me", headers=auth(admin_harness.admin_token)
    ).status_code == 401


def test_token_id_validation_and_isolation(admin_harness):
    alice_token, _ = second_user(admin_harness)
    admin_tokens = admin_harness.client.get(
        "/api/me/tokens", headers=auth(admin_harness.admin_token)
    ).json()["tokens"]
    target = admin_tokens[0]["id"]
    assert admin_harness.client.delete(
        f"/api/me/tokens/{target}", headers=auth(alice_token)
    ).status_code == 404
    assert admin_harness.client.delete(
        "/api/me/tokens/x", headers=auth(alice_token)
    ).status_code == 400


def test_delete_account_requires_password(admin_harness):
    token, _ = second_user(admin_harness)
    headers = auth(token)
    assert admin_harness.client.request(
        "DELETE", "/api/me", headers=headers, json={}
    ).status_code == 400
    wrong = admin_harness.client.request(
        "DELETE", "/api/me", headers=headers, json={"password": "nope"}
    )
    assert wrong.status_code == 401
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 200


def test_delete_account_removes_everything(admin_harness):
    token, user = second_user(admin_harness, "bye")
    headers = auth(token)
    admin_harness.client.put(
        "/api/sessions/s1", headers=headers,
        json={"id": "s1", "title": "t", "createdAt": 1, "updatedAt": 2, "messages": []},
    )
    admin_harness.client.put(
        "/api/files/att1", headers={**headers, "content-type": "text/plain"}, content=b"data"
    )
    upload = admin_harness.client.post(
        "/anthropic/v1/files", headers={"x-api-key": token},
        files={"file": ("a.txt", b"hello", "text/plain")},
    ).json()
    user_dir = admin_harness.app.state.settings.files_dir / user["id"]
    assert user_dir.is_dir() and (user_dir / upload["id"]).is_file()

    response = admin_harness.client.request(
        "DELETE", "/api/me", headers=headers, json={"password": "passw0rd"}
    )
    assert response.status_code == 200, response.text
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 401
    assert not user_dir.exists()
    conn = sqlite3.connect(db_path(admin_harness))
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user["id"],)).fetchone()[0]
        for table in ("sessions", "session_summaries", "files", "client_files", "tokens")
    }
    settings_count = conn.execute(
        "SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (user["id"],)
    ).fetchone()[0]
    users_count = conn.execute("SELECT COUNT(*) FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
    conn.close()
    assert counts == {key: 0 for key in counts}
    assert settings_count == 0
    assert users_count == 0


def test_last_admin_cannot_delete_self(admin_harness):
    response = admin_harness.client.request(
        "DELETE", "/api/me",
        headers=auth(admin_harness.admin_token),
        json={"password": "passw0rd"},
    )
    assert response.status_code == 400
    assert "最后一个管理员" in response.json()["error"]["message"]


def test_second_admin_can_delete_self(admin_harness):
    created = admin_harness.create_user(
        admin_harness.admin_token, "admin2", "passw0rd", role="admin"
    )
    assert created.status_code == 201
    token = admin_harness.login("admin2", "passw0rd").json()["token"]
    response = admin_harness.client.request(
        "DELETE", "/api/me", headers=auth(token), json={"password": "passw0rd"}
    )
    assert response.status_code == 200


def test_my_stats(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    admin_harness.client.put(
        "/api/sessions/stats-session", headers=headers,
        json={"id": "stats-session", "title": "t", "createdAt": 1, "updatedAt": 2,
              "messages": []},
    )
    admin_harness.client.put(
        "/api/files/stats-file", headers={**headers, "content-type": "text/plain"},
        content=b"x" * 2048,
    )
    admin_harness.fake_upstream()
    status, _, _ = admin_harness.stream_message(token, message_body())
    assert status == 200

    response = admin_harness.client.get("/api/me/stats", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["user"]["username"] == "admin1"
    assert data["sessions"] >= 1
    assert data["files"] >= 1
    assert data["fileBytes"] >= 2048
    assert data["requests"] >= 1
    assert len(data["daily"]) == 14
    assert data["daily"][-1]["day"]
    assert data["daily"][-1]["requests"] >= 1
    assert "storageLimitBytes" in data
    assert "ttsCharsToday" in data and "ttsCharsTotal" in data


def test_my_stats_requires_auth(harness):
    assert harness.client.get("/api/me/stats").status_code == 401
    assert harness.client.get("/api/me/tokens").status_code == 401
