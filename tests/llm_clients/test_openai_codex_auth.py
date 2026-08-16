"""Codex サブスク認証のテスト。

対象: llm_clients/openai_codex_auth.py (ストア解決・デバイスコードフロー) と
llm_clients/openai_codex.py の refresh 書き戻し先の所有権。
ネットワークには一切出ない (HTTP は全て偽物に差し替える)。

Intent doc: docs/intent/codex_subscription_auth.md §7
"""
from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_clients import openai_codex_auth as oca
from llm_clients.openai_codex import OpenAICodexClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _jwt(payload: dict) -> str:
    """Unsigned JWT — decode_jwt_payload only reads the payload segment."""

    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _access_token(account_id: str = "acct-123", expires_in_seconds: int = 3600) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    return _jwt(
        {
            "exp": int(exp.timestamp()),
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        }
    )


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
    """SAIVerse ストアと Codex CLI ストアを両方 tmp に隔離する。"""
    saiverse_dir = tmp_path / "user_data"
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("SAIVERSE_USER_DATA_DIR", str(saiverse_dir))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return {
        "saiverse": saiverse_dir / oca.SAIVERSE_STORE_FILENAME,
        "cli": codex_home / "auth.json",
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeSession:
    """_http_session() の差し替え先。応答キューを順に返し、呼び出しを記録する。"""

    def __init__(self, queue: list):
        self._queue = queue
        self.calls: list = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self._queue:
            raise AssertionError(f"unexpected extra HTTP call to {url}")
        return self._queue.pop(0)

    def close(self):
        pass


@pytest.fixture
def fake_http(monkeypatch):
    """openai_codex_auth の HTTP を応答キュー式の偽セッションへ差し替える。"""
    queue: list = []
    session = FakeSession(queue)
    monkeypatch.setattr(oca, "_http_session", lambda: session)
    return {"queue": queue, "session": session}


# ---------------------------------------------------------------------------
# store resolution
# ---------------------------------------------------------------------------

def test_resolve_none_when_no_store_exists(isolated_stores):
    assert oca.resolve_active_store() is None


def test_resolve_falls_back_to_cli_store(isolated_stores):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "a"}})
    store = oca.resolve_active_store()
    assert store is not None
    assert store.kind == oca.STORE_KIND_CODEX_CLI
    assert store.path == isolated_stores["cli"]


def test_resolve_prefers_saiverse_store(isolated_stores):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "sv"}})
    store = oca.resolve_active_store()
    assert store.kind == oca.STORE_KIND_SAIVERSE
    assert store.path == isolated_stores["saiverse"]


def test_saiverse_store_wins_even_if_broken(isolated_stores):
    """自前ストアが壊れていても CLI へ黙って乗り換えない (intent §3-2)。"""
    isolated_stores["saiverse"].parent.mkdir(parents=True, exist_ok=True)
    isolated_stores["saiverse"].write_text("not json", encoding="utf-8")
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    store = oca.resolve_active_store()
    assert store.kind == oca.STORE_KIND_SAIVERSE
    with pytest.raises(Exception):
        oca.read_auth_store(store.path)


def test_logout_deletes_only_saiverse_store(isolated_stores):
    _write_json(isolated_stores["saiverse"], {"tokens": {}})
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli"}})
    assert oca.delete_saiverse_store() is True
    assert not isolated_stores["saiverse"].exists()
    assert isolated_stores["cli"].exists()
    # 2 回目は消すものがない
    assert oca.delete_saiverse_store() is False


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def test_extract_account_id_from_access_token():
    token = _access_token(account_id="acct-xyz")
    assert oca.extract_account_id(token) == "acct-xyz"


def test_extract_account_id_falls_back_to_id_token():
    id_token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-id"}})
    assert oca.extract_account_id("garbage", id_token) == "acct-id"


def test_access_token_expiry_reads_exp():
    token = _access_token(expires_in_seconds=1000)
    expiry = oca.access_token_expiry(token)
    assert expiry is not None
    remaining = (expiry - datetime.now(tz=timezone.utc)).total_seconds()
    assert 900 < remaining < 1100


# ---------------------------------------------------------------------------
# device-code flow steps
# ---------------------------------------------------------------------------

