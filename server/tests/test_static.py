"""Static web client mounting, SPA fallback and docs toggle."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

from tests.conftest import build_settings


def test_docs_can_be_enabled(tmp_path):
    settings = build_settings(tmp_path, docs_enabled=True)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_landing_page_without_web_dir(tmp_path):
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Service is running." in response.text
        deep = client.get("/some/deep/link")
        assert deep.status_code == 404
        assert deep.json()["error"]["type"] == "not_found_error"


def test_web_client_is_served_with_spa_fallback(tmp_path):
    web = tmp_path / "web"
    (web / "static").mkdir(parents=True)
    (web / "index.html").write_text(
        "<!doctype html><title>app</title><div id=root></div>", encoding="utf-8"
    )
    (web / "static" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "<title>app</title>" in index.text

        asset = client.get("/static/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text
        assert "javascript" in asset.headers["content-type"]

        # SPA deep links fall back to index.html
        deep = client.get("/session/abc-123")
        assert deep.status_code == 200
        assert "<title>app</title>" in deep.text

        # API paths must still 404 as JSON, never the SPA shell
        api = client.get("/api/does-not-exist")
        assert api.status_code == 404
        assert api.headers["content-type"].startswith("application/json")
        assert "error" in api.json()

        anthropic = client.get("/anthropic/v1/unknown")
        assert anthropic.status_code == 404
        assert anthropic.headers["content-type"].startswith("application/json")


def test_path_traversal_is_not_served(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("INDEX", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    settings = build_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        for path in ("/../secret.txt", "/..%2Fsecret.txt", "/%2e%2e/secret.txt"):
            response = client.get(path)
            assert "TOP SECRET" not in response.text, path
