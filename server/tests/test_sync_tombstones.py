"""Session deletion tombstones and the delta-sync endpoint."""

from __future__ import annotations

import sqlite3
import time

from tests.test_isolation import auth, make_session, second_user


def test_delete_writes_a_tombstone_and_sync_reports_it(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    admin_harness.client.put(
        "/api/sessions/sync-a", headers=headers, json=make_session("sync-a", "A", 1000)
    )
    deleted = admin_harness.client.delete("/api/sessions/sync-a", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["deletedAt"] > 0

    response = admin_harness.client.get("/api/sessions/sync?since=0", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"sessions", "deleted", "serverTime", "since"}
    assert "sync-a" in body["deleted"]
    assert body["serverTime"] > 0
    assert isinstance(body["sessions"], list)


def test_existing_sessions_endpoint_shape_is_unchanged(admin_harness):
    headers = auth(admin_harness.admin_token)
    admin_harness.client.put(
        "/api/sessions/shape", headers=headers, json=make_session("shape", "Shape", 1234)
    )
    listing = admin_harness.client.get("/api/sessions", headers=headers).json()
    assert isinstance(listing, list)
    assert listing == [
        {"id": "shape", "title": "Shape", "createdAt": 1134, "updatedAt": 1234}
    ]


def test_sync_since_filters_sessions_and_deletions(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    stamp = int(time.time() * 1000)

    admin_harness.client.put(
        "/api/sessions/older", headers=headers, json=make_session("older", "old", stamp - 10_000)
    )
    after_all = admin_harness.client.get(
        f"/api/sessions/sync?since={stamp}", headers=headers
    ).json()
    assert after_all["sessions"] == []
    assert after_all["deleted"] == []

    admin_harness.client.put(
        "/api/sessions/newer", headers=headers, json=make_session("newer", "new", stamp + 5_000)
    )
    delta = admin_harness.client.get(f"/api/sessions/sync?since={stamp}", headers=headers).json()
    assert [item["id"] for item in delta["sessions"]] == ["newer"]


def test_tombstones_are_per_user(admin_harness):
    alice_token, _ = second_user(admin_harness, "alice")
    bob_token, _ = second_user(admin_harness, "bob")
    admin_harness.client.put(
        "/api/sessions/shared", headers=auth(alice_token),
        json=make_session("shared", "alice", 1000),
    )
    admin_harness.client.put(
        "/api/sessions/shared", headers=auth(bob_token),
        json=make_session("shared", "bob", 1000),
    )
    admin_harness.client.delete("/api/sessions/shared", headers=auth(alice_token))
    bob_sync = admin_harness.client.get("/api/sessions/sync?since=0", headers=auth(bob_token)).json()
    assert bob_sync["deleted"] == []
    assert [item["id"] for item in bob_sync["sessions"]] == ["shared"]
    alice_sync = admin_harness.client.get("/api/sessions/sync?since=0", headers=auth(alice_token)).json()
    assert alice_sync["deleted"] == ["shared"]
    assert alice_sync["sessions"] == []


def test_tombstones_older_than_90_days_are_purged(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    old = int(time.time() * 1000) - 91 * 24 * 3600 * 1000
    conn = sqlite3.connect(str(admin_harness.app.state.settings.db_path))
    conn.execute(
        "INSERT INTO deleted_sessions (user_id, session_id, deleted_at) VALUES (?, ?, ?)",
        (admin_harness.admin_user["id"], "ancient", old),
    )
    conn.commit()
    conn.close()

    response = admin_harness.client.get("/api/sessions/sync?since=0", headers=headers)
    assert response.status_code == 200
    assert "ancient" not in response.json()["deleted"]
    conn = sqlite3.connect(str(admin_harness.app.state.settings.db_path))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM deleted_sessions WHERE session_id = 'ancient'"
    ).fetchone()[0]
    conn.close()
    assert remaining == 0


def test_delete_is_idempotent_and_still_tombstones(admin_harness):
    token = admin_harness.admin_token
    headers = auth(token)
    first = admin_harness.client.delete("/api/sessions/ghost", headers=headers)
    assert first.status_code == 200
    assert first.json()["deleted"] is False
    assert "ghost" in admin_harness.client.get(
        "/api/sessions/sync?since=0", headers=headers
    ).json()["deleted"]