def test_request_usercode_success(fake_http):
    fake_http["queue"].append(
        FakeResponse(200, {"user_code": "ABCD-1234", "device_auth_id": "dev-1", "interval": "5"})
    )
    manager = oca.CodexDeviceLoginManager()
    device = manager._request_usercode()
    assert device["user_code"] == "ABCD-1234"
    assert device["device_auth_id"] == "dev-1"
    assert device["interval"] == 5
    url, kwargs = fake_http["session"].calls[0]
    assert url == oca.DEVICE_USERCODE_URL
    assert kwargs["json"] == {"client_id": oca.CODEX_OAUTH_CLIENT_ID}


def test_request_usercode_rate_limited(fake_http):
    fake_http["queue"].append(FakeResponse(429))
    manager = oca.CodexDeviceLoginManager()
    with pytest.raises(oca.CodexDeviceLoginError, match="429"):
        manager._request_usercode()


def test_poll_pending_then_success(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake_http["queue"].extend(
        [
            FakeResponse(403),
            FakeResponse(404),
            FakeResponse(200, {"authorization_code": "code-1", "code_verifier": "ver-1"}),
        ]
    )
    manager = oca.CodexDeviceLoginManager()
    result = manager._poll_for_authorization("dev-1", "ABCD-1234", interval=0)
    assert result == {"authorization_code": "code-1", "code_verifier": "ver-1"}


def test_poll_unexpected_status_raises(fake_http, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake_http["queue"].append(FakeResponse(500))
    manager = oca.CodexDeviceLoginManager()
    with pytest.raises(oca.CodexDeviceLoginError, match="500"):
        manager._poll_for_authorization("dev-1", "ABCD-1234", interval=0)


def test_exchange_code_posts_authorization_grant(fake_http):
    fake_http["queue"].append(
        FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "id_token": "it"})
    )
    manager = oca.CodexDeviceLoginManager()
    tokens = manager._exchange_code({"authorization_code": "code-1", "code_verifier": "ver-1"})
    assert tokens["access_token"] == "at"
    url, kwargs = fake_http["session"].calls[0]
    assert url == oca.CODEX_REFRESH_TOKEN_URL
    body = kwargs["data"]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code-1"
    assert body["code_verifier"] == "ver-1"
    assert body["redirect_uri"] == oca.DEVICE_REDIRECT_URI


def test_persist_login_writes_saiverse_store(isolated_stores):
    manager = oca.CodexDeviceLoginManager()
    access = _access_token(account_id="acct-777")
    manager._persist_login({"access_token": access, "refresh_token": "rt", "id_token": "it"})
    data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert data["tokens"]["access_token"] == access
    assert data["tokens"]["refresh_token"] == "rt"
    assert data["tokens"]["account_id"] == "acct-777"
    assert data["source"] == "saiverse_device_login"


def test_full_login_flow_end_to_end(isolated_stores, fake_http, monkeypatch):
    """start() → 背景スレッド → 成功状態 + ストア書き込み、の一気通貫。"""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    access = _access_token(account_id="acct-e2e")
    fake_http["queue"].extend(
        [
            FakeResponse(200, {"user_code": "WXYZ-9876", "device_auth_id": "dev-9", "interval": 0}),
            FakeResponse(403),
            FakeResponse(200, {"authorization_code": "code-9", "code_verifier": "ver-9"}),
            FakeResponse(200, {"access_token": access, "refresh_token": "rt-9"}),
        ]
    )
    manager = oca.CodexDeviceLoginManager()
    started = manager.start()
    assert started["state"] == oca.LOGIN_STATE_WAITING
    assert started["user_code"] == "WXYZ-9876"
    assert started["verification_url"] == oca.DEVICE_VERIFICATION_URL

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if manager.status()["state"] in (oca.LOGIN_STATE_SUCCESS, oca.LOGIN_STATE_ERROR):
            break
        threading.Event().wait(0.02)
    status = manager.status()
    assert status["state"] == oca.LOGIN_STATE_SUCCESS
    assert status["account_id"] == "acct-e2e"
    assert isolated_stores["saiverse"].exists()
    # トークンは status に出ない
    assert "access_token" not in json.dumps(status)


def test_status_payload_never_contains_tokens(isolated_stores):
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": _access_token(), "refresh_token": "secret-rt"}},
    )
    status = oca.auth_status()
    dumped = json.dumps(status)
    assert "secret-rt" not in dumped
    assert status["logged_in"] is True
    assert status["store"] == oca.STORE_KIND_SAIVERSE


# ---------------------------------------------------------------------------
# refresh write-back ownership (openai_codex.py 側)
# ---------------------------------------------------------------------------

