"""Authentication, invite-code and throttling tests (SPEC §2.1 + security P0)."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, token_hash, verify_password
from app.main import create_app

from tests.conftest import build_settings


def test_first_user_is_admin_without_invite(harness):
    response = harness.client.post(
        "/auth/register", json={"username": "boss", "password": "passw0rd"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["user"]["role"] == "admin"
    assert len(data["token"]) == 64
    me = harness.client.get("/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == "boss"


def test_second_registration_requires_invite_in_invite_mode(admin_harness):
    response = admin_harness.client.post(
        "/auth/register", json={"username": "second", "password": "passw0rd"}
    )
    assert response.status_code == 403
    assert "邀请码" in response.json()["error"]["message"]


def test_invalid_invite_rejected(admin_harness):
    response = admin_harness.client.post(
        "/auth/register",
        json={"username": "second", "password": "passw0rd", "invite_code": "nope"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "邀请码无效或不可用"


def test_valid_invite_consumed_once(admin_harness):
    invite = admin_harness.create_invite(
        admin_harness.admin_token, note="给同事", max_uses=1, expires_in_hours=24
    )
    assert invite.status_code == 200, invite.text
    code = invite.json()["code"]
    assert code.startswith("dgx-")

    first = admin_harness.client.post(
        "/auth/register",
        json={"username": "second", "password": "passw0rd", "invite_code": code},
    )
    assert first.status_code == 200, first.text
    assert first.json()["user"]["role"] == "user"

    second = admin_harness.client.post(
        "/auth/register",
        json={"username": "third", "password": "passw0rd", "invite_code": code},
    )
    assert second.status_code == 403

    listing = admin_harness.client.get(
        "/admin/invites", headers={"Authorization": f"Bearer {admin_harness.admin_token}"}
    )
    row = next(item for item in listing.json()["invites"] if item["code"] == code)
    assert row["usedCount"] == 1
    assert row["usesLeft"] == 0


def test_closed_mode_blocks_registration(tmp_path):
    settings = build_settings(tmp_path, registration_mode="closed")
    app = create_app(settings)
    with TestClient(app) as client:
        first = client.post("/auth/register", json={"username": "a", "password": "passw0rd"})
        assert first.status_code == 200  # bootstrap is always allowed
        admin_token = first.json()["token"]
        invite = client.post(
            "/admin/invites",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"max_uses": 5},
        ).json()["code"]
        blocked = client.post(
            "/auth/register",
            json={"username": "b", "password": "passw0rd", "invite_code": invite},
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"]["message"] == "注册暂未开放"


def test_open_mode_allows_registration_without_code(tmp_path):
    settings = build_settings(tmp_path, registration_mode="open")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/auth/register", json={"username": "a", "password": "passw0rd"}
        ).status_code == 200
        second = client.post("/auth/register", json={"username": "b", "password": "passw0rd"})
        assert second.status_code == 200
        assert second.json()["user"]["role"] == "user"


def test_revoked_and_expired_invites(admin_harness):
    token = admin_harness.admin_token
    revoked = admin_harness.create_invite(token, max_uses=3).json()["code"]
    assert admin_harness.client.delete(
        f"/admin/invites/{revoked}", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200
    response = admin_harness.client.post(
        "/auth/register",
        json={"username": "c", "password": "passw0rd", "invite_code": revoked},
    )
    assert response.status_code == 403

    expired = admin_harness.create_invite(token, max_uses=3).json()["code"]
    admin_harness.app.state.db  # ensure db ready
    import asyncio

    asyncio.get_event_loop_policy()  # no-op; use the portal via asyncio.run is unsafe here
    # Force expiry by editing the row through a fresh connection.
    import sqlite3

    conn = sqlite3.connect(str(admin_harness.app.state.settings.db_path))
    conn.execute(
        "UPDATE invites SET expires_at = ? WHERE code = ?", (int(time.time() * 1000) - 1000, expired)
    )
    conn.commit()
    conn.close()
    response = admin_harness.client.post(
        "/auth/register",
        json={"username": "d", "password": "passw0rd", "invite_code": expired},
    )
    assert response.status_code == 403


def test_login_and_generic_errors(admin_harness):
    ok = admin_harness.login("admin1", "passw0rd")
    assert ok.status_code == 200
    wrong_password = admin_harness.login("admin1", "bad")
    wrong_user = admin_harness.login("ghost", "passw0rd")
    assert wrong_password.status_code == wrong_user.status_code == 401
    assert (
        wrong_password.json()["error"]["message"]
        == wrong_user.json()["error"]["message"]
        == "用户名或密码错误"
    )


def test_credentials_in_authorization_header(admin_harness):
    response = admin_harness.client.get(
        "/auth/me", headers={"Authorization": "Bearer admin1:passw0rd"}
    )
    assert response.status_code == 200
    assert response.json()["username"] == "admin1"


def test_x_api_key_header(admin_harness):
    response = admin_harness.client.get(
        "/models", headers={"x-api-key": admin_harness.admin_token}
    )
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]
    assert ids == ["deepseek-flash", "deepseek-v4-pro"]


def test_logout_revokes_token(admin_harness):
    token = admin_harness.admin_token
    headers = {"Authorization": f"Bearer {token}"}
    assert admin_harness.client.post("/auth/logout", headers=headers).status_code == 200
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 401


def test_admin_can_revoke_user_tokens(admin_harness):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    registered = admin_harness.client.post(
        "/auth/register",
        json={"username": "victim", "password": "passw0rd", "invite_code": invite},
    ).json()
    user_id = registered["user"]["id"]
    user_headers = {"Authorization": f"Bearer {registered['token']}"}
    assert admin_harness.client.get("/auth/me", headers=user_headers).status_code == 200
    revoked = admin_harness.client.post(
        f"/admin/users/{user_id}/revoke-tokens",
        headers={"Authorization": f"Bearer {admin_harness.admin_token}"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revokedTokens"] >= 1
    assert admin_harness.client.get("/auth/me", headers=user_headers).status_code == 401


def test_admin_reset_password_revokes_tokens(admin_harness):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    registered = admin_harness.client.post(
        "/auth/register",
        json={"username": "resetme", "password": "passw0rd", "invite_code": invite},
    ).json()
    user_id = registered["user"]["id"]
    reset = admin_harness.client.post(
        f"/admin/users/{user_id}/reset-password",
        headers={"Authorization": f"Bearer {admin_harness.admin_token}"},
        json={},
    )
    assert reset.status_code == 200
    new_password = reset.json()["password"]
    assert admin_harness.client.get(
        "/auth/me", headers={"Authorization": f"Bearer {registered['token']}"}
    ).status_code == 401
    assert admin_harness.login("resetme", new_password).status_code == 200


def test_password_hashing_roundtrip():
    encoded = hash_password("s3cret", n=2**12)
    assert encoded.startswith("scrypt$")
    assert verify_password("s3cret", encoded) is True
    assert verify_password("wrong", encoded) is False
    assert verify_password("s3cret", "garbage") is False
    assert hash_password("s3cret", n=2**12) != encoded  # unique salt


def test_token_hash_is_sha256():
    import hashlib

    assert token_hash("abc") == hashlib.sha256(b"abc").hexdigest()


def test_login_throttling(admin_harness):
    statuses = [admin_harness.login("admin1", "bad").status_code for _ in range(10)]
    assert statuses.count(401) == 10
    final = admin_harness.login("admin1", "bad")
    assert final.status_code == 429
    assert "Retry-After" in final.headers
    # correct password still blocked while the window is hot
    assert admin_harness.login("admin1", "passw0rd").status_code == 429


def test_register_throttling(tmp_path):
    settings = build_settings(tmp_path, registration_mode="open", register_max_per_hour=2)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post(
            "/auth/register", json={"username": "u1", "password": "passw0rd"}
        ).status_code == 200
        assert client.post(
            "/auth/register", json={"username": "u2", "password": "passw0rd"}
        ).status_code == 200
        third = client.post("/auth/register", json={"username": "u3", "password": "passw0rd"})
        assert third.status_code == 429
        assert "Retry-After" in third.headers


def test_token_ttl_expiry(tmp_path):
    settings = build_settings(tmp_path, registration_mode="open", token_ttl_days=1)
    app = create_app(settings)
    with TestClient(app) as client:
        registered = client.post(
            "/auth/register", json={"username": "ttl", "password": "passw0rd"}
        ).json()
        token = registered["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/auth/me", headers=headers).status_code == 200

        import sqlite3

        conn = sqlite3.connect(str(settings.db_path))
        conn.execute(
            "UPDATE tokens SET expires_at = ? WHERE token_hash = ?",
            (int(time.time() * 1000) - 5000, token_hash(token)),
        )
        conn.commit()
        conn.close()
        assert client.get("/auth/me", headers=headers).status_code == 401


def test_short_password_rejected(harness):
    response = harness.client.post(
        "/auth/register", json={"username": "shorty", "password": "123"}
    )
    assert response.status_code == 400
    assert "密码" in response.json()["error"]["message"]


def test_duplicate_username_conflict(admin_harness):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    response = admin_harness.client.post(
        "/auth/register",
        json={"username": "admin1", "password": "passw0rd", "invite_code": invite},
    )
    assert response.status_code == 409


def test_non_admin_cannot_use_admin_api(admin_harness):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    registered = admin_harness.client.post(
        "/auth/register",
        json={"username": "pleb", "password": "passw0rd", "invite_code": invite},
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    assert admin_harness.client.get("/admin/users", headers=headers).status_code == 403
    assert admin_harness.client.post(
        "/admin/invites", headers=headers, json={"max_uses": 1}
    ).status_code == 403


def test_docs_disabled_by_default(harness):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert harness.client.get(path).status_code == 404


def test_health_is_minimal_for_anonymous(harness):
    body = harness.client.get("/health").json()
    assert body == {"ok": True, "status": "up"}


def test_health_details_for_admin(admin_harness):
    body = admin_harness.client.get(
        "/health", headers={"Authorization": f"Bearer {admin_harness.admin_token}"}
    ).json()
    assert body["version"]
    assert body["registrationMode"] == "invite"


def test_server_header_is_constant(harness):
    response = harness.client.get("/health")
    assert response.headers["server"] == "dgxchat"


def test_landing_page_has_no_hints(harness):
    html = harness.client.get("/").text
    for banned in ("DGX", "docs", "/admin", "deepseek", "DeepSeek", "version", "Python"):
        assert banned not in html
    assert "Service is running." in html


def test_unknown_api_path_is_404_json(harness):
    response = harness.client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


def test_cors_preflight_for_capacitor(harness):
    response = harness.client.options(
        "/anthropic/v1/messages",
        headers={
            "Origin": "https://localhost",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-api-key,anthropic-beta,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://localhost"
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in ("authorization", "x-api-key", "anthropic-beta", "content-type"):
        assert header in allowed
    methods = response.headers["access-control-allow-methods"]
    for method in ("POST", "PUT", "DELETE", "OPTIONS"):
        assert method in methods


def test_change_password_keeps_current_token(admin_harness):
    second = admin_harness.login("admin1", "passw0rd").json()["token"]
    current = admin_harness.login("admin1", "passw0rd").json()["token"]
    headers = {"Authorization": f"Bearer {current}"}

    assert admin_harness.client.post(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "wrong", "new_password": "brandnew123"},
    ).status_code == 401
    assert admin_harness.client.post(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "passw0rd", "new_password": "passw0rd"},
    ).status_code == 400

    changed = admin_harness.client.post(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "passw0rd", "new_password": "brandnew123"},
    )
    assert changed.status_code == 200
    assert changed.json()["ok"] is True
    assert changed.json()["revokedOtherTokens"] >= 2
    # Current token survives, the other sessions do not.
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 200
    assert admin_harness.client.get(
        "/auth/me", headers={"Authorization": f"Bearer {second}"}
    ).status_code == 401
    assert admin_harness.login("admin1", "brandnew123").status_code == 200
    assert admin_harness.login("admin1", "passw0rd").status_code == 401


def test_change_password_is_audited(admin_harness):
    headers = {"Authorization": f"Bearer {admin_harness.admin_token}"}
    admin_harness.client.post(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "passw0rd", "new_password": "auditpass123"},
    )
    entries = admin_harness.client.get("/admin/audit?limit=20", headers=headers).json()["entries"]
    entry = next(e for e in entries if e["action"] == "user.password_changed")
    assert entry["actorName"] == "admin1"
    assert "auditpass123" not in (entry["detail"] or "")


def test_cors_default_is_not_wildcard(settings):
    assert "*" not in settings.cors_origins
    assert "https://localhost" in settings.cors_origins


def test_internal_errors_do_not_leak_details(tmp_path):
    """The client only ever sees {"error":{"message":"internal error"}}."""
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        async def boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("secret C:/app/app/main.py line 42 token=abc")

        # /auth/me resolves the caller through db.fetch_one: let it explode.
        app.state.db.fetch_one = boom  # type: ignore[assignment]
        response = client.get(
            "/auth/me", headers={"Authorization": "Bearer whatever"}
        )
        assert response.status_code == 500
        text = response.text
        assert "secret" not in text
        assert "Traceback" not in text
        assert "main.py" not in text
        assert "token=" not in text
        body = response.json()
        assert body["error"]["message"] == "internal error"
        assert body["error"]["type"] == "server_error"
        assert response.headers["server"] == "dgxchat"


def test_version_endpoint_is_minimal(harness):
    body = harness.client.get("/version").json()
    assert body == {"version": "1.0.0"}
    assert "python" not in body
    assert "startedAt" not in body


def test_json_body_cap(admin_harness):
    admin_harness.app.state.settings.max_json_body_bytes = 200
    payload = {
        "model": "deepseek-flash",
        "system": "x" * 500,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = admin_harness.client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": admin_harness.admin_token},
        json=payload,
    )
    assert response.status_code == 413
    assert response.json()["error"]["type"] == "request_too_large"


def test_upload_body_cap_via_content_length(admin_harness):
    admin_harness.app.state.settings.max_upload_bytes = 64
    response = admin_harness.client.put(
        "/api/files/too-big",
        headers={
            "Authorization": f"Bearer {admin_harness.admin_token}",
            "content-type": "application/octet-stream",
            "content-length": "1000000",
        },
        content=b"small but declared huge",
    )
    assert response.status_code == 413


def test_health_does_not_expose_config_anonymously(harness):
    body = harness.client.get("/health").json()
    for leaked in ("upstream", "dataDir", "searchMode", "version", "ttsProvider"):
        assert leaked not in body
