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
import socket
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


@pytest.fixture
def revocations(monkeypatch):
    """Records every best-effort revocation instead of calling the issuer."""
    calls: list = []

    def fake_revoke(refresh_token, client_id):
        calls.append((refresh_token, client_id))
        return True

    monkeypatch.setattr(oca, "revoke_siwc_refresh_token", fake_revoke)
    return calls


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


def test_cancel_during_exchange_does_not_persist(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
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
    # The issued id passed the state check, so it is kept for the next sign-in
    # (spec: a failed attempt re-authorizes with the id retained for it).
    assert oca.read_siwc_client_record()["client_id"] == "oaiapp_c"
    # The minted grant was not saved, so it was revoked (with the issued id).
    assert revocations == [("rt-siwc", "oaiapp_c")]


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
    isolated_stores, fake_auth_http, siwc_manager, revocations
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
    assert result == {"removed": False, "revoked": None}
    gate.set()
    t.join(20)

    assert not isolated_stores["saiverse"].exists()
    # logged out in this process: the generation moved on, state stays idle
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE
    assert revocations == [("rt-siwc", "oaiapp_l")]


def test_logout_in_another_process_blocks_siwc_commit(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
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
    # The attempt is still this manager's current one, so it ends as an error
    # the modal can show (with a retry), never as an anonymous idle.
    expected = {
        "state": oca.SIWC_STATE_ERROR,
        "attempt_id": started["attempt_id"],
        "error": oca.LOGIN_REFUSED_BY_LOGOUT_MESSAGE,
    }
    assert siwc_manager.status() == expected
    assert {k: out[k] for k in expected} == expected
    assert revocations == [("rt-siwc", "oaiapp_p")]


# ---------------------------------------------------------------------------
# listener lifetime: accepted connections, deadline, worker start, join
# ---------------------------------------------------------------------------

def _worker_thread(attempt_id: int) -> threading.Thread | None:
    name = f"saiverse-siwc-login-{attempt_id}"
    return next((t for t in threading.enumerate() if t.name == name), None)


class _Trickler:
    """A raw TCP client that sends a request line one byte at a time, forever."""

    def __init__(self, port: int, interval: float = 0.2) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self._stop = threading.Event()
        self._interval = interval
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        payload = b"GET /auth/callback?" + b"x" * 10000
        for byte in payload:
            if self._stop.wait(self._interval):
                return
            try:
                self.sock.sendall(bytes([byte]))
            except OSError:
                return

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        try:
            self.sock.close()
        except OSError:
            pass


def test_cancel_cuts_a_connection_still_sending_its_request(isolated_stores, siwc_manager):
    started = siwc_manager.start()
    worker = _worker_thread(started["attempt_id"])
    assert worker is not None and worker.is_alive()
    server = siwc_manager._attempt.server
    client = _Trickler(urllib.parse.urlparse(started["redirect_uri"]).port)
    try:
        assert _wait_for(lambda: server.active_connection is not None, 5)
        cancelled_at = time.monotonic()
        assert siwc_manager.cancel(started["attempt_id"], started["lease_id"]) is True
        worker.join(2.0)
        assert not worker.is_alive(), "worker stayed blocked on the accepted connection"
        assert time.monotonic() - cancelled_at < 2.0
        assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE
    finally:
        client.close()


def test_logout_cuts_a_silent_connection(isolated_stores, siwc_manager):
    """何も送らない接続 (5 秒の無音タイムアウト待ち) もログアウトで即座に切れる。"""
    started = siwc_manager.start()
    worker = _worker_thread(started["attempt_id"])
    server = siwc_manager._attempt.server
    sock = socket.create_connection(
        ("127.0.0.1", urllib.parse.urlparse(started["redirect_uri"]).port), timeout=5
    )
    try:
        assert _wait_for(lambda: server.active_connection is not None, 5)
        oca.logout_all_and_delete_store([siwc_manager])
        worker.join(2.0)
        assert not worker.is_alive()
        assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE
    finally:
        sock.close()


def test_deadline_ends_the_attempt_even_while_a_client_trickles(isolated_stores):
    manager = oca.SiwcOAuthManager(callback_port=0, max_wait_seconds=1.0)
    client = None
    try:
        started = manager.start()
        worker = _worker_thread(started["attempt_id"])
        server = manager._attempt.server
        client = _Trickler(urllib.parse.urlparse(started["redirect_uri"]).port)
        assert _wait_for(lambda: server.active_connection is not None, 5)
        # The trickle never lets handle_request() return on its own; only the
        # deadline (1 s) can end the attempt.
        assert _wait_for(lambda: manager.status()["state"] == oca.SIWC_STATE_ERROR, 3.0)
        st = manager.status()
        assert st["attempt_id"] == started["attempt_id"]
        assert "打ち切りました" in st["error"]
        worker.join(2.0)
        assert not worker.is_alive()
        assert server.closed
    finally:
        if client is not None:
            client.close()
        with manager._lock:
            manager._invalidate_locked()


def test_callback_being_processed_is_not_cut_by_cancel(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """要求を送り終えた接続は切らない — 取り下げの結果ページがブラウザに届く。

    (切るのは要求を送り終えていない接続だけ。test_cancel_during_exchange_does_not_persist
    と同じ状況を、接続が追跡から外れていることまで含めて確かめる。)
    """
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    server = siwc_manager._attempt.server
    entered, gate = threading.Event(), threading.Event()
    inner = _exchange_handler(started, "oaiapp_k")

    def slow_exchange(url, kwargs):
        entered.set()
        assert gate.wait(10)
        return inner(url, kwargs)

    fake_auth_http.post_handler = slow_exchange
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(
            page=_loopback_get(_callback_url(started, code="c", state=q["state"], client_id="oaiapp_k"))
        )
    )
    t.start()
    assert entered.wait(10)
    assert server.active_connection is None  # released once the request arrived
    assert siwc_manager.cancel(started["attempt_id"], started["lease_id"]) is True
    gate.set()
    t.join(20)
    status, body = result["page"]
    assert status == 400
    assert "サインインできませんでした" in body
    assert revocations == [("rt-siwc", "oaiapp_k")]


def test_manual_completion_cuts_a_trickling_connection_and_ends_the_worker(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """貼り付けで試行が終わったら、要求を送り終えていない別の接続も切り、受け口とワーカーを残さない。"""
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    worker = _worker_thread(started["attempt_id"])
    assert worker is not None and worker.is_alive()
    server = siwc_manager._attempt.server
    port = urllib.parse.urlparse(started["redirect_uri"]).port
    client = _Trickler(port)
    try:
        # The worker is now inside handle_request() on the trickling connection.
        assert _wait_for(lambda: server.active_connection is not None, 5)
        fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_tr")
        out = siwc_manager.complete(
            started["attempt_id"],
            _callback_url(started, code="c", state=q["state"], client_id="oaiapp_tr"),
        )
        assert out["ok"] is True
        assert out["state"] == oca.SIWC_STATE_SUCCESS
        worker.join(2.0)
        assert not worker.is_alive(), "worker stayed blocked on the trickling connection"
        assert server.closed
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))  # the listener's port is free again
        finally:
            probe.close()
    finally:
        client.close()
    assert _read_json(isolated_stores["saiverse"])["tokens"]["refresh_token"] == "rt-siwc"
    assert revocations == []


def test_deadline_closes_the_listener_of_an_attempt_a_callback_already_finished(
    isolated_stores, siwc_manager
):
    """打ち切り時刻の処理は、戻りの処理が終わった試行でも受け口を閉じる (後始末の二重化)。"""
    started = siwc_manager.start()
    attempt = siwc_manager._attempt
    with siwc_manager._lock:
        attempt.claimed = True  # a callback took the attempt ...
    attempt.done.set()  # ... and finished, without closing anything
    siwc_manager._expire(attempt)
    assert attempt.server.closed
    # The state stays the callback's (the deadline does not overwrite it).
    assert siwc_manager.status()["attempt_id"] == started["attempt_id"]
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_WAITING


def test_worker_start_failure_releases_the_port_and_ends_as_error(
    isolated_stores, siwc_manager, monkeypatch
):
    failing = {"on": True}
    real_start = threading.Thread.start

    def start_or_fail(self):
        if failing["on"] and self.name.startswith("saiverse-siwc-login-"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start_or_fail)
    with pytest.raises(oca.SiwcOAuthError, match="待ち受け"):
        siwc_manager.start()

    attempt = siwc_manager._attempt
    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_ERROR
    assert st["attempt_id"] == attempt.generation
    assert "待ち受け" in st["error"]
    assert "auth_url" not in st
    assert siwc_manager._leases == set()
    assert attempt.cancel_event.is_set()
    # the bound port was released
    assert attempt.server.closed
    port = attempt.server.server_address[1]
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()
    # a pasted callback for the dead attempt is refused
    with pytest.raises(oca.SiwcCallbackRejected):
        siwc_manager.complete(attempt.generation, f"{attempt.redirect_uri}?code=c&state={attempt.state}")

    failing["on"] = False
    again = siwc_manager.start()
    assert again["state"] == oca.SIWC_STATE_WAITING
    assert again["attempt_id"] != attempt.generation


def test_route_siwc_start_maps_worker_start_failure_to_502(
    api_client, isolated_stores, monkeypatch
):
    client, routes = api_client
    manager = oca.SiwcOAuthManager(callback_port=0)
    monkeypatch.setattr(routes, "SIWC_LOGIN_MANAGER", manager)
    real_start = threading.Thread.start

    def start_or_fail(self):
        if self.name.startswith("saiverse-siwc-login-"):
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", start_or_fail)
    res = client.post("/codex-auth/siwc/start")
    assert res.status_code == 502
    assert "待ち受け" in res.json()["detail"]
    assert manager.status()["state"] == oca.SIWC_STATE_ERROR


def test_start_after_a_logout_elsewhere_opens_a_fresh_attempt_instead_of_joining(
    isolated_stores, siwc_manager
):
    first = siwc_manager.start()
    old_attempt = siwc_manager._attempt
    old_worker = _worker_thread(first["attempt_id"])
    # Another process sharing this ~/.saiverse logs out: only the marker moves.
    with oca.saiverse_store_lock():
        oca._renew_logout_marker_locked()

    second = siwc_manager.start()
    assert second["attempt_id"] != first["attempt_id"]
    assert second["auth_url"] != first["auth_url"]
    assert old_attempt.cancel_event.is_set()
    assert old_attempt.server.closed
    old_worker.join(2.0)
    assert not old_worker.is_alive()
    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_WAITING
    assert st["attempt_id"] == second["attempt_id"]
    # the stale lease is no longer accepted; the fresh attempt joins normally
    assert siwc_manager.cancel(first["attempt_id"], first["lease_id"]) is False
    third = siwc_manager.start()
    assert third["attempt_id"] == second["attempt_id"]


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


def test_failed_exchange_keeps_issued_client_id_and_revokes_nothing(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """invalid_grant など交換の失敗: 発行済みの id は次の試行へ引き継がれ、失効は不要。"""
    started = siwc_manager.start()
    host_id = _query(started["auth_url"])["ext_agent_host_id"]
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = lambda url, kw: FakeResponse(400, {"error": "invalid_grant"})
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_kept"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert not isolated_stores["saiverse"].exists()
    assert oca.read_siwc_client_record() == {
        "ext_agent_host_id": host_id, "client_id": "oaiapp_kept",
    }
    assert revocations == []  # nothing was minted

    again = _query(siwc_manager.start()["auth_url"])
    assert again["client_id"] == "oaiapp_kept"
    assert "agent_name_hint" not in again
    assert again["ext_agent_host_id"] == host_id


def test_invalid_client_clears_saved_client_id_so_next_sign_in_registers_afresh(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    oca.save_siwc_issued_client_id("oaiapp_dead")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    host_id = q["ext_agent_host_id"]
    assert q["client_id"] == "oaiapp_dead"
    fake_auth_http.post_handler = lambda url, kw: FakeResponse(
        401, {"error": "invalid_client", "error_description": "unknown client"}
    )
    out = siwc_manager.complete(started["attempt_id"], _callback_url(started, code="c", state=q["state"]))
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "invalid_client" in out["error"]
    assert "新しく登録し直します" in out["error"]
    # the host id stays; only the rejected registration is forgotten
    assert oca.read_siwc_client_record() == {"ext_agent_host_id": host_id}
    assert revocations == []

    again = _query(siwc_manager.start()["auth_url"])
    assert again["client_id"] == oca.SIWC_DYNAMIC_CLIENT_ID
    assert again["agent_name_hint"] == oca.SIWC_APP_NAME_HINT
    assert again["ext_agent_host_id"] == host_id


def test_authorize_invalid_client_callback_clears_saved_client_id(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """承認ページで保存済みの登録が拒否されたら、その id を忘れて次は新規登録する。"""
    oca.save_siwc_issued_client_id("oaiapp_dead")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    host_id = q["ext_agent_host_id"]
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, error="invalid_client", state=q["state"]),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "新しく登録し直します" in out["error"]
    assert fake_auth_http.calls == []
    assert oca.read_siwc_client_record() == {"ext_agent_host_id": host_id}

    again = _query(siwc_manager.start()["auth_url"])
    assert again["client_id"] == oca.SIWC_DYNAMIC_CLIENT_ID


def test_authorize_unauthorized_client_callback_keeps_saved_client_id(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """unauthorized_client はアカウント側の方針かもしれない — 登録を消すと重複登録が増える。"""
    oca.save_siwc_issued_client_id("oaiapp_kept")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    host_id = q["ext_agent_host_id"]
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, error="unauthorized_client", state=q["state"]),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "新しく登録し直します" not in out["error"]
    assert fake_auth_http.calls == []
    assert oca.read_siwc_client_record() == {
        "ext_agent_host_id": host_id, "client_id": "oaiapp_kept",
    }

    again = _query(siwc_manager.start()["auth_url"])
    assert again["client_id"] == "oaiapp_kept"


def test_first_registration_aud_mismatch_forgets_the_contradicted_client_id(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    """ID トークンの aud が戻りの client_id と食い違ったら、先に覚えた id を捨てる。"""
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    host_id = q["ext_agent_host_id"]
    nonce = q["nonce"]

    def handler(url, kwargs):
        payload = _exchange_payload(nonce, "oaiapp_claimed")
        payload["id_token"] = _jwt({"nonce": nonce, "aud": ["oaiapp_real"]})
        return FakeResponse(200, payload)

    fake_auth_http.post_handler = handler
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_claimed"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert not isolated_stores["saiverse"].exists()
    assert oca.read_siwc_client_record() == {"ext_agent_host_id": host_id}
    assert revocations == [("rt-siwc", "oaiapp_claimed")]


def test_commit_error_after_write_does_not_revoke_saved_tokens(
    isolated_stores, fake_auth_http, siwc_manager, revocations, monkeypatch
):
    """保存は済んだのに例外が上がった (ロック解放の失敗など) とき、保存済みの鍵を失効させない。"""
    # _commit writes through the store-lock-held helper (it already holds the
    # store lock), so that is the write to wrap.
    real_persist = oca._persist_saiverse_login_locked

    def persist_then_fail(tokens):
        real_persist(tokens)
        raise OSError("lock release failed after the write")

    monkeypatch.setattr(oca, "_persist_saiverse_login_locked", persist_then_fail)
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_c")
    siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_c"),
    )
    assert _read_json(isolated_stores["saiverse"])["tokens"]["refresh_token"] == "rt-siwc"
    assert revocations == []


def test_commit_error_before_write_revokes_the_minted_tokens(
    isolated_stores, fake_auth_http, siwc_manager, revocations, monkeypatch
):
    def fail_before_write(tokens):
        raise OSError("disk full")

    monkeypatch.setattr(oca, "_persist_saiverse_login_locked", fail_before_write)
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_c")
    siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_c"),
    )
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-siwc", "oaiapp_c")]


def test_commit_store_lock_timeout_fails_and_revokes_the_minted_tokens(
    isolated_stores, fake_auth_http, siwc_manager, revocations, monkeypatch
):
    """保存がストアのロックを時間内に取れない: 何も書かず、発行された鍵を失効させて失敗で終わる。"""
    monkeypatch.setattr(oca, "STORE_LOCK_TIMEOUT_SECONDS", 0.3)
    # Re-authorization, so nothing but the commit takes the store lock.
    oca.save_siwc_issued_client_id("oaiapp_to")
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_to")
    holder = oca.saiverse_store_lock(timeout=5)  # a refresh that never ends in time
    holder.acquire()
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(
            out=siwc_manager.complete(
                started["attempt_id"],
                _callback_url(started, code="c", state=q["state"], client_id="oaiapp_to"),
            )
        ),
        daemon=True,
    )
    try:
        t.start()
        t.join(10)
    finally:
        holder.release()
    assert not t.is_alive()
    assert result["out"]["ok"] is False
    st = siwc_manager.status()
    assert st["state"] == oca.SIWC_STATE_ERROR
    assert st["attempt_id"] == started["attempt_id"]
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-siwc", "oaiapp_to")]


def _watch_store_lock_requests(monkeypatch) -> threading.Event:
    """Patch oca.saiverse_store_lock so the test sees when someone asks for it."""
    real_lock = oca.saiverse_store_lock
    asked = threading.Event()

    def watched(timeout=None):
        asked.set()
        return real_lock(timeout)

    monkeypatch.setattr(oca, "saiverse_store_lock", watched)
    return asked


def test_commit_waiting_for_the_store_lock_keeps_status_and_cancel_responsive(
    isolated_stores, fake_auth_http, siwc_manager, revocations, monkeypatch
):
    """refresh がストアのロックを握っている間に戻りが保存へ進んでも、状態確認と取り下げは止まらない。

    保存はログイン管理のロックを握ったままストアのロックを待たない。待っている
    間に届いた取り下げが勝ち、ロックが空いた後も鍵は保存されず失効される。
    """
    monkeypatch.setattr(oca, "STORE_LOCK_TIMEOUT_SECONDS", 10.0)
    oca.save_siwc_issued_client_id("oaiapp_wt")  # only the commit takes the store lock
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_wt")

    holder = oca.saiverse_store_lock(timeout=5)  # a refresh in flight
    holder.acquire()
    asked = _watch_store_lock_requests(monkeypatch)
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.update(
            out=siwc_manager.complete(
                started["attempt_id"],
                _callback_url(started, code="c", state=q["state"], client_id="oaiapp_wt"),
            )
        ),
        daemon=True,
    )
    try:
        t.start()
        assert asked.wait(10), "the commit never asked for the store lock"
        threading.Event().wait(0.2)  # let it settle into waiting
        assert t.is_alive()

        asked_at = time.monotonic()
        st = siwc_manager.status()
        assert time.monotonic() - asked_at < 1.0, "status() blocked behind the store lock"
        assert st["state"] == oca.SIWC_STATE_WAITING

        asked_at = time.monotonic()
        assert siwc_manager.cancel(started["attempt_id"], started["lease_id"]) is True
        assert time.monotonic() - asked_at < 1.0, "cancel() blocked behind the store lock"
        assert t.is_alive()  # the commit is still waiting for the store lock
        assert not isolated_stores["saiverse"].exists()
    finally:
        holder.release()
    t.join(10)
    assert not t.is_alive()
    # The cancel issued during the wait wins once the lock is free.
    assert result["out"]["ok"] is False
    assert not isolated_stores["saiverse"].exists()
    assert siwc_manager.status()["state"] == oca.SIWC_STATE_IDLE
    assert revocations == [("rt-siwc", "oaiapp_wt")]


