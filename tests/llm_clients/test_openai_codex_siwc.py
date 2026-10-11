"""Sign in with ChatGPT (SIWC / Token Sharing) integration tests.

Covers:
  - is_siwc_token detection and endpoint / header resolution
  - SiwcOAuthManager: authorize URL (first registration vs re-authorization),
    loopback listener (non-callback requests, state mismatch, access_denied,
    result page), manual pasted-URL completion, exchange validation (direct
    scope, ID-token nonce / aud)
  - cancel / supersession / logout always win over a late callback or exchange
  - host id + issued client id record that survives logout, refresh-token
    revocation on logout
  - refresh routing (stored vs JWT client id, re-login error), scope rotation
  - SIWC request body restrictions (legacy body untouched)
  - API routes under /codex-auth/siwc/

Never touches the network: the auth host is faked through
`openai_codex_auth._http_session`, the refresh path through httpx.Client.post,
and the only real sockets are the loopback listener on 127.0.0.1.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from llm_clients import openai_codex as codex_mod
from llm_clients import openai_codex_auth as oca
from llm_clients.openai_codex import OpenAICodexClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _jwt(payload: dict) -> str:
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(payload)}.sig"


def _siwc_access_token(
    client_id: str | None = "oaiapp_test123",
    scopes: tuple[str, ...] = ("chatgpt.tokens.use.direct", "openid"),
    expires_in_seconds: int = 3600,
) -> str:
    exp = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in_seconds)
    payload = {
        "exp": int(exp.timestamp()),
        "scp": list(scopes),
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-siwc-123"},
    }
    if client_id:
        payload["client_id"] = client_id
    return _jwt(payload)


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
        "client_record": saiverse_dir / oca.SIWC_CLIENT_FILENAME,
    }


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeAuthHTTP:
    """Replacement for oca._http_session(): routes calls to per-test handlers."""

    def __init__(self) -> None:
        self.calls: list = []
        self.post_handler = self._unexpected
        self.get_handler = self._unexpected

    @staticmethod
    def _unexpected(url, kwargs):
        raise AssertionError(f"unexpected HTTP call to {url}")

    def session(self):
        owner = self

        class _Session:
            def post(self, url, **kwargs):
                owner.calls.append(("POST", url, kwargs))
                return owner.post_handler(url, kwargs)

            def get(self, url, **kwargs):
                owner.calls.append(("GET", url, kwargs))
                return owner.get_handler(url, kwargs)

            def close(self):
                pass

        return _Session()


@pytest.fixture
def fake_auth_http(monkeypatch):
    http = FakeAuthHTTP()
    monkeypatch.setattr(oca, "_http_session", http.session)
    return http


@pytest.fixture
def siwc_manager():
    """A SIWC manager on an ephemeral port; any listener is closed afterwards."""
    manager = oca.SiwcOAuthManager(callback_port=0)
    yield manager
    with manager._lock:
        manager._invalidate_locked()


def _query(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def _loopback_get(url: str) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _callback_url(started: dict, **params) -> str:
    return f"{started['redirect_uri']}?{urllib.parse.urlencode(params)}"


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


def _exchange_payload(nonce: str, client_id: str, scope: str = oca.SIWC_SCOPE) -> dict:
    return {
        "access_token": _siwc_access_token(client_id=client_id),
        "refresh_token": "rt-siwc",
        "id_token": _jwt(
            {
                "nonce": nonce,
                "aud": [client_id],
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-siwc-123"},
            }
        ),
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": scope,
    }


def _exchange_handler(started: dict, client_id: str, **overrides):
    """Token-endpoint fake that answers with tokens bound to this attempt's nonce."""
    nonce = _query(started["auth_url"])["nonce"]

    def handler(url, kwargs):
        assert url == oca.SIWC_TOKEN_URL
        payload = _exchange_payload(overrides.get("nonce", nonce), client_id)
        if "scope" in overrides:
            payload["scope"] = overrides["scope"]
            payload["access_token"] = _siwc_access_token(client_id=client_id, scopes=("openid",))
        return FakeResponse(200, payload)

    return handler


