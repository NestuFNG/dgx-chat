"""Admin extras: enable/disable, audit trail, diagnostics, cleanup, specific reset."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from tests.test_isolation import auth, second_user


def db_path(harness) -> str:
    return str(harness.app.state.settings.db_path)


def make_user(harness, username: str, password: str = "passw0rd", role: str = "user"):
    response = harness.create_user(harness.admin_token, username, password, role)
    assert response.status_code == 201, response.text
    return response.json()


def install_fake_upstream_http(harness, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list", "data": [{"id": "m"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    harness.app.state.upstream._client = client
    return client


def test_disable_and_enable_user(admin_harness):
    user = make_user(admin_harness, "toggled")
    token = admin_harness.login("toggled", "passw0rd").json()["token"]
    headers = auth(token)
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 200

    disabled = admin_harness.client.post(
        f"/admin/users/{user['id']}/disable", headers=auth(admin_harness.admin_token)
    )
    assert disabled.status_code == 200
    assert disabled.json()["disabled"] is True
    assert disabled.json()["revokedTokens"] >= 1
    assert admin_harness.client.get("/auth/me", headers=headers).status_code == 401
    assert admin_harness.login("toggled", "passw0rd").status_code == 401

    users = admin_harness.client.get(
        "/admin/users", headers=auth(admin_harness.admin_token)
    ).json()["users"]
    assert next(item for item in users if item["id"] == user["id"])["disabled"] is True

    enabled = admin_harness.client.post(
        f"/admin/users/{user['id']}/enable", headers=auth(admin_harness.admin_token)
    )
    assert enabled.status_code == 200
    assert admin_harness.login("toggled", "passw0rd").status_code == 200


def test_cannot_disable_self_or_last_admin(admin_harness):
    response = admin_harness.client.post(
        f"/admin/users/{admin_harness.admin_user['id']}/disable",
        headers=auth(admin_harness.admin_token),
    )
    assert response.status_code == 400


def test_disable_requires_admin(admin_harness):
    user = make_user(admin_harness, "victim")
    token, _ = second_user(admin_harness, "pleb")
    response = admin_harness.client.post(
        f"/admin/users/{user['id']}/disable", headers=auth(token)
    )
    assert response.status_code == 403


def test_specific_password_reset(admin_harness):
    user = make_user(admin_harness, "resetme")
    response = admin_harness.client.post(
        f"/admin/users/{user['id']}/reset-password",
        headers=auth(admin_harness.admin_token),
        json={"password": "chosen-password-9"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["password"] == "chosen-password-9"
    assert body["generated"] is False
    assert admin_harness.login("resetme", "chosen-password-9").status_code == 200
    assert admin_harness.login("resetme", "passw0rd").status_code == 401

    generated = admin_harness.client.post(
        f"/admin/users/{user['id']}/reset-password",
        headers=auth(admin_harness.admin_token),
        json={},
    ).json()
    assert generated["generated"] is True
    assert len(generated["password"]) >= 8


def test_short_specific_password_rejected(admin_harness):
    user = make_user(admin_harness, "shorty")
    response = admin_harness.client.post(
        f"/admin/users/{user['id']}/reset-password",
        headers=auth(admin_harness.admin_token),
        json={"password": "123"},
    )
    assert response.status_code == 400


def test_audit_records_actions(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    user = make_user(admin_harness, "audited")
    invite = admin_harness.create_invite(token, max_uses=1, note="audit").json()["code"]
    admin_harness.client.delete(f"/admin/invites/{invite}", headers=headers)
    admin_harness.client.post(f"/admin/users/{user['id']}/disable", headers=headers)
    admin_harness.client.post(f"/admin/users/{user['id']}/enable", headers=headers)
    admin_harness.client.post(
        f"/admin/users/{user['id']}/reset-password", headers=headers, json={}
    )
    admin_harness.client.post(f"/admin/users/{user['id']}/revoke-tokens", headers=headers)
    admin_harness.client.post(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "passw0rd", "new_password": "newpass0rd"},
    )

    response = admin_harness.client.get("/admin/audit?limit=100", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    actions = {entry["action"] for entry in body["entries"]}
    expected = {
        "user.created",
        "invite.created",
        "invite.revoked",
        "user.disabled",
        "user.enabled",
        "user.password_reset",
        "user.tokens_revoked",
        "user.password_changed",
    }
    assert expected.issubset(actions), expected - actions
    for entry in body["entries"]:
        assert set(entry) == {
            "id", "actorId", "actorName", "action", "target", "detail", "ip", "createdAt"
        }
        assert entry["createdAt"] > 0
    password_entry = next(e for e in body["entries"] if e["action"] == "user.password_reset")
    assert "newpass0rd" not in json.dumps(password_entry)  # never log secrets


def test_audit_pagination_and_admin_only(admin_harness):
    headers = auth(admin_harness.admin_token)
    page = admin_harness.client.get("/admin/audit?limit=1&offset=0", headers=headers).json()
    assert len(page["entries"]) <= 1
    assert page["limit"] == 1
    assert page["offset"] == 0
    token, _ = second_user(admin_harness, "pleb")
    assert admin_harness.client.get("/admin/audit", headers=auth(token)).status_code == 403


def test_delete_user_is_audited(admin_harness):
    user = make_user(admin_harness, "gone")
    admin_harness.client.delete(
        f"/admin/users/{user['id']}", headers=auth(admin_harness.admin_token)
    )
    entries = admin_harness.client.get(
        "/admin/audit?limit=100", headers=auth(admin_harness.admin_token)
    ).json()["entries"]
    assert any(
        entry["action"] == "user.deleted" and entry["target"] == user["id"]
        for entry in entries
    )


def test_diagnostics(admin_harness, monkeypatch):
    install_fake_upstream_http(admin_harness, monkeypatch)
    response = admin_harness.client.get(
        "/admin/diagnostics", headers=auth(admin_harness.admin_token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"]
    assert body["uptimeSeconds"] >= 0
    assert body["database"]["users"] >= 1
    assert isinstance(body["storage"]["diskFiles"], int)
    assert body["upstreams"] and body["upstreams"][0]["name"] == "primary"
    assert body["upstreams"][0]["ok"] is True
    assert "ffmpeg" in body
    assert "limiters" in body
    assert "api_key" not in response.text.lower()
    token, _ = second_user(admin_harness, "pleb2")
    assert admin_harness.client.get(
        "/admin/diagnostics", headers=auth(token)
    ).status_code == 403


def test_diagnostics_reports_unreachable_upstream(admin_harness, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    admin_harness.app.state.upstream._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    body = admin_harness.client.get(
        "/admin/diagnostics", headers=auth(admin_harness.admin_token)
    ).json()
    assert body["upstreams"][0]["ok"] is False
    assert body["upstreams"][0]["error"]


def test_maintenance_cleanup_dry_run_then_delete(admin_harness):
    admin = admin_harness.admin_user
    files_dir = admin_harness.app.state.settings.files_dir
    user_dir = files_dir / admin["id"]
    user_dir.mkdir(parents=True, exist_ok=True)
    stray = user_dir / "stray-file.bin"
    stray.write_bytes(b"orphan" * 10)

    conn = sqlite3.connect(db_path(admin_harness))
    conn.execute(
        "INSERT INTO files (id, user_id, filename, mime_type, size_bytes, purpose, sha256,"
        " created_at, deleted) VALUES ('file_nodisk', ?, 'x', 'text/plain', 5, NULL, '', 1, 0)",
        (admin["id"],),
    )
    conn.execute(
        "INSERT INTO sessions (user_id, id, title, created_at, updated_at, data)"
        " VALUES ('u_missing', 'sess-orphan', 'orphan', 1, 2, '{}')"
    )
    conn.commit()
    conn.close()

    headers = auth(admin_harness.admin_token)
    dry = admin_harness.client.post("/admin/maintenance/cleanup", headers=headers).json()
    assert dry["dryRun"] is True
    assert dry["orphanDiskFileCount"] >= 1
    assert dry["rowsWithoutFileCount"] >= 1
    assert dry["sessionsWithoutOwnerCount"] >= 1
    assert stray.exists()
    conn = sqlite3.connect(db_path(admin_harness))
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE id = 'file_nodisk'"
    ).fetchone()[0] == 1
    conn.close()

    real = admin_harness.client.post(
        "/admin/maintenance/cleanup?dry_run=0", headers=headers
    ).json()
    assert real["dryRun"] is False
    assert not stray.exists()
    conn = sqlite3.connect(db_path(admin_harness))
    assert conn.execute(
        "SELECT COUNT(*) FROM files WHERE id = 'file_nodisk'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE id = 'sess-orphan'"
    ).fetchone()[0] == 0
    conn.close()

    entries = admin_harness.client.get("/admin/audit?limit=50", headers=headers).json()["entries"]
    assert any(entry["action"] == "maintenance.cleanup" for entry in entries)


def test_cleanup_admin_only(admin_harness):
    token, _ = second_user(admin_harness, "pleb3")
    assert admin_harness.client.post(
        "/admin/maintenance/cleanup", headers=auth(token)
    ).status_code == 403