def test_device_marked_record_is_never_guessed_into_siwc(isolated_stores):
    """デバイスコード方式の印がある記録は、鍵の中身が公式方式に見えても旧方式のまま。"""
    record = {
        "source": "saiverse_device_login",
        "tokens": {
            "access_token": _siwc_access_token(client_id="oaiapp_lookalike"),
            "refresh_token": "rt-device",
            "scope": "openid chatgpt.tokens.use.direct",
            "client_id": "oaiapp_lookalike",
        },
    }
    assert oca.is_siwc_record(record) is False
    _write_json(isolated_stores["saiverse"], record)
    _url, _headers, is_siwc = OpenAICodexClient(model="gpt-6.1-sol")._resolve_connection_params()
    assert is_siwc is False
    assert oca.auth_status()["is_siwc"] is False
    # Codex CLI's auth.json has no marker and is still judged by its tokens.
    assert oca.is_siwc_record({"tokens": record["tokens"]}) is True


def test_clear_issued_client_id_leaves_a_different_id_alone(isolated_stores):
    oca.save_siwc_issued_client_id("oaiapp_newer")
    assert oca.clear_siwc_issued_client_id("oaiapp_older") is False
    assert oca.read_siwc_client_record()["client_id"] == "oaiapp_newer"
    assert oca.clear_siwc_issued_client_id("oaiapp_newer") is True
    assert "client_id" not in oca.read_siwc_client_record()


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