def _exchange_calls(http: FakeAuthHTTP) -> list:
    return [c for c in http.calls if c[0] == "POST" and c[1] == oca.SIWC_TOKEN_URL]


# ---------------------------------------------------------------------------
# is_siwc_token / connection resolution
# ---------------------------------------------------------------------------

def test_is_siwc_token_via_explicit_scope():
    assert oca.is_siwc_token(
        {"access_token": "dummy", "scope": "openid email chatgpt.tokens.use.direct"}
    ) is True


def test_is_siwc_token_via_client_id_prefix():
    assert oca.is_siwc_token({"access_token": "dummy", "client_id": "oaiapp_ABC123xyz"}) is True


def test_is_siwc_token_via_jwt_claims():
    assert oca.is_siwc_token({"access_token": _siwc_access_token()}) is True


def test_is_siwc_token_legacy_token_returns_false():
    tokens = {
        "access_token": _legacy_access_token(),
        "scope": "openid profile email",
        "client_id": oca.CODEX_OAUTH_CLIENT_ID,
    }
    assert oca.is_siwc_token(tokens) is False
    assert oca.is_siwc_token({}) is False
    assert oca.is_siwc_token(None) is False


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
    assert "originator" not in headers
    assert "OpenAI-Beta" not in headers
    assert "ChatGPT-Account-ID" not in headers


def test_resolve_connection_params_rejects_siwc_without_direct_scope(isolated_stores):
    _write_json(
        isolated_stores["saiverse"],
        {
            "tokens": {
                "access_token": _siwc_access_token(scopes=("openid",)),
                "refresh_token": "rt",
                "client_id": "oaiapp_xyz",
                "scope": "openid profile",
            }
        },
    )
    with pytest.raises(RuntimeError, match="chatgpt.tokens.use.direct"):
        OpenAICodexClient(model="m")._resolve_connection_params()


def test_resolve_connection_params_legacy_mode(isolated_stores):
    token = _legacy_access_token()
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": token, "refresh_token": "rt-legacy", "account_id": "acct-legacy-123"}},
    )
    client = OpenAICodexClient(model="gpt-5.5")
    url, headers, is_siwc = client._resolve_connection_params()

    assert is_siwc is False
    assert url == f"{codex_mod.CODEX_BASE_URL}/responses"
    assert headers["originator"] == oca.CODEX_ORIGINATOR
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["ChatGPT-Account-ID"] == "acct-legacy-123"


def test_httpx_adapter_ok_is_2xx_only():
    def adapter(status):
        raw = MagicMock()
        raw.status_code = status
        return codex_mod._HttpxResponseAdapter(raw)

    assert adapter(200).ok is True
    assert adapter(204).ok is True
    assert adapter(302).ok is False
    assert adapter(401).ok is False


# ---------------------------------------------------------------------------
# SiwcOAuthManager: start / authorize URL / host id
# ---------------------------------------------------------------------------

def test_start_first_registration_url_and_host_id_persisted_before_sign_in(
    isolated_stores, siwc_manager
):
    started = siwc_manager.start()
    assert started["state"] == oca.SIWC_STATE_WAITING
    assert {"attempt_id", "lease_id", "auth_url", "redirect_uri"} <= set(started)
    assert started["auth_url"].startswith(oca.SIWC_AUTHORIZE_URL + "?")

    q = _query(started["auth_url"])
    assert q["client_id"] == oca.SIWC_DYNAMIC_CLIENT_ID
    assert q["agent_name_hint"] == oca.SIWC_APP_NAME_HINT
    assert q["response_type"] == "code"
    assert q["scope"] == oca.SIWC_SCOPE
    assert q["resource"] == oca.SIWC_RESOURCE
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] and q["state"] and q["nonce"]
    assert q["redirect_uri"] == started["redirect_uri"]
    assert q["redirect_uri"].startswith("http://127.0.0.1:")
    assert q["redirect_uri"].endswith(oca.SIWC_CALLBACK_PATH)
    assert "localhost" not in q["redirect_uri"]

    # host id is on disk before anything came back from the browser
    record = _read_json(isolated_stores["client_record"])
    assert record["ext_agent_host_id"] == q["ext_agent_host_id"]
    assert record["ext_agent_host_id"].startswith("urn:uuid:")
    assert "client_id" not in record