def _client() -> OpenAICodexClient:
    return OpenAICodexClient(model="codex-test-model")


def test_refresh_writes_back_to_saiverse_store(isolated_stores, monkeypatch):
    old_access = _access_token(expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}},
    )
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": "cli-access", "refresh_token": "cli-rt"}},
    )
    client = _client()
    new_access = _access_token(expires_in_seconds=3600)
    monkeypatch.setattr(
        client,
        "_request_refresh",
        lambda rt: {"access_token": new_access, "refresh_token": "rt-new"},
    )
    client._refresh_or_pickup_latest(prior_access_token=old_access)

    saiverse_data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert saiverse_data["tokens"]["access_token"] == new_access
    assert saiverse_data["tokens"]["refresh_token"] == "rt-new"
    # CLI ストアは無傷 (書き戻し先の所有権、intent §3-1)
    cli_data = json.loads(isolated_stores["cli"].read_text(encoding="utf-8"))
    assert cli_data["tokens"]["access_token"] == "cli-access"
    assert cli_data["tokens"]["refresh_token"] == "cli-rt"


def test_refresh_writes_back_to_cli_store_when_piggybacking(isolated_stores, monkeypatch):
    old_access = _access_token(expires_in_seconds=-100)
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}},
    )
    client = _client()
    new_access = _access_token(expires_in_seconds=3600)
    monkeypatch.setattr(
        client,
        "_request_refresh",
        lambda rt: {"access_token": new_access, "refresh_token": "rt-new"},
    )
    client._refresh_or_pickup_latest(prior_access_token=old_access)
    cli_data = json.loads(isolated_stores["cli"].read_text(encoding="utf-8"))
    assert cli_data["tokens"]["access_token"] == new_access
    assert not isolated_stores["saiverse"].exists()


def test_refresh_picks_up_fresh_token_without_spending_refresh_token(
    isolated_stores, monkeypatch
):
    """他プロセスが更新済みなら refresh_token を使わない (既存挙動の維持)。"""
    fresh_access = _access_token(expires_in_seconds=3600)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": fresh_access, "refresh_token": "rt-keep"}},
    )
    client = _client()

    def _fail(_rt):
        raise AssertionError("refresh_token must not be spent when a fresh token exists")

    monkeypatch.setattr(client, "_request_refresh", _fail)
    client._refresh_or_pickup_latest(prior_access_token="stale-different-token")
    data = json.loads(isolated_stores["saiverse"].read_text(encoding="utf-8"))
    assert data["tokens"]["refresh_token"] == "rt-keep"


def test_load_auth_error_mentions_both_login_paths(isolated_stores):
    client = _client()
    with pytest.raises(RuntimeError) as exc_info:
        client._load_auth()
    message = str(exc_info.value)
    assert "ログイン" in message
    assert "codex login" in message


# ---------------------------------------------------------------------------
# API routes (最小アプリに載せて叩く)
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import codex_auth as codex_auth_routes

    app = FastAPI()
    app.include_router(codex_auth_routes.router, prefix="/codex-auth")
    return TestClient(app), codex_auth_routes


def test_route_status_and_logout(api_client, isolated_stores):
    client, _routes = api_client
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": _access_token(), "refresh_token": "rt"}},
    )
    res = client.get("/codex-auth/status")
    assert res.status_code == 200
    assert res.json()["store"] == "saiverse"

    res = client.post("/codex-auth/logout")
    assert res.status_code == 200
    body = res.json()
    assert body["removed"] is True
    assert body["logged_in"] is False
    assert not isolated_stores["saiverse"].exists()


def test_route_login_start_maps_flow_error_to_502(api_client, monkeypatch):
    client, routes = api_client

    def _boom():
        raise oca.CodexDeviceLoginError("throttled")

    monkeypatch.setattr(routes.LOGIN_MANAGER, "start", _boom)
    res = client.post("/codex-auth/login/start")
    assert res.status_code == 502
    assert "throttled" in res.json()["detail"]


def test_route_login_status_reflects_manager(api_client, monkeypatch):
    client, routes = api_client
    monkeypatch.setattr(
        routes.LOGIN_MANAGER,
        "status",
        lambda: {"state": "waiting", "user_code": "AAAA-0000"},
    )
    res = client.get("/codex-auth/login/status")
    assert res.status_code == 200
    assert res.json()["user_code"] == "AAAA-0000"