def test_missing_direct_scope_is_rejected(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
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
    assert oca.read_siwc_client_record()["client_id"] == "oaiapp_s"
    assert revocations == [("rt-siwc", "oaiapp_s")]


def test_id_token_nonce_mismatch_is_rejected(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_n", nonce="other-nonce")
    out = siwc_manager.complete(
        started["attempt_id"], _callback_url(started, code="c", state=q["state"], client_id="oaiapp_n")
    )
    assert out["ok"] is False
    assert "nonce" in out["error"]
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-siwc", "oaiapp_n")]


@pytest.mark.parametrize(
    "value, usable",
    [
        ("eyJ.abc-_~+/=.sig", True),
        ("tok\n", False),
        ("tok\r\nX-Injected: 1", False),
        ("has space", False),
        ("tab\there", False),
        ("ünicode", False),
        ("", False),
        (123, False),
        (None, False),
    ],
)
def test_is_usable_token_accepts_only_header_safe_strings(value, usable):
    assert oca.is_usable_token(value) is usable


@pytest.mark.parametrize("field", ["access_token", "refresh_token"])
def test_validation_rejects_tokens_with_line_breaks(field):
    nonce = "n-1"
    data = _exchange_payload(nonce, "oaiapp_c")
    data[field] = data[field] + "\r\n"
    with pytest.raises(oca.SiwcOAuthError):
        oca._validate_siwc_exchange(data, "oaiapp_c", nonce)


@pytest.mark.parametrize(
    "refreshed",
    [
        {"access_token": "ok-token\n", "refresh_token": "rt-new"},
        {"access_token": "ok-token", "refresh_token": "rt new"},
    ],
)
def test_siwc_refresh_response_with_line_breaks_is_refused(refreshed):
    with pytest.raises(RuntimeError) as excinfo:
        OpenAICodexClient._check_siwc_refresh_response(refreshed)
    assert "ok-token" not in str(excinfo.value)
    assert "rt-new" not in str(excinfo.value)


def test_unsendable_bearer_header_error_never_carries_the_token(isolated_stores, monkeypatch):
    """httpx の送信エラーはヘッダー全文を含むので、その文面を外へ出さない。"""
    secret = "SECRET-TOKEN-VALUE"
    _write_json(
        isolated_stores["saiverse"],
        {
            "source": oca.SIWC_SOURCE,
            "tokens": {
                "access_token": secret,
                "refresh_token": "rt-siwc",
                "client_id": "oaiapp_c",
                "scope": oca.SIWC_SCOPE,
            },
        },
    )

    class _ExplodingSession:
        def build_request(self, method, url, **kwargs):
            return object()

        def send(self, request, stream=False):
            raise httpx.LocalProtocolError(f"Illegal header value b'Bearer {secret}'")

    client = OpenAICodexClient(model="gpt-6.1-sol")
    monkeypatch.setattr(client, "_ensure_session", lambda is_siwc=False: _ExplodingSession())
    body = {"model": "gpt-6.1-sol", "input": [], "stream": True, "store": False}
    with pytest.raises(RuntimeError) as excinfo:
        client._post_responses(body)
    assert secret not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_redact_secrets_blanks_tokens_but_keeps_error_reasons():
    jwt = _siwc_access_token()
    body = (
        '{"access_token": "at-SECRET-1", "refresh_token":"rt-SECRET-2", '
        '"error": {"code": "deviceauth_authorization_pending"}} '
        f"refresh_token=rt-SECRET-3&x=1 bearer {jwt} echoed sent-SECRET-4"
    )
    out = oca.redact_secrets(body, "sent-SECRET-4")
    for secret in ("at-SECRET-1", "rt-SECRET-2", "rt-SECRET-3", jwt, "sent-SECRET-4"):
        assert secret not in out
    assert "deviceauth_authorization_pending" in out


def test_describe_response_redacts_before_truncating():
    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = '{"access_token": "' + "A" * 300 + '", "refresh_token": "rt-SECRET"'

    out = oca._describe_response(_Resp())
    assert "AAAA" not in out
    assert "rt-SECRET" not in out


@pytest.mark.parametrize(
    "body",
    [
        '{"refresh_token":"rt-SECRET-truncated',  # cut mid-string
        '{"detail": "{\\"access_token\\": \\"at-SECRET-escaped\\"}"}',  # escaped field
        '{"error": "invalid_grant", "error_description": "opaque-SECRET-echo"}',
        "<html>opaque-SECRET-page</html>",
    ],
)
def test_auth_server_bodies_never_leave_more_than_an_error_code(body):
    class _Resp:
        status_code = 400
        headers = {"content-type": "application/json"}
        text = body

    out = oca._describe_response(_Resp())
    assert "SECRET" not in out
    assert "SECRET" not in oca.summarize_error_body(body)


def test_summarize_error_body_keeps_identifier_codes():
    assert "error=invalid_grant" in oca.summarize_error_body('{"error": "invalid_grant"}')
    assert "error=deviceauth_authorization_pending" in oca.summarize_error_body(
        '{"error": {"code": "deviceauth_authorization_pending", "message": "x"}}'
    )
    assert "error=?" in oca.summarize_error_body('{"error": "has space SECRET"}')


def test_callback_error_description_is_not_reflected(isolated_stores, fake_auth_http, siwc_manager):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(
            started, error="server_error", error_description="desc-SECRET-jwt", state=q["state"]
        ),
    )
    assert out["ok"] is False
    assert "desc-SECRET" not in out["error"]
    assert "desc-SECRET" not in out["message"]
    assert "server_error" in out["error"]


def test_exchange_transport_error_does_not_carry_code_or_verifier(
    isolated_stores, fake_auth_http, siwc_manager
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])

    def boom(url, kwargs):
        data = kwargs["data"]
        raise httpx.ConnectError(f"failed with {data['code']} {data['code_verifier']}")

    fake_auth_http.post_handler = boom
    verifier = siwc_manager._attempt.verifier
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="code-SECRET-1", state=q["state"], client_id="oaiapp_c"),
    )
    assert out["ok"] is False
    assert "code-SECRET-1" not in out["error"]
    assert verifier not in out["error"]