def test_join_shares_attempt_and_one_lease_cancel_keeps_it(isolated_stores, siwc_manager):
    first = siwc_manager.start()
    second = siwc_manager.start()
    assert second["attempt_id"] == first["attempt_id"]
    assert second["auth_url"] == first["auth_url"]
    assert second["lease_id"] != first["lease_id"]
    assert siwc_manager.cancel(first["attempt_id"], first["lease_id"]) is False
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_WAITING
    assert siwc_manager.cancel(second["attempt_id"], second["lease_id"]) is True
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE


# ---------------------------------------------------------------------------
# loopback listener
# ---------------------------------------------------------------------------

def test_loopback_success_after_non_callback_request(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_new")

    port = urllib.parse.urlparse(started["redirect_uri"]).port
    status, _ = _loopback_get(f"http://127.0.0.1:{port}/favicon.ico")
    assert status == 404  # served, but the listener keeps waiting
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_WAITING

    status, body = _loopback_get(
        _callback_url(started, code="code-1", state=q["state"], client_id="oaiapp_new",
                      scope=oca.SIWC_SCOPE)
    )
    assert status == 200
    assert "サインインが完了しました" in body

    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_SUCCESS
    assert st["attempt_id"] == started["attempt_id"]
    assert "rt-siwc" not in json.dumps(st) and "access_token" not in json.dumps(st)

    (_m, _url, kwargs), = _exchange_calls(fake_auth_http)
    form = kwargs["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == "oaiapp_new"
    assert form["code"] == "code-1"
    assert form["code_verifier"]
    assert form["redirect_uri"] == started["redirect_uri"]
    assert form["resource"] == oca.SIWC_RESOURCE

    stored = _read_json(isolated_stores["saiverse"])
    assert stored["source"] == "saiverse_siwc"
    assert stored["tokens"]["client_id"] == "oaiapp_new"
    assert stored["tokens"]["ext_agent_host_id"] == q["ext_agent_host_id"]
    assert oca.SIWC_DIRECT_SCOPE in stored["tokens"]["scope"].split()
    assert stored["tokens"]["resource"] == oca.SIWC_RESOURCE
    assert _read_json(isolated_stores["client_record"])["client_id"] == "oaiapp_new"


def test_state_mismatch_is_rejected_without_ending_attempt(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    status, body = _loopback_get(
        _callback_url(started, code="c", state="not-the-state", client_id="oaiapp_x")
    )
    assert status == 400
    assert "できませんでした" in body
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_WAITING
    assert fake_auth_http.calls == []

    # the genuine callback still goes through afterwards
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_x")
    status, _ = _loopback_get(_callback_url(started, code="c", state=q["state"], client_id="oaiapp_x"))
    assert status == 200
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_SUCCESS


def test_access_denied_yields_error_and_failure_page(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    status, body = _loopback_get(_callback_url(started, error="access_denied", state=q["state"]))
    assert status == 400
    assert "サインインできませんでした" in body
    assert "拒否" in body
    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_ERROR
    assert "拒否" in st["error"]
    assert fake_auth_http.calls == []  # never exchanged
    assert not isolated_stores["saiverse"].exists()


def test_cancel_during_exchange_does_not_persist(isolated_stores, fake_auth_http, siwc_manager):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    entered, gate = threading.Event(), threading.Event()
    inner = _exchange_handler(started, "oaiapp_c")

    def slow_exchange(url, kwargs):
        entered.set()
        assert gate.wait(10)
        return inner(url, kwargs)

    fake_auth_http.post_handler = slow_exchange
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(
            page=_loopback_get(_callback_url(started, code="c", state=q["state"], client_id="oaiapp_c"))
        )
    )
    t.start()
    assert entered.wait(10)
    assert siwc_manager.cancel(started["attempt_id"], started["lease_id"]) is True
    gate.set()
    t.join(20)

    status, body = result["page"]
    assert status == 400
    assert "サインインできませんでした" in body
    assert not isolated_stores["saiverse"].exists()
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE
    assert "client_id" not in oca.read_siwc_client_record()


def test_cancelled_attempt_cannot_be_revived_by_new_start(
    isolated_stores, fake_auth_http, siwc_manager
):
    first = siwc_manager.start()
    old_attempt = siwc_manager._attempt
    assert siwc_manager.cancel(first["attempt_id"], first["lease_id"]) is True

    second = siwc_manager.start()
    assert second["attempt_id"] != first["attempt_id"]
    # a fresh start gets its own cancel event; the old one is never cleared
    assert old_attempt.cancel_event.is_set()
    assert siwc_manager._attempt.cancel_event is not old_attempt.cancel_event

    # A late callback for the old attempt is refused before any exchange.
    outcome = siwc_manager._process_callback(
        old_attempt, {"state": old_attempt.state, "code": "c", "client_id": "oaiapp_old"}
    )
    assert outcome.processed is False
    assert fake_auth_http.calls == []
    # And even a commit from the old generation is discarded.
    committed = siwc_manager._commit(
        first["attempt_id"], old_attempt.cancel_event, oca.read_logout_marker(),
        {"access_token": _siwc_access_token(), "refresh_token": "rt"},
        success_state={"state": oca.SIWC_STATE_SUCCESS, "attempt_id": first["attempt_id"]},
    )
    assert committed is False
    assert not isolated_stores["saiverse"].exists()
    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_WAITING
    assert st["attempt_id"] == second["attempt_id"]


def test_logout_during_inflight_exchange_prevents_persistence(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    entered, gate = threading.Event(), threading.Event()
    inner = _exchange_handler(started, "oaiapp_l")

    def slow_exchange(url, kwargs):
        entered.set()
        assert gate.wait(10)
        return inner(url, kwargs)

    fake_auth_http.post_handler = slow_exchange
    t = threading.Thread(
        target=lambda: _loopback_get(
            _callback_url(started, code="c", state=q["state"], client_id="oaiapp_l")
        )
    )
    t.start()
    assert entered.wait(10)
    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result["removed"] is False
    gate.set()
    t.join(20)

    assert not isolated_stores["saiverse"].exists()
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE


def test_logout_in_another_process_blocks_siwc_commit(isolated_stores, fake_auth_http, siwc_manager):
    """別プロセスの logout (= 別インスタンス、共有は marker ファイルだけ) も効く。"""
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    other_process = oca.CodexDeviceLoginManager()
    other_process.logout_and_delete_store()

    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_p")
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_p"),
    )
    assert out["ok"] is False
    assert not isolated_stores["saiverse"].exists()
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE


# ---------------------------------------------------------------------------
# re-authorization with the saved issued client id
# ---------------------------------------------------------------------------

def test_reauth_uses_saved_client_id_and_rejects_different_callback_client_id(
    isolated_stores, fake_auth_http, siwc_manager
):
    oca.save_siwc_issued_client_id("oaiapp_saved")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    assert q["client_id"] == "oaiapp_saved"
    assert "agent_name_hint" not in q

    status, body = _loopback_get(
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_other")
    )
    assert status == 400
    assert "client_id" in body
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_ERROR
    assert fake_auth_http.calls == []
    assert not isolated_stores["saiverse"].exists()


def test_reauth_callback_without_client_id_uses_saved_one(
    isolated_stores, fake_auth_http, siwc_manager
):
    oca.save_siwc_issued_client_id("oaiapp_saved")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_saved")
    out = siwc_manager.complete(started["attempt_id"], _callback_url(started, code="c", state=q["state"]))
    assert out["ok"] is True
    (_m, _url, kwargs), = _exchange_calls(fake_auth_http)
    assert kwargs["data"]["client_id"] == "oaiapp_saved"
    assert _read_json(isolated_stores["saiverse"])["tokens"]["client_id"] == "oaiapp_saved"


def test_first_registration_callback_without_client_id_is_an_error(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    out = siwc_manager.complete(started["attempt_id"], _callback_url(started, code="c", state=q["state"]))
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert fake_auth_http.calls == []


# ---------------------------------------------------------------------------
# manual completion (pasted callback URL)
# ---------------------------------------------------------------------------

def test_manual_complete_commits_and_rejects_stale_attempt(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    url = _callback_url(started, code="c", state=q["state"], client_id="oaiapp_m")

    with pytest.raises(oca.SiwcCallbackRejected) as stale:
        siwc_manager.complete(started["attempt_id"] - 1, url)
    assert stale.value.status_code == 409
    with pytest.raises(oca.SiwcCallbackRejected) as not_callback:
        siwc_manager.complete(started["attempt_id"], "https://example.com/somewhere?x=1")
    assert not_callback.value.status_code == 400
    with pytest.raises(oca.SiwcCallbackRejected) as wrong_state:
        siwc_manager.complete(
            started["attempt_id"],
            _callback_url(started, code="c", state="bogus", client_id="oaiapp_m"),
        )
    assert wrong_state.value.status_code == 400
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_WAITING
    assert fake_auth_http.calls == []

    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_m")
    out = siwc_manager.complete(started["attempt_id"], f"  {url}  ")
    assert out["ok"] is True
    assert out["state"] == oca.SIWC_STATE_SUCCESS
    assert _read_json(isolated_stores["saiverse"])["tokens"]["client_id"] == "oaiapp_m"
    # the listener stops once a callback was processed
    assert _wait_for(lambda: siwc_manager._attempt.done.is_set())

    # the same URL again: the attempt is over
    with pytest.raises(oca.SiwcCallbackRejected) as again:
        siwc_manager.complete(started["attempt_id"], url)
    assert again.value.status_code == 409


# ---------------------------------------------------------------------------
# exchange validation
# ---------------------------------------------------------------------------

def test_missing_direct_scope_is_rejected(isolated_stores, fake_auth_http, siwc_manager):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(
        started, "oaiapp_s", scope="openid profile email offline_access"
    )
    out = siwc_manager.complete(
        started["attempt_id"], _callback_url(started, code="c", state=q["state"], client_id="oaiapp_s")
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "chatgpt.tokens.use.direct" in out["error"]
    assert not isolated_stores["saiverse"].exists()
    assert "client_id" not in oca.read_siwc_client_record()


def test_id_token_nonce_mismatch_is_rejected(isolated_stores, fake_auth_http, siwc_manager):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_n", nonce="other-nonce")
    out = siwc_manager.complete(
        started["attempt_id"], _callback_url(started, code="c", state=q["state"], client_id="oaiapp_n")
    )
    assert out["ok"] is False
    assert "nonce" in out["error"]
    assert not isolated_stores["saiverse"].exists()


def test_id_token_aud_must_contain_issued_client_id():
    data = _exchange_payload("n1", "oaiapp_someone_else")
    with pytest.raises(oca.SiwcOAuthError, match="aud"):
        oca._validate_siwc_exchange(data, "oaiapp_me", "n1")


# ---------------------------------------------------------------------------
# logout: client record survives, refresh token is revoked
# ---------------------------------------------------------------------------

def test_client_record_survives_logout_and_host_id_is_stable(isolated_stores, siwc_manager):
    first = siwc_manager.start()
    host_id = _query(first["auth_url"])["ext_agent_host_id"]
    oca.save_siwc_issued_client_id("oaiapp_keep")
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": "legacy"}})

    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result == {"removed": True, "revoked": None}
    assert not isolated_stores["saiverse"].exists()
    record = oca.read_siwc_client_record()
    assert record == {"ext_agent_host_id": host_id, "client_id": "oaiapp_keep"}

    again = siwc_manager.start()
    q = _query(again["auth_url"])
    assert q["ext_agent_host_id"] == host_id
    assert q["client_id"] == "oaiapp_keep"


def _siwc_store(path: Path) -> None:
    _write_json(
        path,
        {
            "tokens": {
                "access_token": _siwc_access_token(client_id="oaiapp_rv"),
                "refresh_token": "rt-to-revoke",
                "client_id": "oaiapp_rv",
                "scope": oca.SIWC_SCOPE,
            }
        },
    )


def test_logout_revokes_siwc_refresh_token_before_deleting(
    isolated_stores, fake_auth_http, siwc_manager
):
    _siwc_store(isolated_stores["saiverse"])
    revoke_url = "https://auth.openai.com/oauth/revoke"
    store_present_at_revoke: list = []

    fake_auth_http.get_handler = lambda url, kw: (
        FakeResponse(200, {"revocation_endpoint": revoke_url})
        if url == oca.SIWC_OPENID_CONFIGURATION_URL else pytest.fail(url)
    )

    def revoke(url, kwargs):
        assert url == revoke_url
        store_present_at_revoke.append(isolated_stores["saiverse"].exists())
        return FakeResponse(200, None)

    fake_auth_http.post_handler = revoke
    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result == {"removed": True, "revoked": True}
    assert store_present_at_revoke == [True]
    assert not isolated_stores["saiverse"].exists()
    _m, _url, kwargs = fake_auth_http.calls[-1]
    assert kwargs["data"] == {
        "token": "rt-to-revoke",
        "token_type_hint": "refresh_token",
        "client_id": "oaiapp_rv",
    }


def test_logout_still_clears_tokens_when_revocation_fails(
    isolated_stores, fake_auth_http, siwc_manager
):
    _siwc_store(isolated_stores["saiverse"])
    fake_auth_http.get_handler = lambda url, kw: FakeResponse(
        200, {"revocation_endpoint": "https://auth.openai.com/oauth/revoke"}
    )
    fake_auth_http.post_handler = lambda url, kw: FakeResponse(500, {"error": "boom"})
    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result == {"removed": True, "revoked": False}
    assert not isolated_stores["saiverse"].exists()


def test_logout_never_revokes_or_touches_codex_cli_store(isolated_stores, fake_auth_http, siwc_manager):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": _legacy_access_token()}})
    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result == {"removed": False, "revoked": None}
    assert isolated_stores["cli"].exists()
    assert fake_auth_http.calls == []


# ---------------------------------------------------------------------------
# refresh routing
# ---------------------------------------------------------------------------

def _capture_httpx_post(monkeypatch, response_payload: dict) -> dict:
    captured: dict = {}

    def fake_post(_self, url, headers=None, data=None, json=None, **kwargs):
        captured.update(url=url, headers=headers, data=data, json=json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = response_payload
        return resp

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    return captured


def test_siwc_refresh_uses_jwt_client_id_and_persists_rotated_scope(isolated_stores, monkeypatch):
    old_access = _siwc_access_token(client_id="oaiapp_jwt", expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old", "scope": "openid chatgpt.tokens.use.direct"}},
    )
    new_access = _siwc_access_token(client_id="oaiapp_jwt")
    new_scope = "openid offline_access resource.invoke chatgpt.tokens.use.direct"
    captured = _capture_httpx_post(
        monkeypatch,
        {"access_token": new_access, "refresh_token": "rt-new", "scope": new_scope},
    )
    OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)

    assert captured["url"] == oca.SIWC_TOKEN_URL
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "client_id": "oaiapp_jwt",
        "refresh_token": "rt-old",
        "resource": oca.SIWC_RESOURCE,
    }  # no scope sent
    tokens = _read_json(isolated_stores["saiverse"])["tokens"]
    assert tokens["access_token"] == new_access
    assert tokens["refresh_token"] == "rt-new"
    assert tokens["scope"] == new_scope


def test_siwc_refresh_prefers_stored_client_id(isolated_stores, monkeypatch):
    old_access = _siwc_access_token(client_id="oaiapp_jwt", expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old", "client_id": "oaiapp_stored"}},
    )
    captured = _capture_httpx_post(monkeypatch, {"access_token": _siwc_access_token(), "refresh_token": "rt-new"})
    OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)
    assert captured["data"]["client_id"] == "oaiapp_stored"


def test_siwc_refresh_without_any_client_id_requires_relogin(isolated_stores, monkeypatch):
    old_access = _siwc_access_token(client_id=None, expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": old_access, "refresh_token": "rt-old", "scope": oca.SIWC_SCOPE}},
    )
    monkeypatch.setattr(
        httpx.Client, "post",
        lambda *a, **k: pytest.fail("a SIWC refresh token must not be sent anywhere"),
    )
    with pytest.raises(RuntimeError, match="ログインし直して"):
        OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)
    assert _read_json(isolated_stores["saiverse"])["tokens"]["refresh_token"] == "rt-old"


def test_legacy_refresh_does_not_add_scope_to_cli_store(isolated_stores, monkeypatch):
    old_access = _legacy_access_token(expires_in_seconds=-100)
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}})
    captured = _capture_httpx_post(
        monkeypatch,
        {"access_token": _legacy_access_token(), "refresh_token": "rt-new", "scope": "openid profile"},
    )
    OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)
    assert captured["url"] == oca.CODEX_REFRESH_TOKEN_URL
    tokens = _read_json(isolated_stores["cli"])["tokens"]
    assert tokens["refresh_token"] == "rt-new"
    assert "scope" not in tokens


