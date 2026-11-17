"""Per-user storage quota (USER_STORAGE_MB) and orphan cleanup behaviour."""

from __future__ import annotations

import pytest

from tests.test_isolation import auth, second_user


def payload(size: int) -> bytes:
    return b"q" * size


def set_quota(harness, mb: int) -> None:
    harness.app.state.settings.user_storage_mb = mb


def test_client_file_quota(admin_harness):
    set_quota(admin_harness, 1)
    headers = auth(admin_harness.admin_token)
    assert admin_harness.client.put(
        "/api/files/a", headers={**headers, "content-type": "application/octet-stream"},
        content=payload(600 * 1024),
    ).status_code == 200

    too_big = admin_harness.client.put(
        "/api/files/b", headers={**headers, "content-type": "application/octet-stream"},
        content=payload(600 * 1024),
    )
    assert too_big.status_code == 413
    assert "存储配额" in too_big.json()["error"]["message"]

    # Replacing an existing attachment with the same size is not growth.
    assert admin_harness.client.put(
        "/api/files/a", headers={**headers, "content-type": "application/octet-stream"},
        content=payload(600 * 1024),
    ).status_code == 200

    admin_harness.client.delete("/api/files/a", headers=headers)
    assert admin_harness.client.put(
        "/api/files/b", headers={**headers, "content-type": "application/octet-stream"},
        content=payload(600 * 1024),
    ).status_code == 200


def test_anthropic_file_upload_quota(admin_harness):
    set_quota(admin_harness, 1)
    token = admin_harness.admin_token
    first = admin_harness.client.post(
        "/anthropic/v1/files", headers={"x-api-key": token},
        files={"file": ("a.bin", payload(600 * 1024), "application/octet-stream")},
    )
    assert first.status_code == 200
    second = admin_harness.client.post(
        "/anthropic/v1/files", headers={"x-api-key": token},
        files={"file": ("b.bin", payload(600 * 1024), "application/octet-stream")},
    )
    assert second.status_code == 413
    assert "存储配额" in second.json()["error"]["message"]
    # No half-written file left behind
    user_dir = admin_harness.app.state.settings.files_dir / admin_harness.admin_user["id"]
    assert not list(user_dir.glob(".*.part"))


def test_quota_is_per_user(admin_harness):
    set_quota(admin_harness, 1)
    alice_token, _ = second_user(admin_harness, "alice")
    admin_harness.client.put(
        "/api/files/alice-file",
        headers={**auth(alice_token), "content-type": "application/octet-stream"},
        content=payload(900 * 1024),
    )
    # Admin still has the full 1 MiB available.
    response = admin_harness.client.put(
        "/api/files/admin-file",
        headers={**auth(admin_harness.admin_token), "content-type": "application/octet-stream"},
        content=payload(900 * 1024),
    )
    assert response.status_code == 200


def test_zero_quota_means_unlimited(admin_harness):
    set_quota(admin_harness, 0)
    response = admin_harness.client.put(
        "/api/files/unlimited",
        headers={**auth(admin_harness.admin_token), "content-type": "application/octet-stream"},
        content=payload(1024 * 1024),
    )
    assert response.status_code == 200


def test_stats_exposes_the_quota(admin_harness):
    set_quota(admin_harness, 7)
    data = admin_harness.client.get(
        "/api/me/stats", headers=auth(admin_harness.admin_token)
    ).json()
    assert data["storageLimitBytes"] == 7 * 1024 * 1024