def test_stream_failure_event_is_scrubbed_of_the_sent_token():
    client = OpenAICodexClient(model="gpt-6.1-sol")
    client._last_sent_token = "SENT-TOKEN-SECRET-9"
    failed = {
        "type": "response.failed",
        "response": {"error": {"code": "server_error", "message": "echo SENT-TOKEN-SECRET-9 " + _siwc_access_token()}},
    }

    class _Resp:
        def iter_content(self):
            yield ("data: " + json.dumps(failed) + "\n\n").encode()

    with pytest.raises(RuntimeError) as excinfo:
        list(client._iter_chunks(_Resp()))
    text = str(excinfo.value)
    assert "SENT-TOKEN-SECRET-9" not in text
    assert "eyJ" not in text
    assert "server_error" in text


def test_stored_token_with_line_break_is_refused_without_echo(isolated_stores):
    secret = "stored-SECRET\r\nX-Injected: 1"
    _write_json(
        isolated_stores["saiverse"],
        {"source": "saiverse_device_login", "tokens": {"access_token": secret, "refresh_token": "rt"}},
    )
    with pytest.raises(RuntimeError) as excinfo:
        OpenAICodexClient(model="gpt-6.1-sol")._resolve_connection_params()
    assert "stored-SECRET" not in str(excinfo.value)