# ---------------------------------------------------------------------------
# request body
# ---------------------------------------------------------------------------

_MESSAGES = [
    {"role": "system", "content": "be nice"},
    {"role": "user", "content": "hi"},
]


def _body_with_max_tokens(client: OpenAICodexClient) -> dict:
    client.configure_parameters({"max_output_tokens": 1234})
    return client._build_body(
        _MESSAGES, temperature=0.3, tools=None, response_schema=None, reasoning_effort=None
    )


def test_siwc_body_omits_max_output_tokens(isolated_stores, monkeypatch):
    _write_json(
        isolated_stores["saiverse"],
        {"tokens": {"access_token": _siwc_access_token(), "refresh_token": "rt",
                    "client_id": "oaiapp_b", "scope": oca.SIWC_SCOPE}},
    )
    client = OpenAICodexClient(model="m")
    body = _body_with_max_tokens(client)
    original = json.loads(json.dumps(body))
    sent: dict = {}

    class FakeHttpx:
        def build_request(self, method, url, headers=None, json=None):
            sent.update(method=method, url=url, json=json)
            return object()

        def send(self, _req, stream=False):
            raw = MagicMock()
            raw.status_code = 200
            return raw

    monkeypatch.setattr(client, "_ensure_session", lambda is_siwc=False: FakeHttpx())
    client._post_responses(body)

    assert sent["url"] == oca.SIWC_RESPONSES_URL
    assert "max_output_tokens" not in sent["json"]
    assert "temperature" not in sent["json"]
    assert sent["json"]["store"] is False
    assert sent["json"]["stream"] is True
    assert sent["json"]["instructions"] == "be nice"
    assert all(item.get("role") != "system" for item in sent["json"]["input"])
    assert body == original  # the caller's body is not mutated


