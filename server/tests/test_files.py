"""File store tests: Files API lifecycle, sanitisation, limits, isolation."""

from __future__ import annotations

import pytest

from app.files import sanitize_filename, sanitize_local_id


def auth(token: str) -> dict[str, str]:
    return {"x-api-key": token, "anthropic-beta": "files-api-2025-04-14"}


def test_upload_download_delete_lifecycle(admin_harness):
    token = admin_harness.admin_token
    upload = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(token),
        files={"file": ("报告 v1.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"purpose": "rich_layout_recognition"},
    )
    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["type"] == "file"
    assert body["id"].startswith("file_")
    assert body["size_bytes"] == len(b"%PDF-1.4 fake")
    assert body["filename"] == "报告 v1.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["created_at"].endswith("Z")

    fetched = admin_harness.client.get(
        f"/anthropic/v1/files/{body['id']}", headers=auth(token)
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == body["id"]

    content = admin_harness.client.get(
        f"/anthropic/v1/files/{body['id']}/content", headers=auth(token)
    )
    assert content.status_code == 200
    assert content.content == b"%PDF-1.4 fake"

    deleted = admin_harness.client.delete(
        f"/anthropic/v1/files/{body['id']}", headers=auth(token)
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"id": body["id"], "type": "file_deleted"}
    assert admin_harness.client.get(
        f"/anthropic/v1/files/{body['id']}", headers=auth(token)
    ).status_code == 404
    assert admin_harness.client.delete(
        f"/anthropic/v1/files/{body['id']}", headers=auth(token)
    ).status_code == 404


def test_upload_requires_file_field(admin_harness):
    response = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(admin_harness.admin_token),
        data={"purpose": "user_data"},
    )
    assert response.status_code == 400
    assert "file" in response.json()["error"]["message"]


def test_upload_size_limit(admin_harness):
    admin_harness.app.state.settings.max_upload_bytes = 1024
    response = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(admin_harness.admin_token),
        files={"file": ("big.bin", b"x" * 4096, "application/octet-stream")},
    )
    assert response.status_code == 413


def test_filename_sanitisation():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\temp\\evil.exe") == "evil.exe"
    assert sanitize_filename("with\x00null.txt") == "withnull.txt"
    assert sanitize_filename("   ") == "file"
    assert sanitize_filename(None) == "file"
    assert sanitize_filename("中文 文件名.docx") == "中文 文件名.docx"
    assert sanitize_filename("a:b*c?.txt") == "a_b_c_.txt"
    assert len(sanitize_filename("a" * 500)) <= 180


def test_local_id_sanitisation():
    assert sanitize_local_id("abc-123_DEF.txt") == "abc-123_DEF.txt"
    for bad in ("..", "a/b", "a b", "", "x" * 200):
        with pytest.raises(Exception):
            sanitize_local_id(bad)


def test_upload_stores_file_under_user_directory(admin_harness, settings):
    response = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(admin_harness.admin_token),
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    file_id = response.json()["id"]
    path = settings.files_dir / admin_harness.admin_user["id"] / file_id
    assert path.read_bytes() == b"hello"
    # no leftovers
    assert not list((settings.files_dir / admin_harness.admin_user["id"]).glob(".*.part"))


def test_cross_user_file_access_is_404(admin_harness):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    other = admin_harness.client.post(
        "/auth/register",
        json={"username": "other", "password": "passw0rd", "invite_code": invite},
    ).json()["token"]
    file_id = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(admin_harness.admin_token),
        files={"file": ("secret.txt", b"admin only", "text/plain")},
    ).json()["id"]
    assert admin_harness.client.get(
        f"/anthropic/v1/files/{file_id}", headers=auth(other)
    ).status_code == 404
    assert admin_harness.client.get(
        f"/anthropic/v1/files/{file_id}/content", headers=auth(other)
    ).status_code == 404
    assert admin_harness.client.delete(
        f"/anthropic/v1/files/{file_id}", headers=auth(other)
    ).status_code == 404


def test_upload_requires_auth(harness):
    response = harness.client.post(
        "/anthropic/v1/files",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401


def test_unicode_filename_roundtrip(admin_harness):
    response = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(admin_harness.admin_token),
        files={"file": ("时序图-草图 1.png", b"\x89PNG", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["filename"] == "时序图-草图 1.png"


def test_api_file_traversal_via_client_id(admin_harness):
    headers = auth(admin_harness.admin_token)
    response = admin_harness.client.put("/api/files/..%2Fescape", headers=headers, content=b"x")
    assert response.status_code in (400, 404, 405)


def test_files_deleted_on_user_deletion(admin_harness, settings):
    invite = admin_harness.create_invite(admin_harness.admin_token, max_uses=1).json()["code"]
    registered = admin_harness.client.post(
        "/auth/register",
        json={"username": "gone", "password": "passw0rd", "invite_code": invite},
    ).json()
    token, user = registered["token"], registered["user"]
    file_id = admin_harness.client.post(
        "/anthropic/v1/files",
        headers=auth(token),
        files={"file": ("x.txt", b"bye", "text/plain")},
    ).json()["id"]
    admin_harness.client.delete(
        f"/admin/users/{user['id']}",
        headers={"Authorization": f"Bearer {admin_harness.admin_token}"},
    )
    assert not (settings.files_dir / user["id"] / file_id).exists()