def test_cli_store_token_with_line_break_is_refused(isolated_stores):
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": "cli tok", "refresh_token": "rt"}})
    with pytest.raises(RuntimeError):
        OpenAICodexClient(model="gpt-6.1-sol")._resolve_connection_params()


def test_legacy_refresh_with_unusable_token_never_overwrites_the_store(isolated_stores, monkeypatch):
    original = {
        "source": "saiverse_device_login",
        "tokens": {"access_token": _legacy_access_token(expires_in_seconds=-60), "refresh_token": "rt-old"},
    }
    _write_json(isolated_stores["saiverse"], original)
    client = OpenAICodexClient(model="gpt-6.1-sol")
    monkeypatch.setattr(
        client, "_request_refresh", lambda rt: {"access_token": "bad\ntoken", "refresh_token": "rt-new"}
    )
    with pytest.raises(RuntimeError):
        client._refresh_or_pickup_latest(None)
    assert _read_json(isolated_stores["saiverse"])["tokens"] == original["tokens"]


def test_http_error_body_echoing_the_sent_token_is_redacted(isolated_stores, monkeypatch):
    secret = "SENT-TOKEN-VALUE-123"
    _write_json(
        isolated_stores["saiverse"],
        {
            "source": oca.SIWC_SOURCE,
            "tokens": {"access_token": secret, "refresh_token": "rt", "client_id": "oaiapp_c",
                       "scope": oca.SIWC_SCOPE},
        },
    )

    class _ErrResp:
        status_code = 401
        ok = False
        headers = {}

        def iter_content(self):
            yield f"invalid token {secret}".encode()

        def close(self):
            pass

    class _Session:
        def build_request(self, method, url, **kwargs):
            return object()

        def send(self, request, stream=False):
            return object()

    client = OpenAICodexClient(model="gpt-6.1-sol")
    monkeypatch.setattr(client, "_ensure_session", lambda is_siwc=False: _Session())
    monkeypatch.setattr(codex_mod, "_HttpxResponseAdapter", lambda raw: _ErrResp())
    with pytest.raises(codex_mod.CodexHTTPError) as excinfo:
        client._post_responses({"model": "m", "input": [], "stream": True, "store": False})
    assert secret not in str(excinfo.value)
    assert secret not in excinfo.value.body