def test_legacy_body_keeps_max_output_tokens(isolated_stores, monkeypatch):
    _write_json(
        isolated_stores["cli"],
        {"tokens": {"access_token": _legacy_access_token(), "refresh_token": "rt"}},
    )
    client = OpenAICodexClient(model="m")
    body = _body_with_max_tokens(client)
    sent: dict = {}

    class FakeCffi:
        def post(self, url, headers=None, json=None, timeout=None, stream=False):
            sent.update(url=url, json=json)
            resp = MagicMock()
            resp.ok = True
            return resp

    monkeypatch.setattr(client, "_ensure_session", lambda is_siwc=False: FakeCffi())
    client._post_responses(body)
    assert sent["url"] == f"{codex_mod.CODEX_BASE_URL}/responses"
    assert sent["json"] is body
    assert sent["json"]["max_output_tokens"] == 1234


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routes import codex_auth as codex_auth_routes

    app = FastAPI()
    app.include_router(codex_auth_routes.router, prefix="/codex-auth")
    return TestClient(app), codex_auth_routes


def test_route_siwc_start_status_cancel(api_client, monkeypatch):
    client, routes = api_client
    monkeypatch.setattr(
        routes.SIWC_LOGIN_MANAGER, "start",
        lambda: {"state": "waiting", "attempt_id": 4, "lease_id": "L", "auth_url": "u",
                 "redirect_uri": "http://127.0.0.1:1455/auth/callback"},
    )
    res = client.post("/codex-auth/siwc/start")
    assert res.status_code == 200
    assert res.json()["auth_url"] == "u"

    monkeypatch.setattr(routes.SIWC_LOGIN_MANAGER, "status", lambda: {"state": "idle"})
    assert client.get("/codex-auth/siwc/status").json() == {"state": "idle"}

    assert client.post("/codex-auth/siwc/cancel").status_code == 422
    assert client.post("/codex-auth/siwc/cancel", json={"attempt_id": 4}).status_code == 422
    calls: list = []
    monkeypatch.setattr(
        routes.SIWC_LOGIN_MANAGER, "cancel",
        lambda attempt_id, lease_id: calls.append((attempt_id, lease_id)) or True,
    )
    res = client.post("/codex-auth/siwc/cancel", json={"attempt_id": 4, "lease_id": "L"})
    assert res.status_code == 200 and res.json()["cancelled"] is True
    assert calls == [(4, "L")]


