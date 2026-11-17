"""Regression tests for the five security-audit findings.

1. Admin page must not compile data into inline JS handlers (stored XSS).
2. A user-chosen search base_url must never receive the server's search key.
3. Uploaded script-capable files must be served as sandboxed attachments.
4. username:password credentials share the login rate limiter.
5. First-registration bootstrap is serialised by a real transaction.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app import admin as admin_module
from app.config import SearchConfig
from app.db import Database
from app.main import create_app
from app.search import SearchService

from tests.conftest import build_settings


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. admin page: no data-driven inline handlers
# ---------------------------------------------------------------------------


def test_admin_page_has_no_data_driven_inline_handlers():
    html = admin_module.ADMIN_PAGE_HTML
    inline = re.findall(r"on\w+\s*=\s*\"([^\"]*)\"", html)
    assert inline, "expected some static inline handlers (login buttons etc.)"
    for value in inline:
        assert "${" not in value, f"data compiled into an inline handler: {value[:120]}"
    # Row actions are declarative buttons + one delegated listener.
    for action in (
        "reset-password",
        "specify-password",
        "toggle-disabled",
        "revoke-tokens",
        "delete-user",
        "copy-invite",
        "revoke-invite",
    ):
        assert f'data-action="{action}"' in html, action
    assert "addEventListener('click'" in html
    assert "${esc(u.username)}" in html  # escaped into a data attribute, not JS
    assert "onclick=\"resetPassword(" not in html


def test_admin_page_escapes_quotes_in_attribute_values():
    html = admin_module.ADMIN_PAGE_HTML
    # The esc() helper must encode single and double quotes so that a username
    # like  x','alert(1)','y  cannot terminate a data-name attribute value.
    assert "&#39;" in html and "&quot;" in html
    script = html[html.index("<script>") : html.index("</script>")]
    assert "dataset.action" in script
    assert "dataset.name" in script


def test_hostile_username_stays_out_of_js_source():
    """Simulate row rendering: data attributes only, no onclick is produced."""
    html = admin_module.ADMIN_PAGE_HTML
    # No JS template compiles a value into an event handler at all.
    assert not re.search(r"on\w+=\"[^\"]*\$\{", html)
    users_block = html.split("const rows = data.users")[1].split("catch (err)")[0]
    assert "onclick" not in users_block
    invites_block = html.split("const rows = data.invites")[1].split("catch (err)")[0]
    assert "onclick" not in invites_block


# ---------------------------------------------------------------------------
# 2. search key must not leak to a user-chosen host
# ---------------------------------------------------------------------------


def search_settings(tmp_path) -> "object":
    settings = build_settings(tmp_path)
    settings.search = SearchConfig(
        mode="dgx",
        provider="searxng",
        provider_base_url="https://search.internal.example",
        provider_api_key="server-search-secret",
        official_api_key="",
    )
    return settings


def test_custom_search_base_url_does_not_inherit_server_key(tmp_path):
    service = SearchService(search_settings(tmp_path), upstream=object())
    mode, provider, config = service._config(  # noqa: SLF001 - unit under test
        {"provider": "searxng", "base_url": "https://attacker.example"}
    )
    assert provider == "searxng"
    assert config.provider_base_url == "https://attacker.example"
    assert config.provider_api_key == ""

    _, _, with_key = service._config(  # noqa: SLF001
        {"provider": "searxng", "base_url": "https://attacker.example", "api_key": "user-key"}
    )
    assert with_key.provider_api_key == "user-key"

    _, _, untouched = service._config({"max_results": 3})  # noqa: SLF001
    assert untouched.provider_base_url == "https://search.internal.example"
    assert untouched.provider_api_key == "server-search-secret"


@pytest.mark.asyncio
async def test_plan_does_not_send_server_key_to_user_host(tmp_path):
    settings = search_settings(tmp_path)
    service = SearchService(settings, upstream=object())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        plan = await service.plan(
            query="hello",
            urls=[],
            want_search=True,
            want_read_url=False,
            user_settings={"provider": "searxng", "base_url": "https://attacker.example"},
        )
    finally:
        await service.client.aclose()
    assert requests, "searxng should still be callable without a key"
    request = requests[0]
    assert request.url.host == "attacker.example"
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert "authorization" not in headers
    assert "x-subscription-token" not in headers
    assert "server-search-secret" not in str(request.url)
    assert plan.notice in ("searxng", "no_results")


@pytest.mark.asyncio
async def test_plan_still_uses_server_key_for_server_configured_host(tmp_path):
    settings = search_settings(tmp_path)
    settings.search.provider = "brave"
    settings.search.provider_base_url = "https://api.search.brave.com/res/v1/web/search"
    service = SearchService(settings, upstream=object())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"web": {"results": []}})

    service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await service.plan(query="q", urls=[], want_search=True, want_read_url=False)
    finally:
        await service.client.aclose()
    assert requests[0].headers["X-Subscription-Token"] == "server-search-secret"


# ---------------------------------------------------------------------------
# 3. uploaded script-capable files are sandboxed attachments
# ---------------------------------------------------------------------------


def upload(harness, token: str, name: str, data: bytes, mime: str) -> dict:
    response = harness.client.post(
        "/anthropic/v1/files",
        headers={"x-api-key": token},
        files={"file": (name, data, mime)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_html_upload_is_served_as_sandboxed_attachment(admin_harness):
    token = admin_harness.admin_token
    payload = b"<html><script>alert(document.domain)</script></html>"
    obj = upload(admin_harness, token, "evil.html", payload, "text/html")
    assert obj["mime_type"] == "text/html"
    response = admin_harness.client.get(
        f"/anthropic/v1/files/{obj['id']}/content", headers=auth(token)
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox"


def test_svg_upload_is_served_as_sandboxed_attachment(admin_harness):
    token = admin_harness.admin_token
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    obj = upload(admin_harness, token, "evil.svg", svg, "image/svg+xml")
    assert obj["mime_type"] == "image/svg+xml"
    response = admin_harness.client.get(
        f"/anthropic/v1/files/{obj['id']}/content", headers=auth(token)
    )
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_png_upload_stays_inline_with_nosniff(admin_harness):
    token = admin_harness.admin_token
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    obj = upload(admin_harness, token, "ok.png", png, "image/png")
    assert obj["mime_type"] == "image/png"
    response = admin_harness.client.get(
        f"/anthropic/v1/files/{obj['id']}/content", headers=auth(token)
    )
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers


def test_declared_mime_is_sanitised(admin_harness):
    token = admin_harness.admin_token
    weird = upload(
        admin_harness, token, "payload.weird", b"data", "application/x-msdownload"
    )
    assert weird["mime_type"] == "application/octet-stream"

    text_fallback = upload(admin_harness, token, "note.txt", b"hello", "application/x-msdownload")
    assert text_fallback["mime_type"] == "text/plain"


def test_client_attachment_html_is_sandboxed(admin_harness):
    token = admin_harness.admin_token
    put = admin_harness.client.put(
        "/api/files/local-html",
        headers={**auth(token), "content-type": "text/html"},
        content=b"<script>alert(1)</script>",
    )
    assert put.status_code == 200
    assert put.json()["contentType"] == "text/html"
    response = admin_harness.client.get("/api/files/local-html", headers=auth(token))
    assert response.headers["content-disposition"].startswith("attachment")
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_application_xhtml_is_attachment(admin_harness):
    token = admin_harness.admin_token
    obj = upload(
        admin_harness, token, "page.xhtml", b"<html xmlns='http://www.w3.org/1999/xhtml'/>",
        "application/xhtml+xml",
    )
    response = admin_harness.client.get(
        f"/anthropic/v1/files/{obj['id']}/content", headers=auth(token)
    )
    assert response.headers["content-disposition"].startswith("attachment")


# ---------------------------------------------------------------------------
# 4. username:password shares the login limiter
# ---------------------------------------------------------------------------


def bad_basic(username: str, password: str = "wrong") -> dict[str, str]:
    return {"Authorization": f"Bearer {username}:{password}"}


def test_credentials_header_is_rate_limited_like_login(admin_harness):
    client = admin_harness.client
    statuses = [client.get("/models", headers=bad_basic("admin1")).status_code for _ in range(10)]
    assert statuses.count(401) == 10
    blocked = client.get("/models", headers=bad_basic("admin1"))
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    # Even the correct password is blocked while the window is hot.
    assert client.get(
        "/models", headers=bad_basic("admin1", "passw0rd")
    ).status_code == 429
    # The proper login endpoint shares the same counters.
    assert admin_harness.login("admin1", "passw0rd").status_code == 429


def test_credentials_header_rate_limit_is_per_ip(admin_harness):
    """Changing the username does not reset the per-IP counter."""
    client = admin_harness.client
    for index in range(10):
        client.get("/models", headers=bad_basic(f"ghost{index}"))
    assert client.get("/models", headers=bad_basic("another-ghost")).status_code == 429


def test_credentials_auth_can_be_disabled(tmp_path):
    settings = build_settings(tmp_path, registration_mode="open")
    settings.allow_credentials_auth = False
    app = create_app(settings)
    with TestClient(app) as client:
        registered = client.post(
            "/auth/register", json={"username": "user1", "password": "passw0rd"}
        ).json()
        assert client.get(
            "/models", headers={"Authorization": "Bearer user1:passw0rd"}
        ).status_code == 401
        # Tokens still work, and /auth/login still works.
        assert client.get(
            "/models", headers={"Authorization": f"Bearer {registered['token']}"}
        ).status_code == 200
        assert client.post(
            "/auth/login", json={"username": "user1", "password": "passw0rd"}
        ).status_code == 200


def test_successful_credentials_auth_does_not_exhaust_the_limiter(admin_harness):
    client = admin_harness.client
    # A few failures, then a success, then a few more failures stay below the
    # per-(IP+username) limit because success clears that counter.
    for _ in range(4):
        client.get("/models", headers=bad_basic("admin1"))
    assert client.get(
        "/models", headers=bad_basic("admin1", "passw0rd")
    ).status_code == 200
    for _ in range(4):
        client.get("/models", headers=bad_basic("admin1"))
    assert client.get(
        "/models", headers=bad_basic("admin1", "passw0rd")
    ).status_code == 200


# ---------------------------------------------------------------------------
# 5. bootstrap transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_commits_and_rolls_back(tmp_path):
    db = Database(tmp_path / "tx.db")
    await db.connect()
    try:
        await db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await db.execute("INSERT INTO t (v) VALUES ('rolled-back')", commit=False)
                raise RuntimeError("boom")
        assert await db.fetch_value("SELECT COUNT(*) FROM t", (), 0) == 0

        async with db.transaction():
            await db.execute("INSERT INTO t (v) VALUES ('committed')", commit=False)
        assert await db.fetch_value("SELECT COUNT(*) FROM t", (), 0) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_registration_creates_exactly_one_admin(tmp_path):
    settings = build_settings(tmp_path, registration_mode="open")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                client.post(
                    "/auth/register", json={"username": "racer-a", "password": "passw0rd"}
                ),
                client.post(
                    "/auth/register", json={"username": "racer-b", "password": "passw0rd"}
                ),
            )
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    roles = [response.json()["user"]["role"] for response in responses]
    assert sorted(roles) == ["admin", "user"], roles

    conn = sqlite3.connect(str(settings.db_path))
    admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert users == 2
    assert admins == 1