def test_id_token_aud_must_contain_issued_client_id():
    data = _exchange_payload("n1", "oaiapp_someone_else")
    with pytest.raises(oca.SiwcOAuthError, match="aud"):
        oca._validate_siwc_exchange(data, "oaiapp_me", "n1")


def test_non_string_tokens_are_rejected_as_oauth_errors_not_type_errors():
    for value in (None, 123, ["a.b.c"], {"a": 1}, ""):
        assert oca.decode_jwt_payload(value) is None

    data = _exchange_payload("n1", "oaiapp_me")
    data["id_token"] = 12345
    with pytest.raises(oca.SiwcOAuthError, match="ID トークン"):
        oca._validate_siwc_exchange(data, "oaiapp_me", "n1")

    data = _exchange_payload("n1", "oaiapp_me")
    data["scope"] = None
    data["access_token"] = _jwt({"scope": 42})  # no usable scope claim either
    with pytest.raises(oca.SiwcOAuthError, match="chatgpt.tokens.use.direct"):
        oca._validate_siwc_exchange(data, "oaiapp_me", "n1")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("access_token", 123),
        ("access_token", ""),
        ("access_token", None),
        ("refresh_token", None),
        ("refresh_token", 123),
        ("refresh_token", ""),
        ("refresh_token", ["rt"]),
    ],
)
def test_validation_requires_string_access_and_refresh_tokens(field, value):
    data = _exchange_payload("n1", "oaiapp_me")
    if value is None:
        del data[field]
    else:
        data[field] = value
    with pytest.raises(oca.SiwcOAuthError, match=field):
        oca._validate_siwc_exchange(data, "oaiapp_me", "n1")


def _existing_siwc_login(path: Path) -> dict:
    record = {
        "source": oca.SIWC_SOURCE,
        "tokens": {
            "access_token": _siwc_access_token(client_id="oaiapp_old"),
            "refresh_token": "rt-existing",
            "client_id": "oaiapp_old",
            "scope": oca.SIWC_SCOPE,
        },
    }
    _write_json(path, record)
    return record


def test_exchange_with_non_string_access_token_keeps_the_store_and_revokes(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    existing = _existing_siwc_login(isolated_stores["saiverse"])
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    nonce = q["nonce"]

    def handler(url, kwargs):
        payload = _exchange_payload(nonce, "oaiapp_x")
        payload["access_token"] = 123
        return FakeResponse(200, payload)

    fake_auth_http.post_handler = handler
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_x"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "access_token" in out["error"]
    assert _read_json(isolated_stores["saiverse"]) == existing
    assert revocations == [("rt-siwc", "oaiapp_x")]


@pytest.mark.parametrize("refresh_token", [None, 123, ""])
def test_exchange_without_a_usable_refresh_token_keeps_the_store(
    isolated_stores, fake_auth_http, siwc_manager, revocations, refresh_token
):
    existing = _existing_siwc_login(isolated_stores["saiverse"])
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    nonce = q["nonce"]

    def handler(url, kwargs):
        payload = _exchange_payload(nonce, "oaiapp_y")
        if refresh_token is None:
            del payload["refresh_token"]
        else:
            payload["refresh_token"] = refresh_token
        return FakeResponse(200, payload)

    fake_auth_http.post_handler = handler
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_y"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "refresh_token" in out["error"]
    assert _read_json(isolated_stores["saiverse"]) == existing
    assert revocations == []  # no usable refresh token to revoke


def test_exchange_200_without_access_token_revokes_the_minted_refresh_token(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = lambda url, kw: FakeResponse(
        200, {"refresh_token": "rt-orphan", "token_type": "Bearer"}
    )
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_a"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "access_token" in out["error"]
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-orphan", "oaiapp_a")]


def test_non_string_id_token_fails_the_attempt_and_revokes(
    isolated_stores, fake_auth_http, siwc_manager, revocations
):
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    nonce = q["nonce"]

    def handler(url, kwargs):
        payload = _exchange_payload(nonce, "oaiapp_t")
        payload["id_token"] = 12345
        return FakeResponse(200, payload)

    fake_auth_http.post_handler = handler
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_t"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-siwc", "oaiapp_t")]