def test_route_siwc_start_maps_flow_error_to_502(api_client, monkeypatch):
    client, routes = api_client

    def _boom():
        raise oca.SiwcOAuthError("port busy")

    monkeypatch.setattr(routes.SIWC_LOGIN_MANAGER, "start", _boom)
    res = client.post("/codex-auth/siwc/start")
    assert res.status_code == 502
    assert "port busy" in res.json()["detail"]


def test_route_siwc_complete(api_client, monkeypatch):
    client, routes = api_client
    assert client.post("/codex-auth/siwc/complete", json={"attempt_id": 1}).status_code == 422

    def _stale(attempt_id, callback_url):
        raise oca.SiwcCallbackRejected("stale", 409)

    monkeypatch.setattr(routes.SIWC_LOGIN_MANAGER, "complete", _stale)
    res = client.post("/codex-auth/siwc/complete", json={"attempt_id": 1, "callback_url": "x"})
    assert res.status_code == 409

    monkeypatch.setattr(
        routes.SIWC_LOGIN_MANAGER, "complete",
        lambda attempt_id, callback_url: {"ok": True, "state": "success", "attempt_id": attempt_id},
    )
    res = client.post("/codex-auth/siwc/complete", json={"attempt_id": 2, "callback_url": "x"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "state": "success", "attempt_id": 2}


def test_route_logout_invalidates_both_managers(api_client, isolated_stores):
    client, routes = api_client
    device_gen = routes.LOGIN_MANAGER._generation
    siwc_gen = routes.SIWC_LOGIN_MANAGER._generation
    _write_json(isolated_stores["saiverse"], {"tokens": {"access_token": _legacy_access_token()}})
    res = client.post("/codex-auth/logout")
    assert res.status_code == 200
    body = res.json()
    assert body["removed"] is True
    assert body["revoked"] is None
    assert routes.LOGIN_MANAGER._generation == device_gen + 1
    assert routes.SIWC_LOGIN_MANAGER._generation == siwc_gen + 1
    assert not isolated_stores["saiverse"].exists()
