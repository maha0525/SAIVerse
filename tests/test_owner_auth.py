from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.owner_auth import BrowserOriginMiddleware, OwnerAuthMiddleware, router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")

    @app.get("/api/protected")
    def protected_get():
        return {"ok": True}

    @app.post("/api/protected")
    def protected_post():
        return {"ok": True}

    @app.get("/api/oauth/callback/addon/flow")
    def oauth_callback():
        return {"callback": True}

    app.add_middleware(OwnerAuthMiddleware)
    return app


def test_owner_auth_rejects_unauthenticated_request(monkeypatch) -> None:
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    client = TestClient(_app())

    response = client.get("/api/protected")

    assert response.status_code == 401


def test_bearer_auth_allows_mutation_without_cookie_csrf(monkeypatch) -> None:
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    client = TestClient(_app())

    response = client.post(
        "/api/protected",
        headers={"Authorization": "Bearer secret-token"},
    )

    assert response.status_code == 200


def test_login_sets_http_only_session_cookie(monkeypatch) -> None:
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    monkeypatch.setenv("SAIVERSE_ALLOWED_ORIGINS", "http://city.local:3000")
    client = TestClient(_app(), follow_redirects=False)

    login = client.post(
        "/api/auth/login",
        content="token=secret-token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert login.status_code == 303
    assert "HttpOnly" in login.headers["set-cookie"]
    assert client.get("/api/protected").status_code == 200
    assert client.post("/api/protected").status_code == 403
    assert client.post(
        "/api/protected",
        headers={"Origin": "http://city.local:3000"},
    ).status_code == 200


def test_oauth_callback_remains_available_for_state_validation(monkeypatch) -> None:
    monkeypatch.setenv("SAIVERSE_OWNER_TOKEN", "secret-token")
    client = TestClient(_app())

    response = client.get("/api/oauth/callback/addon/flow")

    assert response.status_code == 200


def _local_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/items")
    def read_items():
        return {"ok": True}

    @app.post("/api/items")
    def mutate_items():
        return {"ok": True}

    @app.delete("/api/items/1")
    def delete_item():
        return {"ok": True}

    app.add_middleware(BrowserOriginMiddleware)
    return app


def test_browser_origin_allows_local_scripts_without_browser_headers() -> None:
    client = TestClient(_local_app())

    assert client.post("/api/items").status_code == 200
    assert client.delete("/api/items/1").status_code == 200


def test_browser_origin_allows_configured_and_same_host_origins(monkeypatch) -> None:
    monkeypatch.setenv("SAIVERSE_ALLOWED_ORIGINS", "https://my-box.tailnet.ts.net:3000")
    client = TestClient(_local_app())

    assert client.post("/api/items", headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert client.post("/api/items", headers={"Origin": "http://127.0.0.1:3000"}).status_code == 200
    assert client.post(
        "/api/items",
        headers={"Origin": "https://my-box.tailnet.ts.net:3000"},
    ).status_code == 200
    assert client.post(
        "/api/items",
        headers={"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"},
    ).status_code == 200


def test_browser_origin_rejects_untrusted_origin_sec_fetch_or_referer() -> None:
    client = TestClient(_local_app())

    # Untrusted Origin / null Origin / empty Origin
    assert client.post("/api/items", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.delete("/api/items/1", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/items", headers={"Origin": ""}).status_code == 403

    # Cross-site Sec-Fetch-Site without Origin
    assert client.post("/api/items", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403

    # Untrusted Referer without Origin, while allowed Referer passes
    assert client.post(
        "/api/items",
        headers={"Referer": "https://evil.example/attack.html"},
    ).status_code == 403
    assert client.post(
        "/api/items",
        headers={"Referer": "http://localhost:3000/chat"},
    ).status_code == 200

    # Safe GET requests remain unaffected
    assert client.get("/api/items", headers={"Origin": "https://evil.example"}).status_code == 200
