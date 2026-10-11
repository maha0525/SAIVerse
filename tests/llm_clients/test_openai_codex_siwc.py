"""Sign in with ChatGPT (SIWC / Token Sharing) official integration tests.

Covers:
  - is_siwc_token detection logic (explicit scope, client_id pattern, JWT claims)
  - Endpoint and header resolution (official Responses API vs legacy backend)
  - SIWC token refresh via official form-encoded OAuth endpoint
  - SiwcOAuthManager state machine, cancel, and token persistence
  - fetch_available_models catalog discovery
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from llm_clients import openai_codex_auth as oca
from llm_clients.openai_codex import OpenAICodexClient


def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _siwc_access_token(
    client_id: str = "oaiapp_test123",
    scopes: list[str] = ("chatgpt.tokens.use.direct", "openid"),
    expires_in_seconds: int = 3600,
) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    return _jwt(
        {
            "exp": int(exp.timestamp()),
            "client_id": client_id,
            "scp": list(scopes),
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-siwc-123"},
        }
    )


def _legacy_access_token(expires_in_seconds: int = 3600) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    return _jwt(
        {
            "exp": int(exp.timestamp()),
            "client_id": oca.CODEX_OAUTH_CLIENT_ID,
            "scp": ["openid", "profile", "email"],
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-legacy-123"},
        }
    )


@pytest.fixture
def isolated_stores(tmp_path, monkeypatch):
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


# ---------------------------------------------------------------------------
# is_siwc_token tests
# ---------------------------------------------------------------------------

def test_is_siwc_token_via_explicit_scope():
    tokens = {
        "access_token": "dummy",
        "scope": "openid email chatgpt.tokens.use.direct",
    }
    assert oca.is_siwc_token(tokens) is True


def test_is_siwc_token_via_client_id_prefix():
    tokens = {
        "access_token": "dummy",
        "client_id": "oaiapp_ABC123xyz",
    }
    assert oca.is_siwc_token(tokens) is True


def test_is_siwc_token_via_jwt_claims():
    tok = _siwc_access_token()
    tokens = {"access_token": tok}
    assert oca.is_siwc_token(tokens) is True


def test_is_siwc_token_legacy_token_returns_false():
    tok = _legacy_access_token()
    tokens = {
        "access_token": tok,
        "scope": "openid profile email",
        "client_id": oca.CODEX_OAUTH_CLIENT_ID,
    }
    assert oca.is_siwc_token(tokens) is False
    assert oca.is_siwc_token({}) is False
    assert oca.is_siwc_token(None) is False


# ---------------------------------------------------------------------------
# Endpoint & Header Resolution tests
# ---------------------------------------------------------------------------

def test_resolve_connection_params_siwc_mode(isolated_stores):
    token = _siwc_access_token()
    _write_json(
        isolated_stores["saiverse"],
        {
            "tokens": {
                "access_token": token,
                "refresh_token": "rt-siwc",
                "client_id": "oaiapp_xyz",
                "scope": "chatgpt.tokens.use.direct",
            }
        },
    )
    client = OpenAICodexClient(model="gpt-6.1-sol")
    url, headers, is_siwc = client._resolve_connection_params()

    assert is_siwc is True
    assert url == oca.SIWC_RESPONSES_URL
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Content-Type"] == "application/json"
    # Official endpoint does NOT include legacy/spoofed headers
    assert "originator" not in headers
    assert "OpenAI-Beta" not in headers
    assert "ChatGPT-Account-ID" not in headers


def test_resolve_connection_params_legacy_mode(isolated_stores):
    token = _legacy_access_token()
    _write_json(
        isolated_stores["cli"],
        {
            "tokens": {
                "access_token": token,
                "refresh_token": "rt-legacy",
                "account_id": "acct-legacy-123",
            }
        },
    )
    client = OpenAICodexClient(model="gpt-5.5")
    url, headers, is_siwc = client._resolve_connection_params()

    assert is_siwc is False
    assert url == f"{oca.CODEX_OAUTH_ISSUER.replace('auth.', 'chatgpt.')}/backend-api/codex/responses" or "backend-api/codex/responses" in url
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["originator"] == oca.CODEX_ORIGINATOR
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["ChatGPT-Account-ID"] == "acct-legacy-123"


# ---------------------------------------------------------------------------
# Refresh tests
# ---------------------------------------------------------------------------

def test_siwc_refresh_calls_official_token_url(monkeypatch):
    client = OpenAICodexClient(model="gpt-6.1-sol")
    captured_req = {}

    def mock_post(_self, url, headers=None, data=None, **kwargs):
        captured_req["url"] = url
        captured_req["headers"] = headers
        captured_req["data"] = data
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-siwc-acc",
            "refresh_token": "new-siwc-rt",
        }
        return mock_resp

    monkeypatch.setattr(httpx.Client, "post", mock_post)

    result = client._request_refresh(
        "old-rt", client_id="oaiapp_my_app", is_siwc=True
    )
    assert captured_req["url"] == oca.SIWC_TOKEN_URL
    assert captured_req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured_req["data"]["client_id"] == "oaiapp_my_app"
    assert captured_req["data"]["grant_type"] == "refresh_token"
    assert captured_req["data"]["refresh_token"] == "old-rt"
    assert result["access_token"] == "new-siwc-acc"


# ---------------------------------------------------------------------------
# SiwcOAuthManager tests
# ---------------------------------------------------------------------------

def test_siwc_oauth_manager_state_and_cancel():
    mgr = oca.SiwcOAuthManager()
    assert mgr.status()["state"] == oca.SIWC_STATE_IDLE
    assert mgr.cancel() is False  # idle cannot cancel

    status = mgr.start(open_browser=False)
    assert status["state"] == oca.SIWC_STATE_WAITING
    assert "auth_url" in status
    assert "dynamic_agent_client" in status["auth_url"]
    assert "chatgpt.tokens.use.direct" in status["auth_url"]
    assert status["host_id"].startswith("urn:uuid:")

    assert mgr.cancel() is True
    assert mgr.status()["state"] == oca.SIWC_STATE_IDLE


def test_siwc_oauth_manager_exchange_code(monkeypatch, isolated_stores):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "test-acc-tok",
        "refresh_token": "test-rf-tok",
        "scope": oca.SIWC_SCOPE,
    }
    monkeypatch.setattr(httpx.Client, "post", lambda *args, **kwargs: mock_resp)

    mgr = oca.SiwcOAuthManager()
    tokens = mgr._exchange_code("oaiapp_123", "code-abc", "verifier-xyz", "http://127.0.0.1:1455/auth/callback")
    assert tokens["access_token"] == "test-acc-tok"
    assert tokens["refresh_token"] == "test-rf-tok"


# ---------------------------------------------------------------------------
# fetch_available_models tests
# ---------------------------------------------------------------------------

def test_fetch_available_models(monkeypatch):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "models": [
            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list"},
            {"slug": "internal-model", "display_name": "Internal", "visibility": "hide"},
        ]
    }
    monkeypatch.setattr(httpx.Client, "get", lambda *args, **kwargs: mock_resp)

    models = OpenAICodexClient.fetch_available_models(access_token="test-token")
    assert len(models) == 1
    assert models[0]["slug"] == "gpt-6.1-sol"
    assert models[0]["display_name"] == "GPT-6.1-Sol"