def test_unexpected_validation_crash_still_fails_and_revokes(
    isolated_stores, fake_auth_http, siwc_manager, revocations, monkeypatch
):
    """検証で SiwcOAuthError 以外が起きても、発行済みの鍵を残さない。"""
    def crash(data, issued_client_id, nonce):
        raise TypeError("unexpected shape")

    monkeypatch.setattr(oca, "_validate_siwc_exchange", crash)
    started = siwc_manager.start()
    q = _query(started["auth_url"])
    fake_auth_http.post_handler = _exchange_handler(started, "oaiapp_v")
    out = siwc_manager.complete(
        started["attempt_id"],
        _callback_url(started, code="c", state=q["state"], client_id="oaiapp_v"),
    )
    assert out["ok"] is False
    assert out["state"] == oca.SIWC_STATE_ERROR
    assert "TypeError" in out["error"]
    assert "unexpected shape" not in out["error"]
    assert not isolated_stores["saiverse"].exists()
    assert revocations == [("rt-siwc", "oaiapp_v")]


def test_revoke_never_raises_even_if_the_http_client_cannot_be_created(monkeypatch):
    def no_client():
        raise RuntimeError("cannot build client")

    monkeypatch.setattr(oca, "_http_session", no_client)
    assert oca.revoke_siwc_refresh_token("rt", "oaiapp_x") is False

    closed: list = []

    class BrokenSession:
        def get(self, url, **kwargs):
            raise httpx.ConnectError("offline")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(oca, "_http_session", BrokenSession)
    assert oca.revoke_siwc_refresh_token("rt", "oaiapp_x") is False
    assert closed == [True]


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


def test_logout_revokes_siwc_refresh_token_after_deleting_outside_locks(
    isolated_stores, fake_auth_http, siwc_manager
):
    _siwc_store(isolated_stores["saiverse"])
    revoke_url = "https://auth.openai.com/oauth/revoke"
    store_present_at_revoke: list = []
    locks_free_at_revoke: list = []

    def locks_are_free() -> bool:
        # The cross-process store lock must be acquirable right now ...
        probe = oca.saiverse_store_lock(timeout=0)
        try:
            probe.acquire()
        except Exception:  # noqa: BLE001
            return False
        probe.release()
        # ... and so must the manager lock (status polling never waits on I/O).
        if not siwc_manager._lock.acquire(blocking=False):
            return False
        siwc_manager._lock.release()
        return True

    fake_auth_http.get_handler = lambda url, kw: (
        FakeResponse(200, {"revocation_endpoint": revoke_url})
        if url == oca.SIWC_OPENID_CONFIGURATION_URL else pytest.fail(url)
    )

    def revoke(url, kwargs):
        assert url == revoke_url
        store_present_at_revoke.append(isolated_stores["saiverse"].exists())
        locks_free_at_revoke.append(locks_are_free())
        return FakeResponse(200, None)

    fake_auth_http.post_handler = revoke
    result = oca.logout_all_and_delete_store([siwc_manager])
    assert result == {"removed": True, "revoked": True}
    # Revocation runs after the file is gone (no refresher can rotate the token
    # again) and while no lock is held (network I/O never blocks other
    # processes' refresh / login on the store lock).
    assert store_present_at_revoke == [False]
    assert locks_free_at_revoke == [True]
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


def test_no_logout_entry_point_skips_marker_or_revocation(isolated_stores, revocations):
    """F: 印も失効も飛ばす削除口は存在せず、どの manager からのログアウトも両方式を止める。"""
    assert not hasattr(oca, "delete_saiverse_store")
    assert "delete_saiverse_store" not in oca.__all__

    # from a device manager: itself + the module-level SIWC manager
    device = oca.CodexDeviceLoginManager()
    device_gen, siwc_gen = device._generation, oca.SIWC_LOGIN_MANAGER._generation
    marker_before = oca.read_logout_marker()
    _siwc_store(isolated_stores["saiverse"])
    assert device.logout_and_delete_store() is True
    assert device._generation == device_gen + 1
    assert oca.SIWC_LOGIN_MANAGER._generation == siwc_gen + 1
    assert oca.read_logout_marker() != marker_before
    assert revocations == [("rt-to-revoke", "oaiapp_rv")]
    assert not isolated_stores["saiverse"].exists()

    # from a SIWC manager: the module-level device manager + itself
    siwc = oca.SiwcOAuthManager(callback_port=0)
    login_gen, own_gen = oca.LOGIN_MANAGER._generation, siwc._generation
    assert siwc.logout_and_delete_store() is False
    assert oca.LOGIN_MANAGER._generation == login_gen + 1
    assert siwc._generation == own_gen + 1


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


def _failing_persist(*_args, **_kwargs):
    raise OSError("disk full")


def test_siwc_refresh_write_failure_revokes_the_new_refresh_token_after_the_lock(
    isolated_stores, monkeypatch
):
    """再発行は成功したのに書き戻しに失敗: 古い鍵は使用済みなので、新しい鍵を失効させる。"""
    old_access = _siwc_access_token(client_id="oaiapp_w", expires_in_seconds=-100)
    _write_json(
        isolated_stores["saiverse"],
        {"source": oca.SIWC_SOURCE,
         "tokens": {"access_token": old_access, "refresh_token": "rt-old", "client_id": "oaiapp_w"}},
    )
    _capture_httpx_post(
        monkeypatch, {"access_token": _siwc_access_token(client_id="oaiapp_w"), "refresh_token": "rt-new"}
    )
    monkeypatch.setattr(OpenAICodexClient, "_persist_refreshed_tokens", staticmethod(_failing_persist))
    revoked: list = []

    def fake_revoke(refresh_token, client_id):
        # The store lock must already be released (no network I/O under it).
        probe = oca.saiverse_store_lock(timeout=0)
        probe.acquire()
        probe.release()
        revoked.append((refresh_token, client_id))
        return True

    monkeypatch.setattr(codex_mod, "revoke_siwc_refresh_token", fake_revoke)
    with pytest.raises(OSError, match="disk full"):
        OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)
    assert revoked == [("rt-new", "oaiapp_w")]
    assert _read_json(isolated_stores["saiverse"])["tokens"]["refresh_token"] == "rt-old"


@pytest.mark.parametrize(
    ("response", "expected_revoked"),
    [
        ({"access_token": 123, "refresh_token": "rt-new"}, [("rt-new", "oaiapp_r")]),
        ({"access_token": "", "refresh_token": "rt-new"}, [("rt-new", "oaiapp_r")]),
        ({"refresh_token": "rt-new"}, [("rt-new", "oaiapp_r")]),
        ({"access_token": 123, "refresh_token": 456}, []),
        ({"access_token": "a.b.c", "refresh_token": 456}, []),
        (["not", "a", "dict"], []),
    ],
)
def test_siwc_refresh_with_unusable_tokens_never_overwrites_the_store(
    isolated_stores, monkeypatch, response, expected_revoked
):
    """再発行の応答の鍵が文字列でなければ、ストアを書き換えず、使える新しい鍵だけ失効させる。"""
    old_access = _siwc_access_token(client_id="oaiapp_r", expires_in_seconds=-100)
    record = {
        "source": oca.SIWC_SOURCE,
        "tokens": {"access_token": old_access, "refresh_token": "rt-old", "client_id": "oaiapp_r"},
    }
    _write_json(isolated_stores["saiverse"], record)
    _capture_httpx_post(monkeypatch, response)
    revoked: list = []
    monkeypatch.setattr(
        codex_mod, "revoke_siwc_refresh_token",
        lambda rt, cid: revoked.append((rt, cid)) or True,
    )
    with pytest.raises(RuntimeError, match="ログインし直して"):
        OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)
    assert _read_json(isolated_stores["saiverse"]) == record
    assert revoked == expected_revoked


def test_legacy_refresh_write_failure_revokes_nothing(isolated_stores, monkeypatch):
    old_access = _legacy_access_token(expires_in_seconds=-100)
    _write_json(isolated_stores["cli"], {"tokens": {"access_token": old_access, "refresh_token": "rt-old"}})
    _capture_httpx_post(monkeypatch, {"access_token": _legacy_access_token(), "refresh_token": "rt-new"})
    monkeypatch.setattr(OpenAICodexClient, "_persist_refreshed_tokens", staticmethod(_failing_persist))
    monkeypatch.setattr(
        codex_mod, "revoke_siwc_refresh_token",
        lambda *a: pytest.fail("the legacy refresh path never revokes"),
    )
    with pytest.raises(OSError, match="disk full"):
        OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=old_access)


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


def _source_marked_siwc_store(path: Path, expires_in_seconds: int = 3600) -> str:
    """A record written by the SIWC login whose tokens no longer look like SIWC."""
    access = _legacy_access_token(expires_in_seconds=expires_in_seconds)
    _write_json(
        path,
        {
            "source": oca.SIWC_SOURCE,
            "tokens": {
                "access_token": access,
                "refresh_token": "rt-marked",
                "client_id": "app_issued_unprefixed",
            },
        },
    )
    return access


def test_source_marker_routes_to_siwc_even_if_tokens_look_legacy(isolated_stores, monkeypatch):
    access = _source_marked_siwc_store(isolated_stores["saiverse"], expires_in_seconds=-100)
    record = _read_json(isolated_stores["saiverse"])
    assert oca.is_siwc_token(record["tokens"]) is False  # heuristics alone say legacy
    assert oca.is_siwc_record(record) is True

    assert oca.auth_status()["is_siwc"] is True
    url, headers, is_siwc = OpenAICodexClient(model="m")._resolve_connection_params()
    assert is_siwc is True
    assert url == oca.SIWC_RESPONSES_URL
    assert "originator" not in headers

    new_scope = "openid offline_access chatgpt.tokens.use.direct"
    captured = _capture_httpx_post(
        monkeypatch,
        {"access_token": _legacy_access_token(), "refresh_token": "rt-rotated", "scope": new_scope},
    )
    OpenAICodexClient(model="m")._refresh_or_pickup_latest(prior_access_token=access)
    assert captured["url"] == oca.SIWC_TOKEN_URL  # never the legacy endpoint
    assert captured["data"]["client_id"] == "app_issued_unprefixed"
    assert captured["data"]["refresh_token"] == "rt-marked"

    stored = _read_json(isolated_stores["saiverse"])
    assert stored["source"] == oca.SIWC_SOURCE  # the marker survives the refresh
    assert stored["tokens"]["refresh_token"] == "rt-rotated"
    assert stored["tokens"]["scope"] == new_scope  # SIWC scope rotation applies


def test_logout_revokes_a_source_marked_record(isolated_stores, revocations):
    _source_marked_siwc_store(isolated_stores["saiverse"])
    result = oca.logout_all_and_delete_store([oca.SiwcOAuthManager(callback_port=0)])
    assert result == {"removed": True, "revoked": True}
    assert revocations == [("rt-marked", "app_issued_unprefixed")]


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
