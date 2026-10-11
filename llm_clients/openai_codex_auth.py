"""Codex subscription authentication: device-code login + token store resolution.

This module is the single owner of "which file holds the Codex OAuth tokens".
Two stores exist:

    1. SAIVerse's own store   ~/.saiverse/user_data/codex_auth.json  (preferred)
    2. Codex CLI's store      ~/.codex/auth.json                    (fallback)

The SAIVerse store is written by the device-code login below; the CLI store is
written by `codex login`. Invariant: when the SAIVerse store exists it is THE
auth source — we never silently fall back to the CLI store (it may hold a
different account). Refreshed tokens are always written back to the file they
were read from, because refresh_tokens are single-use: cross-writing would
leave a dead refresh_token in the other file and break whoever reads it next.

The device-code flow (verified against Hermes Agent's working implementation,
2026-08-16) uses OpenAI's device auth service:

    POST {issuer}/api/accounts/deviceauth/usercode  {client_id}
        -> {user_code, device_auth_id, interval}
    (user opens {issuer}/codex/device in any browser and enters user_code)
    POST {issuer}/api/accounts/deviceauth/token     {device_auth_id, user_code}
        -> 403/404 while pending; 200 -> {authorization_code, code_verifier}
    POST {issuer}/oauth/token  (form-encoded authorization_code grant)
        -> {access_token, refresh_token, id_token}

These endpoints are undocumented; if OpenAI changes them the CLI-store
fallback (`codex login`) remains the escape hatch.

The second login path is OpenAI's official "Sign in with ChatGPT" token
sharing for open-source apps (SIWC, developers.openai.com/siwc):

    browser → {issuer}/api/accounts/authorize   (PKCE S256, state, nonce,
              client_id=dynamic_agent_client on first registration, else the
              issued oaiapp_... id; ext_agent_host_id = stable per host)
    browser → http://127.0.0.1:<port>/auth/callback?code&state&client_id
              (loopback listener here, or the user pastes that URL into the
              UI when the browser runs on another device)
    POST {issuer}/api/accounts/oauth/token  (form, authorization_code grant)
        -> {access_token, refresh_token, id_token, scope, ...}

SIWC tokens are used against https://api.openai.com/v1/responses. The host id
and the issued client id live in their own small file
(`codex_siwc_client.json`) that survives logout, so re-authorization reuses
the same registration.

Both login managers share one attempt-generation / lease / logout-marker
discipline (`_LoginAttemptManager`), so a cancelled, superseded or logged-out
attempt can never persist tokens, whichever flow it belongs to.

Intent doc: docs/intent/codex_subscription_auth.md
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import html
import json
import logging
import os
import platform
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import httpx
from filelock import FileLock

LOG = logging.getLogger("saiverse.llm_clients.openai_codex_auth")

# ---------------------------------------------------------------------------
# Shared constants (openai_codex.py imports these — single definition here)
# ---------------------------------------------------------------------------

CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/oauth/token"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLI_VERSION = "0.45.0"
CODEX_IMPERSONATE = "chrome124"

# Sign in with ChatGPT (SIWC) official open-source token-sharing constants
SIWC_AUTHORIZE_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/authorize"
SIWC_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/oauth/token"
SIWC_DYNAMIC_CLIENT_ID = "dynamic_agent_client"
SIWC_APP_NAME_HINT = "SAIVerse"
SIWC_DEFAULT_CALLBACK_PORT = 1455
SIWC_CALLBACK_PATH = "/auth/callback"
SIWC_SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
SIWC_RESOURCE = "https://api.openai.com/v1"
SIWC_DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SIWC_RESPONSES_URL = "https://api.openai.com/v1/responses"
SIWC_OPENID_CONFIGURATION_URL = f"{CODEX_OAUTH_ISSUER}/.well-known/openid-configuration"
SIWC_LOGIN_MAX_WAIT_SECONDS = 10 * 60
# How often the loopback listener wakes up to notice cancel / logout / timeout.
SIWC_LISTENER_POLL_SECONDS = 0.25
# Host id + issued client id. Lives next to the token store but is NOT removed
# by logout: the spec wants the same host id forever and the issued client id
# reused for re-authorization.
SIWC_CLIENT_FILENAME = "codex_siwc_client.json"
# Top-level `source` of a store record written by the SIWC login. Routing
# (inference endpoint, refresh endpoint, revocation) trusts this marker first.
SIWC_SOURCE = "saiverse_siwc"

DEVICE_USERCODE_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_POLL_URL = f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URL = f"{CODEX_OAUTH_ISSUER}/codex/device"
DEVICE_REDIRECT_URI = f"{CODEX_OAUTH_ISSUER}/deviceauth/callback"
DEVICE_LOGIN_MAX_WAIT_SECONDS = 15 * 60

SAIVERSE_STORE_FILENAME = "codex_auth.json"

# What the user should do when no usable auth exists. Referenced by every
# error message that used to say only "run `codex login`".
NO_AUTH_HINT = (
    "SAIVerse の設定画面 (モデル管理 > プロバイダ) で ChatGPT アカウントに"
    "ログインするか、Codex CLI で `codex login` を実行してください "
    '(CLI の場合は ~/.codex/config.toml に cli_auth_credentials_store_mode = "file" が必要)。'
)


def build_user_agent() -> str:
    arch = platform.machine() or "unknown"
    system = platform.system() or "unknown"
    release = platform.release() or "0"
    return (
        f"{CODEX_ORIGINATOR}/{CODEX_CLI_VERSION} "
        f"({system} {release}; {arch}) python-saiverse"
    )


# ---------------------------------------------------------------------------
# Store resolution
# ---------------------------------------------------------------------------

STORE_KIND_SAIVERSE = "saiverse"
STORE_KIND_CODEX_CLI = "codex_cli"


@dataclass(frozen=True)
class AuthStore:
    """A concrete token file plus which family it belongs to."""

    path: Path
    kind: str  # STORE_KIND_SAIVERSE | STORE_KIND_CODEX_CLI

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(".json.lock")


def get_saiverse_store_path() -> Path:
    """SAIVerse's own token file. Env-resolved at call time for testability."""
    env_dir = os.getenv("SAIVERSE_USER_DATA_DIR")
    if env_dir:
        return Path(env_dir) / SAIVERSE_STORE_FILENAME
    env_home = os.getenv("SAIVERSE_HOME")
    home = Path(env_home) if env_home else Path.home() / ".saiverse"
    return home / "user_data" / SAIVERSE_STORE_FILENAME


def get_cli_auth_file() -> Path:
    """Codex CLI's auth.json (honors CODEX_HOME like the CLI itself does)."""
    codex_home = os.getenv("CODEX_HOME", "").strip()
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "auth.json"


def resolve_active_store() -> Optional[AuthStore]:
    """Which token file is the auth source right now, or None if neither exists.

    The SAIVerse store wins whenever its file exists — even if its content
    turns out to be broken or expired. Falling back behind the user's back
    could switch to a different ChatGPT account (see intent doc §3-2).
    """
    saiverse_path = get_saiverse_store_path()
    if saiverse_path.exists():
        return AuthStore(path=saiverse_path, kind=STORE_KIND_SAIVERSE)
    cli_path = get_cli_auth_file()
    if cli_path.exists():
        return AuthStore(path=cli_path, kind=STORE_KIND_CODEX_CLI)
    return None


def read_auth_store(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_auth_store(path: Path, data: Dict[str, Any]) -> None:
    """Atomic write (unique tmp + rename), token file kept at 0600.

    The tmp name is unique per write so concurrent writers (login persist vs
    refresh) can never truncate each other's half-written file; whoever
    os.replace()s last wins atomically. chmod is a no-op on Windows; on POSIX
    it keeps access_token/refresh_token unreadable to other local users.

    Callers that mutate the SAIVerse store must hold `saiverse_store_lock()`
    (the refresh path in openai_codex.py holds the per-store lock already).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():  # replace failed — don't leave half-written secrets
            try:
                tmp_path.unlink()
            except OSError:
                pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# All mutations of the SAIVerse store (login persist / refresh write-back /
# logout) serialize on this one lock. The path matches AuthStore.lock_path for
# the SAIVerse store, so the refresh path in openai_codex.py takes the same
# lock without knowing about this helper.
#
# Lock order (the single statement of it; every path below follows it):
#
#     store lock  →  manager lock (`_LoginAttemptManager._lock`)
#
# The store lock may be held while a manager lock is taken (the login commit
# does exactly that), but a manager lock is NEVER held while acquiring the
# store lock. The store lock can be held for a long time by a refresh in
# openai_codex.py, which deliberately keeps it across its network round-trip
# (two processes must not spend one rotating refresh token); waiting for it
# under a manager lock would freeze status() / cancel() / logout of that flow
# for up to STORE_LOCK_TIMEOUT_SECONDS. Where the paths stand:
#
#   * `_LoginAttemptManager._commit`: store lock first, then the manager lock
#     for the re-checks, the write and the success state.
#   * `start()`: the logout-marker snapshots (which may initialize the marker
#     under the store lock) are taken outside the manager lock; the SIWC
#     `_prepare_attempt` (`ensure_siwc_client_record`, store lock) runs
#     outside the manager lock too. Both run under the per-manager START lock,
#     which only serializes start() calls — status / cancel / logout / commit
#     never take it, so waiting for the store lock under it blocks only other
#     start() calls, which would wait for that start anyway.
#   * `save_siwc_issued_client_id` / `clear_siwc_issued_client_id`: called
#     from the SIWC callback outside the manager lock.
#   * `logout_all_and_delete_store`: manager locks (device → SIWC, the only
#     place two manager locks nest) are released before the store lock.
#   * `cancel` / `complete` / `_expire` / `_fail` / `_is_stale` / `status`:
#     manager lock only, never the store lock.
STORE_LOCK_TIMEOUT_SECONDS = 30.0


def saiverse_store_lock(timeout: Optional[float] = None) -> FileLock:
    lock_path = get_saiverse_store_path().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(
        str(lock_path),
        timeout=STORE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout,
    )


# The logout marker is a persistent random nonce next to the store, replaced
# by every logout under the store lock. Login attempts snapshot it at start
# and the commit re-checks it (by equality) under the same lock before
# writing tokens. This is what carries "the user logged out" across PROCESS
# boundaries — the manager's in-memory generation/cancel-event only reaches
# attempts of its own process, but multiple SAIVerse processes may share one
# ~/.saiverse.
#
# A nonce, deliberately not a counter: a counter re-reaches old values after
# file corruption (corrupt → treated as 0 → logout writes 1 → matches an old
# snapshot of 1 → a logged-out login persists). A fresh random value per
# logout can never collide with any earlier snapshot, so corruption or
# deletion always lands on the refuse-to-persist side (fail-closed).
LOGOUT_MARKER_FILENAME = "codex_auth.logout_marker"


def _logout_marker_path() -> Path:
    return get_saiverse_store_path().with_name(LOGOUT_MARKER_FILENAME)


def read_logout_marker() -> str:
    """Current persistent logout marker.

    "" means "no logout ever happened" (file absent). An unreadable file
    yields a unique value that can never equal any snapshot — comparisons
    then fail and the login is refused (fail-closed), never silently allowed.
    """
    path = _logout_marker_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError:
        return f"unreadable:{uuid.uuid4().hex}"


def _renew_logout_marker_locked() -> None:
    """Replace the marker with a fresh nonce. Caller must hold the store lock."""
    path = _logout_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(uuid.uuid4().hex, encoding="utf-8")
    os.replace(tmp_path, path)


class LoginInvalidatedByLogout(RuntimeError):
    """A logout happened after this login attempt started — do not persist."""


def persist_saiverse_login(
    tokens: Dict[str, Any], expected_logout_marker: Optional[str] = None
) -> None:
    """Write a fresh login's tokens to the SAIVerse store (under the store lock).

    When `expected_logout_marker` is given, the write is refused with
    LoginInvalidatedByLogout if any logout (this process or another sharing
    the same ~/.saiverse) happened since the attempt snapshotted that marker.
    The re-check happens under the store lock — the same lock logout renews
    the marker under — so the two cannot interleave.
    """
    with saiverse_store_lock():
        if (
            expected_logout_marker is not None
            and read_logout_marker() != expected_logout_marker
        ):
            raise LoginInvalidatedByLogout(
                "logout happened after this login attempt started; tokens discarded"
            )
        _persist_saiverse_login_locked(tokens)


def _persist_saiverse_login_locked(tokens: Dict[str, Any]) -> None:
    """Write a fresh login's tokens to the SAIVerse store. Caller holds the store lock.

    The logout-marker check is the caller's: `persist_saiverse_login` does it
    for direct callers, `_LoginAttemptManager._commit` does it together with
    the attempt re-checks.
    """
    account_id = extract_account_id(
        tokens.get("access_token", ""), tokens.get("id_token", "")
    )
    store_tokens: Dict[str, Any] = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
    }
    if tokens.get("id_token"):
        store_tokens["id_token"] = tokens["id_token"]
    if account_id:
        store_tokens["account_id"] = account_id
    if tokens.get("client_id"):
        store_tokens["client_id"] = tokens["client_id"]
    if tokens.get("ext_agent_host_id"):
        store_tokens["ext_agent_host_id"] = tokens["ext_agent_host_id"]
    if tokens.get("scope"):
        store_tokens["scope"] = tokens["scope"]
    if tokens.get("resource"):
        store_tokens["resource"] = tokens["resource"]

    source = tokens.get("source", "saiverse_device_login")
    write_auth_store(
        get_saiverse_store_path(),
        {
            "tokens": store_tokens,
            "last_refresh": datetime.now(tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "source": source,
        },
    )


def is_siwc_token(tokens: Optional[Dict[str, Any]]) -> bool:
    """Check if the token record was issued via Sign in with ChatGPT (token sharing).

    Detection checks (any match qualifies):
        1. Explicit granted scope contains `chatgpt.tokens.use.direct`
        2. Issued `client_id` starts with `oaiapp_`
        3. Decoded JWT `scp` / `scope` claims contain `chatgpt.tokens.use.direct`
        4. Decoded JWT `client_id` starts with `oaiapp_`
    """
    if not isinstance(tokens, dict):
        return False
    scope = tokens.get("scope") or ""
    if SIWC_DIRECT_SCOPE in scope:
        return True
    client_id = tokens.get("client_id") or ""
    if isinstance(client_id, str) and client_id.startswith("oaiapp_"):
        return True
    access_token = tokens.get("access_token")
    if isinstance(access_token, str) and access_token:
        payload = decode_jwt_payload(access_token)
        if isinstance(payload, dict):
            scp = payload.get("scp") or []
            if isinstance(scp, list) and SIWC_DIRECT_SCOPE in scp:
                return True
            scope_str = payload.get("scope") or ""
            if isinstance(scope_str, str) and SIWC_DIRECT_SCOPE in scope_str:
                return True
            cid = payload.get("client_id") or ""
            if isinstance(cid, str) and cid.startswith("oaiapp_"):
                return True
    return False


def resolve_siwc_client_id(tokens: Optional[Dict[str, Any]]) -> Optional[str]:
    """The issued client id a SIWC token set must be refreshed / revoked with.

    The stored `client_id` wins; records written before it was persisted fall
    back to the access-token JWT `client_id` claim (the spec puts the issued id
    there). `dynamic_agent_client` is a registration placeholder, never a
    usable client id, so it is never returned. None means "cannot refresh —
    the user has to sign in again".
    """
    if not isinstance(tokens, dict):
        return None
    candidates = [tokens.get("client_id")]
    access_token = tokens.get("access_token")
    if isinstance(access_token, str) and access_token:
        candidates.append((decode_jwt_payload(access_token) or {}).get("client_id"))
    for candidate in candidates:
        if (
            isinstance(candidate, str)
            and candidate
            and candidate != SIWC_DYNAMIC_CLIENT_ID
        ):
            return candidate
    return None


def siwc_scope_grants_direct_use(scope: Any) -> bool:
    """Whether a granted scope (space-separated string or list) allows inference."""
    if isinstance(scope, str):
        return SIWC_DIRECT_SCOPE in scope.split()
    if isinstance(scope, (list, tuple)):
        return SIWC_DIRECT_SCOPE in scope
    return False


def is_siwc_record(auth: Optional[Dict[str, Any]]) -> bool:
    """Whether a whole auth-store record holds Sign in with ChatGPT tokens.

    The explicit marker wins: SIWC logins are written with top-level
    `"source": "saiverse_siwc"`, and such a record is SIWC no matter what its
    scope / client_id / JWT look like — a SIWC refresh token must never reach
    the legacy refresh endpoint. Any other marker (SAIVerse's device-code
    login writes `"saiverse_device_login"`) is equally decisive the other way:
    such a record is never guessed into SIWC from what its tokens look like.
    Only records without a `source` (in practice Codex CLI's auth.json) fall
    back to the token heuristics of `is_siwc_token`.

    This is the single routing decision for the inference endpoint, the
    refresh endpoint, scope rotation, logout revocation and the UI badge.
    """
    if not isinstance(auth, dict):
        return False
    source = auth.get("source")
    if source:
        return source == SIWC_SOURCE
    return is_siwc_token(auth.get("tokens"))


def _delete_store_file_locked() -> bool:
    """Remove the store file. Caller must hold the store lock.

    The lock file itself is deliberately kept — unlinking a held lock file
    lets a second process acquire a lock on a new inode and breaks mutual
    exclusion on POSIX.
    """
    path = get_saiverse_store_path()
    if path.exists():
        path.unlink()
        return True
    return False


def _read_store_record_locked() -> Dict[str, Any]:
    """The whole SAIVerse store record, {} when absent or unreadable.

    Caller must hold the store lock.
    """
    path = get_saiverse_store_path()
    if not path.exists():
        return {}
    try:
        record = read_auth_store(path)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not read SAIVerse auth store before logout: %s", exc)
        return {}
    return record if isinstance(record, dict) else {}


# ---------------------------------------------------------------------------
# SIWC client record (host id + issued client id; survives logout)
# ---------------------------------------------------------------------------

def get_siwc_client_record_path() -> Path:
    return get_saiverse_store_path().with_name(SIWC_CLIENT_FILENAME)


def read_siwc_client_record() -> Dict[str, Any]:
    """{ext_agent_host_id, client_id} — {} when absent or unreadable."""
    path = get_siwc_client_record_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("SIWC client record %s is unreadable (%s); treating as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _valid_host_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("urn:uuid:") and len(value) > 9


def _valid_issued_client_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value != SIWC_DYNAMIC_CLIENT_ID


def _is_nonempty_str(value: Any) -> bool:
    """A token field from an untrusted token response that is actually usable."""
    return isinstance(value, str) and bool(value)


def ensure_siwc_client_record() -> Tuple[str, Optional[str]]:
    """Return (ext_agent_host_id, saved issued client id or None).

    The host id is created and persisted here, i.e. BEFORE the first
    authorize request leaves this machine (the spec requires the id to be
    stable per host from the very first sign-in). Serialized on the store lock
    so two processes sharing ~/.saiverse cannot mint two different host ids.
    """
    with saiverse_store_lock():
        record = read_siwc_client_record()
        host_id = record.get("ext_agent_host_id")
        if not _valid_host_id(host_id):
            host_id = f"urn:uuid:{uuid.uuid4()}"
            record["ext_agent_host_id"] = host_id
            write_auth_store(get_siwc_client_record_path(), record)
        client_id = record.get("client_id")
    return host_id, (client_id if _valid_issued_client_id(client_id) else None)


def save_siwc_issued_client_id(client_id: str) -> None:
    """Remember the issued client id for the next (re-)authorization."""
    with saiverse_store_lock():
        record = read_siwc_client_record()
        if not _valid_host_id(record.get("ext_agent_host_id")):
            record["ext_agent_host_id"] = f"urn:uuid:{uuid.uuid4()}"
        record["client_id"] = client_id
        write_auth_store(get_siwc_client_record_path(), record)


def clear_siwc_issued_client_id(expected_client_id: Optional[str] = None) -> bool:
    """Forget the saved issued client id (the host id is kept).

    Used when the issuer rejects the saved registration (`invalid_client`),
    so the next sign-in registers afresh with `dynamic_agent_client`. With
    `expected_client_id`, only that id is cleared — an id another process
    saved in the meantime is left alone. Returns True if an id was removed.
    """
    with saiverse_store_lock():
        record = read_siwc_client_record()
        current = record.get("client_id")
        if current is None:
            return False
        if expected_client_id is not None and current != expected_client_id:
            return False
        del record["client_id"]
        write_auth_store(get_siwc_client_record_path(), record)
        return True


def revoke_siwc_refresh_token(refresh_token: str, client_id: str) -> bool:
    """Best-effort revocation at the issuer's `revocation_endpoint`.

    Returns True on the spec's success response (HTTP 200). Never raises —
    not even when the HTTP client itself cannot be created: sign-out and the
    "minted but not saved" cleanups must finish whether or not revocation
    worked, so every failure is logged as a warning (never with token values)
    and reported as False.
    """
    session = None
    try:
        session = _http_session()
        resp = session.get(SIWC_OPENID_CONFIGURATION_URL, timeout=10)
        if resp.status_code != 200:
            LOG.warning(
                "SIWC revocation skipped: openid-configuration returned status=%s",
                resp.status_code,
            )
            return False
        endpoint = (resp.json() or {}).get("revocation_endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            LOG.warning("SIWC revocation skipped: no usable revocation_endpoint")
            return False
        resp = session.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "token": refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": client_id,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        LOG.warning("SIWC refresh-token revocation failed: status=%s", resp.status_code)
        return False
    except Exception as exc:  # noqa: BLE001
        # The type only: an exception message could echo request data.
        LOG.warning("SIWC refresh-token revocation failed: %s", type(exc).__name__)
        return False
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# JWT helpers (payload decode without signature verification)
# ---------------------------------------------------------------------------

def decode_jwt_payload(token: Any) -> Optional[Dict[str, Any]]:
    """Payload claims of a JWT, or None for anything that is not a readable JWT.

    Token responses are untrusted JSON, so a "token" may be a number, a list
    or None; those are answered with None like a malformed string, never with
    a TypeError.
    """
    if not isinstance(token, str) or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def extract_account_id(access_token: str, id_token: str = "") -> Optional[str]:
    """ChatGPT account id from token claims.

    Both the access_token and the id_token carry it under the
    `https://api.openai.com/auth` claim as `chatgpt_account_id` (this is where
    Codex CLI's auth.rs and Hermes Agent read it from).
    """
    for token in (access_token, id_token):
        payload = decode_jwt_payload(token)
        if not payload:
            continue
        auth_claim = payload.get("https://api.openai.com/auth")
        if isinstance(auth_claim, dict):
            account_id = auth_claim.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    return None


def access_token_expiry(access_token: str) -> Optional[datetime]:
    payload = decode_jwt_payload(access_token)
    if not payload:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Device-code login state machine
# ---------------------------------------------------------------------------

LOGIN_STATE_IDLE = "idle"
LOGIN_STATE_STARTING = "starting"  # usercode request in flight
LOGIN_STATE_WAITING = "waiting"  # user_code issued, waiting for browser step
LOGIN_STATE_SUCCESS = "success"
LOGIN_STATE_ERROR = "error"


LOGIN_REFUSED_BY_LOGOUT_MESSAGE = (
    "ログアウトされたため、このログインは取り消されました。もう一度ログインしてください。"
)


class CodexDeviceLoginError(RuntimeError):
    pass


def _http_session() -> Any:
    """Plain httpx for the auth host — deliberately NOT curl_cffi.

    The device-auth endpoints on auth.openai.com serve the browser
    verification HTML page (200 text/html) to any client whose TLS handshake
    looks like a browser's. curl_cffi impersonates Chrome, so it gets that
    HTML instead of the JSON API response (the deviceauth/token poll returns
    the login page, not {"code":"deviceauth_authorization_pending"}), and
    every poll fails to parse as JSON. Only the inference host
    (chatgpt.com/backend-api/codex) needs the TLS impersonation to clear
    Cloudflare; the auth host must be hit as a plain API client, exactly as
    Codex CLI and Hermes Agent do. (Verified against the live endpoint
    2026-08-16: curl_cffi → 200 text/html, httpx → 403 JSON pending.)
    """
    return httpx.Client(timeout=30.0, follow_redirects=False)


def _auth_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "originator": CODEX_ORIGINATOR,
        "User-Agent": build_user_agent(),
    }


def _describe_response(resp: Any) -> str:
    """status + content-type + short body preview, for diagnosing bad responses.

    Never includes request credentials; the response body may echo an error
    message but device-auth error bodies carry no secrets.
    """
    try:
        ctype = resp.headers.get("content-type", "?")
    except Exception:  # noqa: BLE001
        ctype = "?"
    try:
        body = resp.text
    except Exception:  # noqa: BLE001
        body = "<unreadable>"
    return f"status={resp.status_code} content-type={ctype!r} body[:200]={body[:200]!r}"


class _LoginAttemptManager:
    """Attempt-generation / lease / logout-marker discipline shared by both flows.

    Subclasses supply the flow itself through four hooks:

        _prepare_attempt()                   start-time work outside the manager
                                             lock (device: usercode request;
                                             SIWC: host id + loopback listener)
        _waiting_fields(prepared)            extra fields of the WAITING state
        _worker_args(prepared)               extra args for the worker thread
        _run(generation, cancel_event, logout_marker, *worker_args)

    and may override `_discard_prepared` (a prepared attempt that went stale
    before it opened), `_on_attempt_waiting_locked` and
    `_on_attempt_aborted_locked` (cancel / logout / supersession).

    Everything that decides whether an attempt may still change state or
    persist tokens lives here, once, so the two flows cannot drift apart:
    generation bumps, per-attempt cancel events, leases, the persistent logout
    marker snapshot, and the commit that re-checks all of them under the
    store lock and the manager lock together (lock order: see
    `saiverse_store_lock`).
    """

    _flow_error: type = RuntimeError
    _thread_name_prefix = "codex-login"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._state: Dict[str, Any] = {"state": LOGIN_STATE_IDLE}
        self._generation = 0
        self._current_cancel: Optional[threading.Event] = None
        self._leases: set[str] = set()
        # The logout marker the current WAITING attempt snapshotted at its
        # start. A join compares it with the marker now: a logout in another
        # process renews the marker without touching this process's memory,
        # so without this comparison a later start() would join an attempt
        # whose commit is already doomed to be refused.
        self._attempt_logout_marker: Optional[str] = None
        # The logout marker itself is persistent (read_logout_marker): every
        # logout renews it under the store lock. start() snapshots it before
        # queueing on the start lock and re-checks after acquiring: a start
        # that was accepted before a logout but reaches the front of the queue
        # after it must NOT open a fresh attempt — the user's logout wins over
        # every start that was already in flight. (Making logout take the
        # start lock instead would not give this guarantee: lock hand-off
        # order between a queued start and the logout is arbitrary.) The same
        # snapshot travels with the attempt and is re-verified under the store
        # lock at commit, which is what makes a logout in ANOTHER process
        # sharing this ~/.saiverse kill this process's in-flight login too.

    # -- flow hooks ------------------------------------------------------------

    def _prepare_attempt(self) -> Any:
        raise NotImplementedError

    def _waiting_fields(self, prepared: Any) -> Dict[str, Any]:
        raise NotImplementedError

    def _worker_args(self, prepared: Any) -> Tuple[Any, ...]:
        raise NotImplementedError

    def _run(self, generation: int, cancel_event: threading.Event,
             logout_marker: str, *worker_args: Any) -> None:
        raise NotImplementedError

    def _discard_prepared(self, prepared: Any) -> None:
        """A prepared attempt went stale before opening. Caller holds the lock."""

    def _on_attempt_waiting_locked(
        self, generation: int, cancel_event: threading.Event,
        logout_marker: str, prepared: Any,
    ) -> None:
        """The attempt is now WAITING. Caller holds the manager lock."""

    def _on_attempt_aborted_locked(self) -> None:
        """The current attempt was cancelled or invalidated. Caller holds the lock."""

    # -- public API ---------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """Open (or join) a login attempt and return it with a fresh lease.

        While an attempt is waiting for the browser step, further start()
        calls join it: same attempt_id (and user_code / auth_url), new
        lease_id — unless a logout (typically in another process sharing this
        ~/.saiverse) renewed the logout marker after that attempt started.
        Such an attempt can never commit, so it is aborted and a fresh one is
        opened instead of handing out a lease on it.

        Raises the flow's error type when a logout raced this start, and when
        the worker thread cannot be started (the attempt then ends as an
        error with its attempt_id and its prepared resources are released).
        """
        # Snapshot before queueing on the start lock, outside the manager lock.
        # It reads the persistent marker file (initializing it under the store
        # lock on a pristine install); logout replaces that file atomically
        # (tmp + os.replace) under the store lock, so this read sees either
        # the old or the new nonce, never a torn value. The re-check below,
        # after the start lock is acquired, is what catches a logout that
        # landed while this start was queued.
        marker_before_queueing = self._snapshot_logout_marker()
        with self._start_lock:
            # Also outside the manager lock: the snapshot may take the store
            # lock (see the lock order at `saiverse_store_lock`). Reading it
            # before rather than under the manager lock changes nothing: logout
            # renews the marker outside the manager locks anyway, and a renewal
            # landing after this read is caught by the commit's re-check.
            marker_after_queueing = self._snapshot_logout_marker()
            with self._lock:
                if marker_after_queueing != marker_before_queueing:
                    raise self._flow_error(
                        "ログアウトと重なったためログインを中断しました。"
                        "もう一度お試しください。"
                    )
                if self._state.get("state") == LOGIN_STATE_WAITING:
                    if self._attempt_logout_marker == marker_before_queueing:
                        lease_id = uuid.uuid4().hex
                        self._leases.add(lease_id)
                        return {**self._state, "lease_id": lease_id}
                    # A logout happened since that attempt started: abort it
                    # (its worker exits, a SIWC listener closes) and open a
                    # fresh attempt below.
                    LOG.info(
                        "login attempt %d predates a logout; opening a new attempt "
                        "instead of joining it",
                        self._generation,
                    )
                    if self._current_cancel is not None:
                        self._current_cancel.set()
                    self._on_attempt_aborted_locked()
                self._generation += 1
                generation = self._generation
                cancel_event = threading.Event()
                self._current_cancel = cancel_event
                lease_id = uuid.uuid4().hex
                self._leases = {lease_id}
                self._state = {"state": LOGIN_STATE_STARTING, "attempt_id": generation}

            # Preparation (network round-trip / socket bind) outside the
            # manager lock so status() and cancel() stay responsive. The start
            # lock is still held: no second preparation can race this one.
            try:
                prepared = self._prepare_attempt()
            except self._flow_error:
                with self._lock:
                    if generation == self._generation:
                        self._state = {"state": LOGIN_STATE_IDLE}
                raise

            with self._lock:
                if generation != self._generation or cancel_event.is_set():
                    # Cancelled (or invalidated by logout) while preparing —
                    # never spawn the worker thread.
                    if generation == self._generation:
                        self._state = {"state": LOGIN_STATE_IDLE}
                    self._discard_prepared(prepared)
                    return {"state": LOGIN_STATE_IDLE, "attempt_id": generation}
                self._state = {
                    "state": LOGIN_STATE_WAITING,
                    "attempt_id": generation,
                    **self._waiting_fields(prepared),
                }
                self._attempt_logout_marker = marker_before_queueing
                self._on_attempt_waiting_locked(
                    generation, cancel_event, marker_before_queueing, prepared
                )
                try:
                    thread = threading.Thread(
                        target=self._run,
                        args=(
                            generation,
                            cancel_event,
                            marker_before_queueing,
                            *self._worker_args(prepared),
                        ),
                        name=f"{self._thread_name_prefix}-{generation}",
                        daemon=True,
                    )
                    thread.start()
                except Exception as exc:  # noqa: BLE001
                    # No worker will ever serve this attempt. Release what was
                    # prepared (a SIWC listener keeps its port bound otherwise),
                    # kill the generation so nothing can commit for it, and end
                    # it as an error the waiting UI can match by attempt_id —
                    # never a phantom WAITING that nobody drives.
                    LOG.exception("could not start the login worker thread")
                    cancel_event.set()
                    self._leases = set()
                    self._discard_prepared(prepared)
                    message = (
                        "ログインの待ち受けを開始できませんでした。"
                        f"もう一度お試しください。({type(exc).__name__})"
                    )
                    self._state = {
                        "state": LOGIN_STATE_ERROR,
                        "attempt_id": generation,
                        "error": message,
                    }
                    raise self._flow_error(message) from exc
                return {**self._state, "lease_id": lease_id}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def cancel(self, attempt_id: int, lease_id: str) -> bool:
        """Release one lease on the given attempt; abort it when none remain.

        A stale `attempt_id` (an older generation), an unknown `lease_id`, and
        a cancel arriving after success/error are all ignored — a delayed
        cancel from a closed modal must never stop an attempt other clients
        (or a newer modal) still hold leases on. Returns True only when the
        attempt itself was aborted.
        """
        with self._lock:
            if attempt_id != self._generation:
                return False
            if self._state.get("state") not in (LOGIN_STATE_STARTING, LOGIN_STATE_WAITING):
                return False
            if lease_id not in self._leases:
                return False
            self._leases.discard(lease_id)
            if self._leases:
                return False  # other clients still watching this attempt
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._on_attempt_aborted_locked()
            self._state = {"state": LOGIN_STATE_IDLE}
            return True

    def logout_and_delete_store(self) -> bool:
        """Logout of BOTH flows; returns True if a store file was removed.

        Delegates to `logout_all_and_delete_store` with one manager per flow:
        this manager in its own flow's slot and the module-level manager of
        the other flow, in the fixed device → SIWC lock order. A logout
        started from either manager therefore always invalidates both flows,
        renews the logout marker and revokes a SIWC refresh token — there is
        no logout entry point that skips any of these. A store-lock timeout
        propagates exactly as from `logout_all_and_delete_store`.
        """
        if isinstance(self, SiwcOAuthManager):
            managers: Tuple[_LoginAttemptManager, ...] = (LOGIN_MANAGER, self)
        else:
            managers = (self, SIWC_LOGIN_MANAGER)
        return logout_all_and_delete_store(managers)["removed"]

    def _invalidate_locked(self) -> None:
        """Kill the current attempt (logout). Caller holds the manager lock.

        Bumping the generation (and setting the cancel event) stops any
        in-flight worker from committing afterwards — without this, a login
        that completes the browser step during logout would recreate the store
        right after it was removed.
        """
        self._generation += 1
        if self._current_cancel is not None:
            self._current_cancel.set()
        self._leases = set()
        self._attempt_logout_marker = None
        self._on_attempt_aborted_locked()
        self._state = {"state": LOGIN_STATE_IDLE}

    # -- generation guards ---------------------------------------------------

    def _snapshot_logout_marker(self) -> str:
        """Snapshot the logout marker, initializing it on first use.

        Never returns "": on a pristine install the marker file is created
        (under the store lock) before the first snapshot is taken. "" must not
        exist as a legitimate snapshot value — it is what a MISSING file reads
        as, so a snapshot of "" would match again after the marker file is
        destroyed post-logout (the same ABA the nonce design removed from the
        counter). With initialization, every snapshot is a concrete nonce and
        any later destruction of the file can only compare as a mismatch.

        Separate method so tests can deterministically reproduce the
        「start が並んでいる間に logout」 race by hooking the snapshot point.
        """
        marker = read_logout_marker()
        if marker:
            return marker
        with saiverse_store_lock():
            marker = read_logout_marker()
            if not marker:
                _renew_logout_marker_locked()
                marker = read_logout_marker()
        return marker

    def _is_stale(self, generation: int, cancel_event: threading.Event) -> bool:
        with self._lock:
            return generation != self._generation or cancel_event.is_set()

    def _commit(
        self,
        generation: int,
        cancel_event: threading.Event,
        logout_marker: str,
        tokens: Dict[str, Any],
        success_state: Dict[str, Any],
    ) -> bool:
        """Persist tokens and flip to success — only as the current generation.

        Lock order is store → manager (see `saiverse_store_lock`): the store
        lock is acquired FIRST, with no manager lock held, because a refresh
        may hold it across a network round-trip; status() / cancel() / logout
        of this flow stay responsive while the commit waits for it. Only once
        the store lock is held is the manager lock taken, and then, as one
        unit under both locks:

          1. the generation and the cancel event are re-checked (a cancel,
             newer start() or logout that landed while we waited wins);
          2. the persistent logout marker is re-checked (the cross-process
             guard — logout renews it under the same store lock);
          3. the store is written (`_persist_saiverse_login_locked`, which
             does not re-acquire the store lock);
          4. the success state is set.

        So a cancel() can never slip between "check passed" and "tokens
        written", and nothing ever waits for the store lock under a manager
        lock, so this cannot deadlock with logout or start().

        Refusals:
          * stale / cancelled generation — the state belongs to someone else
            (idle after cancel, or a newer attempt), so it is not touched;
          * current generation, but a logout happened after the attempt
            started (typically in another process sharing this ~/.saiverse)
            — this attempt still owns the state, so it ends as an ERROR with
            its attempt_id: the UI watching it gets a message and a retry
            button instead of an anonymous idle it cannot match.

        A store-lock timeout propagates (filelock's Timeout) without touching
        the state, exactly like any other store-write failure.

        Returns True only when the tokens were written.
        """
        if self._is_stale(generation, cancel_event):
            # Already dead: do not queue behind a refresh for nothing.
            LOG.info("discarding tokens from stale/cancelled login attempt %d", generation)
            return False
        with saiverse_store_lock():
            with self._lock:
                if generation != self._generation or cancel_event.is_set():
                    LOG.info(
                        "discarding tokens from stale/cancelled login attempt %d",
                        generation,
                    )
                    return False
                if read_logout_marker() != logout_marker:
                    LOG.info(
                        "discarding tokens from login attempt %d: logout happened "
                        "after it started",
                        generation,
                    )
                    self._state = {
                        "state": LOGIN_STATE_ERROR,
                        "attempt_id": generation,
                        "error": LOGIN_REFUSED_BY_LOGOUT_MESSAGE,
                    }
                    return False
                _persist_saiverse_login_locked(tokens)
                self._state = success_state
        return True

    def _fail(self, generation: int, cancel_event: threading.Event, message: str) -> None:
        with self._lock:
            if generation != self._generation or cancel_event.is_set():
                return
            self._state = {
                "state": LOGIN_STATE_ERROR,
                "attempt_id": generation,
                "error": message,
            }


class CodexDeviceLoginManager(_LoginAttemptManager):
    """Runs at most one device-code login at a time, in a background thread.

    The API layer calls `start()` (returns the user_code to display) and then
    polls `status()`. Tokens never appear in any status payload.

    Every start() call either opens a new *attempt generation* (an increasing
    `attempt_id` plus a per-attempt cancel event) or joins the attempt already
    waiting for the browser step. Each start() — including joins — is handed
    its own *lease*: an opaque token identifying that client's interest in the
    attempt. cancel() releases one lease; the attempt is only aborted when its
    last lease is released. This is what makes a delayed cancel from a closed
    modal harmless to a reopened modal that joined the same attempt — the two
    hold different leases even though they share an attempt_id.

    The worker thread re-checks "am I still the current, un-cancelled
    generation" before every state update and before persisting tokens, so a
    cancelled, superseded, or logged-out attempt can never resurrect itself,
    overwrite a newer attempt, or write the auth store. A shared boolean cannot
    give this guarantee: a new start() would reset it and revive the old
    thread.

    start() calls are additionally serialized on a dedicated start lock (held
    across the usercode round-trip), so two concurrent starts can never race
    two usercode requests: the second waits, then joins the first's attempt.

    The attempt / lease / logout-marker machinery lives in
    `_LoginAttemptManager`; this class only supplies the device-code steps.
    """

    _flow_error = CodexDeviceLoginError
    _thread_name_prefix = "codex-device-login"

    # -- flow hooks ------------------------------------------------------------

    def _prepare_attempt(self) -> Dict[str, Any]:
        # A user_code issued for an attempt that goes stale before opening
        # simply expires unused on OpenAI's side — nothing to discard.
        return self._request_usercode()

    def _waiting_fields(self, prepared: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "user_code": prepared["user_code"],
            "verification_url": DEVICE_VERIFICATION_URL,
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    def _worker_args(self, prepared: Dict[str, Any]) -> Tuple[Any, ...]:
        return (prepared["device_auth_id"], prepared["user_code"], prepared["interval"])

    # -- flow steps ---------------------------------------------------------

    def _request_usercode(self) -> Dict[str, Any]:
        session = _http_session()
        try:
            resp = session.post(
                DEVICE_USERCODE_URL,
                headers=_auth_headers(),
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise CodexDeviceLoginError(f"デバイスコードの申請に失敗しました: {exc}") from exc
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

        if resp.status_code == 429:
            raise CodexDeviceLoginError(
                "OpenAI がログイン要求を一時的に制限しています (HTTP 429)。"
                "1 分ほど待ってからもう一度お試しください。"
            )
        if resp.status_code != 200:
            raise CodexDeviceLoginError(
                f"デバイスコードの申請が status={resp.status_code} で失敗しました。"
            )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("usercode response was not JSON: %s", _describe_response(resp))
            raise CodexDeviceLoginError("デバイスコード応答が JSON ではありませんでした。") from exc

        user_code = data.get("user_code") or ""
        device_auth_id = data.get("device_auth_id") or ""
        if not user_code or not device_auth_id:
            raise CodexDeviceLoginError("デバイスコード応答に必要な項目がありません。")
        try:
            interval = max(3, int(data.get("interval", 5)))
        except (TypeError, ValueError):
            interval = 5
        return {
            "user_code": user_code,
            "device_auth_id": device_auth_id,
            "interval": interval,
        }

    def _run(
        self,
        generation: int,
        cancel_event: threading.Event,
        logout_marker: str,
        device_auth_id: str,
        user_code: str,
        interval: int,
    ) -> None:
        try:
            code_resp = self._poll_for_authorization(
                generation, cancel_event, device_auth_id, user_code, interval
            )
            if code_resp is None:  # cancelled or superseded
                return
            tokens = self._exchange_code(code_resp)
            self._commit_login(generation, cancel_event, logout_marker, tokens)
        except CodexDeviceLoginError as exc:
            LOG.warning("Codex device login failed: %s", exc)
            self._fail(generation, cancel_event, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Codex device login crashed")
            self._fail(generation, cancel_event, f"予期しないエラー: {exc}")

    def _commit_login(
        self,
        generation: int,
        cancel_event: threading.Event,
        logout_marker: str,
        tokens: Dict[str, Any],
    ) -> None:
        """Persist tokens and flip to success — only as the current generation.

        See `_LoginAttemptManager._commit` for the locking contract.
        """
        account_id = extract_account_id(
            tokens.get("access_token", ""), tokens.get("id_token", "")
        )
        committed = self._commit(
            generation,
            cancel_event,
            logout_marker,
            tokens,
            success_state={
                "state": LOGIN_STATE_SUCCESS,
                "attempt_id": generation,
                "account_id": account_id,
            },
        )
        if committed:
            LOG.info("Codex device login succeeded; tokens written to SAIVerse store")

    def _poll_for_authorization(
        self,
        generation: int,
        cancel_event: threading.Event,
        device_auth_id: str,
        user_code: str,
        interval: int,
    ) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + DEVICE_LOGIN_MAX_WAIT_SECONDS
        session = _http_session()
        try:
            while time.monotonic() < deadline:
                time.sleep(interval)
                if self._is_stale(generation, cancel_event):
                    return None
                try:
                    resp = session.post(
                        DEVICE_POLL_URL,
                        headers=_auth_headers(),
                        json={"device_auth_id": device_auth_id, "user_code": user_code},
                        timeout=30,
                    )
                except Exception as exc:  # noqa: BLE001
                    LOG.debug("device login poll error (will retry): %s", exc)
                    continue
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning(
                            "poll response was not JSON: %s", _describe_response(resp)
                        )
                        raise CodexDeviceLoginError(
                            "ログイン確認の応答が JSON ではありませんでした。"
                        ) from exc
                if resp.status_code in (403, 404):
                    continue  # user hasn't finished the browser step yet
                raise CodexDeviceLoginError(
                    f"ログイン確認が status={resp.status_code} で失敗しました。"
                )
            raise CodexDeviceLoginError(
                "15 分以内にログインが完了しなかったため打ち切りました。"
                "もう一度ログインをやり直してください。"
            )
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _exchange_code(self, code_resp: Dict[str, Any]) -> Dict[str, Any]:
        authorization_code = code_resp.get("authorization_code") or ""
        code_verifier = code_resp.get("code_verifier") or ""
        if not authorization_code or not code_verifier:
            raise CodexDeviceLoginError(
                "ログイン確認の応答に authorization_code / code_verifier がありません。"
            )
        session = _http_session()
        try:
            resp = session.post(
                CODEX_REFRESH_TOKEN_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "originator": CODEX_ORIGINATOR,
                    "User-Agent": build_user_agent(),
                },
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": DEVICE_REDIRECT_URI,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise CodexDeviceLoginError(f"トークン交換に失敗しました: {exc}") from exc
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

        if resp.status_code != 200:
            raise CodexDeviceLoginError(
                f"トークン交換が status={resp.status_code} で失敗しました。"
            )
        try:
            tokens = resp.json()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("token exchange response was not JSON: %s", _describe_response(resp))
            raise CodexDeviceLoginError("トークン交換の応答が JSON ではありませんでした。") from exc
        # Untrusted JSON: a token that is not a string must never reach the
        # store (same rule as the SIWC exchange). The device flow has no
        # revocation path, so an unusable response is simply not saved.
        if not isinstance(tokens, dict) or not _is_nonempty_str(tokens.get("access_token")):
            raise CodexDeviceLoginError("トークン交換の応答に access_token がありません。")
        if tokens.get("refresh_token") is not None and not _is_nonempty_str(
            tokens.get("refresh_token")
        ):
            raise CodexDeviceLoginError("トークン交換の応答の refresh_token が不正です。")
        return tokens

# Single manager shared by the API layer.
LOGIN_MANAGER = CodexDeviceLoginManager()


# ---------------------------------------------------------------------------
# Sign in with ChatGPT (SIWC): PKCE authorize + loopback (or pasted) callback
# ---------------------------------------------------------------------------

# SIWC shares the device flow's state vocabulary (same strings on the wire).
SIWC_STATE_IDLE = LOGIN_STATE_IDLE
SIWC_STATE_WAITING = LOGIN_STATE_WAITING
SIWC_STATE_SUCCESS = LOGIN_STATE_SUCCESS
SIWC_STATE_ERROR = LOGIN_STATE_ERROR


class SiwcOAuthError(RuntimeError):
    pass


class SiwcInvalidClient(SiwcOAuthError):
    """The token endpoint answered OAuth `invalid_client` for the issued client id."""


class SiwcAudienceMismatch(SiwcOAuthError):
    """The ID token's `aud` does not contain the client id the callback named."""


class SiwcCallbackRejected(SiwcOAuthError):
    """A callback was refused without touching the attempt (stale, wrong state, ...)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _flatten_query(query: str) -> Dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items() if v}


def _shutdown_socket(conn: Any) -> None:
    """Wake any thread blocked on `conn` (recv returns EOF) — never raises."""
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close_server(server: Any) -> None:
    """End a loopback listener: the connection it is waiting on, then the socket.

    Idempotent and safe from any thread. Closing only the listening socket is
    not enough — the listener serves one connection at a time on the worker
    thread, and a client that never finishes its request line would keep the
    worker (and so the attempt) alive past cancel / logout / the deadline.
    """
    close_all = getattr(server, "close_all", None)
    if close_all is not None:
        close_all()
        return
    try:
        server.server_close()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class _SiwcAttempt:
    """Everything one SIWC authorization attempt needs, bound to its generation."""

    server: Any
    verifier: str
    state: str
    nonce: str
    redirect_uri: str
    auth_url: str
    host_id: str
    saved_client_id: Optional[str]
    generation: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    logout_marker: str = ""
    # Set (under the manager lock) when one callback has been accepted for
    # exchange or the attempt timed out — at most one callback per attempt is
    # ever processed, whether it came through the loopback or a pasted URL.
    claimed: bool = False
    done: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class _CallbackOutcome:
    processed: bool  # False = refused without changing the attempt
    ok: bool  # tokens were committed
    message: str
    http_status: int


def _callback_page(ok: bool, message: str) -> bytes:
    title = "サインインが完了しました" if ok else "サインインできませんでした"
    color = "#10a37f" if ok else "#b42318"
    hint = (
        "このタブを閉じて、SAIVerse の画面に戻ってください。"
        if ok
        else "SAIVerse の画面に戻って結果を確認してください。"
    )
    detail = "" if ok else f"<p>{html.escape(message)}</p>"
    page = (
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:sans-serif;text-align:center;padding:48px;'>"
        f"<h2 style='color:{color};'>{title}</h2>{detail}<p>{hint}</p>"
        "</body></html>"
    )
    return page.encode("utf-8")


class _SiwcCallbackHandler(BaseHTTPRequestHandler):
    """Serves /auth/callback. Any other path is a 404 that leaves the attempt alone."""

    # A connection that never sends a request line (a port probe) must not
    # pin the listener loop for long. This only bounds silence; a client
    # trickling bytes is ended by the attempt abort / deadline instead
    # (`_SiwcCallbackServer.close_all`).
    timeout = 5

    def setup(self) -> None:
        super().setup()
        register = getattr(self.server, "register_connection", None)
        if register is not None and not register(self.connection):
            # The attempt ended between accept() and here: serve nothing.
            _shutdown_socket(self.connection)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            release = getattr(self.server, "release_connection", None)
            if release is not None:
                release(self.connection)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != SIWC_CALLBACK_PATH:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # The whole request has arrived. From here on the connection carries
        # a callback being processed (a bounded exchange plus the result
        # page), so an abort no longer cuts it: the commit re-checks the
        # attempt itself, and the browser still gets the real outcome page.
        release = getattr(self.server, "release_connection", None)
        if release is not None:
            release(self.connection)
        on_callback = getattr(self.server, "on_callback", None)
        if on_callback is None:
            outcome = _CallbackOutcome(False, False, "ログイン試行の準備ができていません。", 503)
        else:
            # Runs the exchange synchronously, so the page below reflects the
            # real outcome (committed or not), not just "callback received".
            outcome = on_callback(_flatten_query(parsed.query))
        body = _callback_page(outcome.ok, outcome.message)
        self.send_response(outcome.http_status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # never log request lines: the callback URL carries the code


class _SiwcCallbackServer(HTTPServer):
    # On Windows SO_REUSEADDR lets a second socket bind a port that is already
    # listening (e.g. Codex CLI's own login server on 1455), which would split
    # callbacks between the two processes. Bind exclusively there; on POSIX
    # REUSEADDR only affects TIME_WAIT, so keep it for quick port reuse.
    allow_reuse_address = sys.platform != "win32"
    on_callback: Optional[Callable[[Dict[str, str]], _CallbackOutcome]] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Before super().__init__: a failed bind calls server_close() there.
        self._conn_lock = threading.Lock()
        # The accepted connection whose request has not fully arrived yet —
        # the one an abort must cut. None while idle or while a callback is
        # being processed.
        self.active_connection: Optional[socket.socket] = None
        self.closed = False
        super().__init__(*args, **kwargs)

    def register_connection(self, conn: socket.socket) -> bool:
        """Track a freshly accepted connection; False if the listener is closed."""
        with self._conn_lock:
            if self.closed:
                return False
            self.active_connection = conn
            return True

    def release_connection(self, conn: socket.socket) -> None:
        """Stop tracking `conn` (request complete, or the handler finished)."""
        with self._conn_lock:
            if self.active_connection is conn:
                self.active_connection = None

    def close_all(self) -> None:
        """Close the listener and cut the connection still sending its request."""
        with self._conn_lock:
            self.closed = True
            conn, self.active_connection = self.active_connection, None
        if conn is not None:
            _shutdown_socket(conn)
            try:
                if sys.platform == "win32":
                    # On Windows shutdown() does not wake a thread already
                    # blocked waiting on this socket when no more bytes come
                    # (a silent client), and socket.close() is deferred while
                    # the handler's makefile() stream still references it.
                    # Close the OS handle now: the blocked wait then fails at
                    # once and the handler ends. (On POSIX shutdown() wakes
                    # it, and closing an fd another thread waits on would
                    # risk fd reuse, so it is left to the normal close.)
                    super(socket.socket, conn).close()
                else:
                    conn.close()
            except OSError:
                pass
        try:
            self.server_close()
        except Exception:  # noqa: BLE001
            pass

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # Skip HTTPServer.server_bind: its socket.getfqdn() reverse lookup can
        # stall for seconds on Windows.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request: Any, client_address: Any) -> None:
        if self.closed:
            # A connection cut by an abort fails mid-read/write; expected.
            LOG.debug("SIWC callback connection ended by the attempt closing")
            return
        LOG.exception("SIWC callback handler error")


def _validate_siwc_exchange(
    data: Dict[str, Any], issued_client_id: str, nonce: str
) -> Dict[str, Any]:
    """Check an exchange response before anything is persisted.

    0. `access_token` and `refresh_token` must both be non-empty strings. The
       store and every later request assume strings; and SIWC always asks for
       `offline_access`, so a response without a usable refresh token cannot
       keep the login past the access token's lifetime.
    1. The granted scope must include `chatgpt.tokens.use.direct` — without it
       the tokens cannot be used for inference (the user did not grant plan
       usage). The response `scope` is authoritative; when absent we fall back
       to the access-token JWT `scope` claim.
    2. ID token `nonce` must equal this attempt's nonce and `aud` must contain
       the issued client id.

    Gap (deliberate, no JWT library in this codebase): the ID token's
    signature is NOT verified — only its claims are read. The token arrives
    directly from the issuer's token endpoint over TLS, in exchange for this
    attempt's PKCE verifier, which is what makes claim checks meaningful here
    (OIDC Core 3.1.3.7 permits TLS server validation in place of signature
    checks for tokens received this way).
    """
    if not _is_nonempty_str(data.get("access_token")):
        raise SiwcOAuthError("トークン交換の応答に使える access_token がありません。")
    if not _is_nonempty_str(data.get("refresh_token")):
        raise SiwcOAuthError(
            "トークン交換の応答に使える refresh_token がありません。"
            "このままではログインを維持できないため保存しませんでした。もう一度お試しください。"
        )
    scope = data.get("scope")
    if isinstance(scope, (list, tuple)):
        scope = " ".join(str(s) for s in scope)
    if not isinstance(scope, str) or not scope.strip():
        claim_scope = (decode_jwt_payload(data.get("access_token")) or {}).get("scope")
        scope = claim_scope if isinstance(claim_scope, str) else ""
    scope = scope.strip()
    if not siwc_scope_grants_direct_use(scope):
        raise SiwcOAuthError(
            "ChatGPT プランでの利用 (chatgpt.tokens.use.direct) が許可されませんでした。"
            "サインイン画面で SAIVerse にプランの利用を許可したか確認して、"
            "もう一度お試しください。"
        )

    # decode_jwt_payload answers None for a missing, non-string or malformed
    # token, so every one of those ends here as a SiwcOAuthError.
    claims = decode_jwt_payload(data.get("id_token"))
    if not claims:
        raise SiwcOAuthError("トークン交換の応答に読める ID トークンがありません。")
    claim_nonce = claims.get("nonce")
    if not isinstance(claim_nonce, str) or not secrets.compare_digest(
        claim_nonce.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise SiwcOAuthError("ID トークンの nonce がこのログイン試行のものと一致しません。")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else (aud if isinstance(aud, list) else [])
    if issued_client_id not in audiences:
        raise SiwcAudienceMismatch(
            "ID トークンの宛先 (aud) が SAIVerse の client_id と一致しません。"
        )

    tokens = dict(data)
    tokens["scope"] = scope
    return tokens


class SiwcOAuthManager(_LoginAttemptManager):
    """Sign in with ChatGPT login: authorize URL + callback + code exchange.

    The backend never opens a browser: start() returns `auth_url` and the UI
    shows it as a link (the user's browser may be on another device). The
    redirect lands either on the loopback listener this manager runs on
    127.0.0.1, or — when that browser is elsewhere — the user pastes the URL
    it landed on into the UI, which calls complete(). Both feed the same
    `_process_callback`, which processes at most one callback per attempt and
    commits through `_LoginAttemptManager._commit` (generation, cancel event
    and logout marker re-checked under the locks), so cancel / supersession /
    logout always win over a late callback or exchange.
    """

    _flow_error = SiwcOAuthError
    _thread_name_prefix = "saiverse-siwc-login"

    def __init__(
        self,
        callback_port: int = SIWC_DEFAULT_CALLBACK_PORT,
        max_wait_seconds: float = SIWC_LOGIN_MAX_WAIT_SECONDS,
    ) -> None:
        super().__init__()
        self._callback_port = callback_port
        self._max_wait_seconds = float(max_wait_seconds)
        self._attempt: Optional[_SiwcAttempt] = None

    # -- flow hooks ------------------------------------------------------------

    def _prepare_attempt(self) -> _SiwcAttempt:
        try:
            host_id, saved_client_id = ensure_siwc_client_record()
        except OSError as exc:
            raise SiwcOAuthError(
                f"Sign in with ChatGPT のクライアント情報を保存できませんでした: {exc}"
            ) from exc

        verifier = secrets.token_urlsafe(64)
        challenge = _base64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)

        server = self._bind_listener()
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}{SIWC_CALLBACK_PATH}"

        # First registration: dynamic_agent_client + agent_name_hint.
        # Re-authorization: the issued client id, and no name hint.
        params: Dict[str, str] = {"client_id": saved_client_id or SIWC_DYNAMIC_CLIENT_ID}
        if not saved_client_id:
            params["agent_name_hint"] = SIWC_APP_NAME_HINT
        params.update(
            {
                "ext_agent_host_id": host_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": SIWC_SCOPE,
                "resource": SIWC_RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
        )
        auth_url = (
            f"{SIWC_AUTHORIZE_URL}?"
            f"{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"
        )
        return _SiwcAttempt(
            server=server,
            verifier=verifier,
            state=state,
            nonce=nonce,
            redirect_uri=redirect_uri,
            auth_url=auth_url,
            host_id=host_id,
            saved_client_id=saved_client_id,
        )

    def _bind_listener(self) -> _SiwcCallbackServer:
        # Only the port may vary (spec); never `localhost`.
        ports = [self._callback_port] if self._callback_port == 0 else [self._callback_port, 0]
        last_exc: Optional[OSError] = None
        for port in ports:
            try:
                server = _SiwcCallbackServer(("127.0.0.1", port), _SiwcCallbackHandler)
            except OSError as exc:
                last_exc = exc
                continue
            server.timeout = SIWC_LISTENER_POLL_SECONDS
            return server
        raise SiwcOAuthError(
            f"ログインの受け口 (127.0.0.1) を開けませんでした: {last_exc}"
        )

    def _waiting_fields(self, attempt: _SiwcAttempt) -> Dict[str, Any]:
        return {
            "auth_url": attempt.auth_url,
            "redirect_uri": attempt.redirect_uri,
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
        }

    def _worker_args(self, attempt: _SiwcAttempt) -> Tuple[Any, ...]:
        return (attempt,)

    def _discard_prepared(self, attempt: _SiwcAttempt) -> None:
        _close_server(attempt.server)

    def _on_attempt_waiting_locked(
        self,
        generation: int,
        cancel_event: threading.Event,
        logout_marker: str,
        attempt: _SiwcAttempt,
    ) -> None:
        attempt.generation = generation
        attempt.cancel_event = cancel_event
        attempt.logout_marker = logout_marker
        attempt.server.on_callback = lambda params: self._process_callback(attempt, params)
        self._attempt = attempt

    def _on_attempt_aborted_locked(self) -> None:
        # Close the listener right away (cancel / logout / supersession); the
        # worker notices on its next poll and exits.
        if self._attempt is not None:
            _close_server(self._attempt.server)

    # -- public API ---------------------------------------------------------

    def complete(self, attempt_id: int, callback_url: str) -> Dict[str, Any]:
        """Manual fallback: the URL the user's browser landed on, pasted into the UI.

        Feeds the exact same validation + commit path as the loopback.
        Raises SiwcCallbackRejected (with an HTTP status) when the callback is
        refused without being processed: stale attempt_id, not a callback URL,
        state mismatch, or another callback already being processed.
        """
        with self._lock:
            attempt = self._attempt
            if (
                attempt is None
                or attempt_id != self._generation
                or attempt.generation != attempt_id
                or self._state.get("state") != LOGIN_STATE_WAITING
            ):
                raise SiwcCallbackRejected(
                    "このログイン試行は既に終了しているか、新しい試行に置き換えられています。"
                    "ログインをやり直してください。",
                    409,
                )
        parsed = urllib.parse.urlparse((callback_url or "").strip())
        if parsed.path.rstrip("/") != SIWC_CALLBACK_PATH or not parsed.query:
            raise SiwcCallbackRejected(
                "貼り付けた URL がサインイン後の転送先 (…/auth/callback?code=…) ではありません。"
                "ブラウザのアドレス欄の URL を丸ごと貼り付けてください。",
                400,
            )
        outcome = self._process_callback(attempt, _flatten_query(parsed.query))
        if not outcome.processed:
            raise SiwcCallbackRejected(outcome.message, outcome.http_status)
        return {"ok": outcome.ok, "message": outcome.message, **self.status()}

    # -- flow steps ---------------------------------------------------------

    def _run(
        self,
        generation: int,
        cancel_event: threading.Event,
        logout_marker: str,
        attempt: _SiwcAttempt,
    ) -> None:
        """Serve the loopback listener until a callback is processed or the attempt ends.

        Requests to other paths (favicon, port probes) and callbacks with the
        wrong state are answered without ending the attempt.

        The deadline is enforced by a timer of its own, not only by this
        loop: while the loop is inside handle_request() serving a client that
        trickles its request, the loop cannot look at the clock. The timer
        expires the attempt and cuts that connection, which returns the loop.
        """
        server = attempt.server
        deadline = time.monotonic() + self._max_wait_seconds
        deadline_timer = threading.Timer(self._max_wait_seconds, self._expire, args=(attempt,))
        deadline_timer.daemon = True
        deadline_timer.name = f"{self._thread_name_prefix}-{generation}-deadline"
        try:
            deadline_timer.start()
            while not attempt.done.is_set() and not self._is_stale(generation, cancel_event):
                if attempt.claimed:
                    # A pasted callback URL is being exchanged; wait for it.
                    attempt.done.wait(SIWC_LISTENER_POLL_SECONDS)
                    continue
                if time.monotonic() >= deadline:
                    self._expire(attempt)
                    continue
                try:
                    server.handle_request()  # returns after server.timeout when idle
                except (OSError, ValueError) as exc:
                    # The listening socket was closed under us — expected on
                    # cancel / logout, an error otherwise.
                    if attempt.done.is_set() or self._is_stale(generation, cancel_event):
                        return
                    raise SiwcOAuthError(f"ログインの受け口が閉じられました: {exc}") from exc
        except SiwcOAuthError as exc:
            LOG.warning("SIWC login failed: %s", exc)
            self._fail(generation, cancel_event, str(exc))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("SIWC login worker crashed")
            self._fail(generation, cancel_event, f"予期しないエラー: {exc}")
        finally:
            deadline_timer.cancel()
            _close_server(server)

    def _expire(self, attempt: _SiwcAttempt) -> None:
        """Timeout: end the attempt unless a callback is already being processed.

        Called from the deadline timer and from the worker loop (whichever
        sees the deadline first; the `claimed` flag makes it happen once).

        When a callback already claimed the attempt, the state is left to it.
        If that callback has finished, the listener is still closed here (an
        idempotent backstop to `_process_callback`'s own close), so a
        finished attempt never keeps its port or worker past the deadline.
        """
        with self._lock:
            if attempt.claimed:
                if attempt.done.is_set():
                    _close_server(attempt.server)
                return
            attempt.claimed = True
            if attempt.generation == self._generation and not attempt.cancel_event.is_set():
                self._state = {
                    "state": LOGIN_STATE_ERROR,
                    "attempt_id": attempt.generation,
                    "error": (
                        f"{int(self._max_wait_seconds // 60)} 分以内にサインインが完了しなかったため"
                        "打ち切りました。もう一度ログインをやり直してください。"
                    ),
                }
        attempt.done.set()
        # Cut a connection still sending its request and close the listener,
        # so a worker blocked in handle_request() returns now.
        _close_server(attempt.server)

    def _process_callback(
        self, attempt: _SiwcAttempt, params: Dict[str, str]
    ) -> _CallbackOutcome:
        """Single entry for loopback and pasted callbacks."""
        generation, cancel_event = attempt.generation, attempt.cancel_event
        with self._lock:
            if (
                generation != self._generation
                or cancel_event.is_set()
                or attempt.done.is_set()
            ):
                return _CallbackOutcome(
                    False, False,
                    "このログイン試行は既に終了しています。SAIVerse の画面からやり直してください。",
                    409,
                )
            received_state = params.get("state") or ""
            if not secrets.compare_digest(
                received_state.encode("utf-8"), attempt.state.encode("utf-8")
            ):
                return _CallbackOutcome(
                    False, False,
                    "state が一致しません。別のログイン試行のリンクか、改ざんされた URL の可能性があります。",
                    400,
                )
            if attempt.claimed:
                return _CallbackOutcome(
                    False, False,
                    "このログインは既に処理中です。SAIVerse の画面で結果を確認してください。",
                    409,
                )
            attempt.claimed = True
        try:
            ok, message = self._authorize_from_callback(attempt, params)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("SIWC callback processing crashed")
            message = f"予期しないエラー: {exc}"
            self._fail(generation, cancel_event, message)
            ok = False
        finally:
            # `done` first: a worker woken by the close below must see the
            # attempt as finished, never as "listener closed under us".
            attempt.done.set()
            # The attempt is over whichever path delivered the callback. Close
            # the listener and cut a connection still sending its request —
            # with a pasted URL the worker may be blocked in handle_request()
            # on such a connection (a trickling client), and nothing else
            # would end it. A loopback callback's own connection was released
            # from tracking once its request arrived, so it is not cut and
            # still receives the result page.
            _close_server(attempt.server)
        return _CallbackOutcome(True, ok, message, 200 if ok else 400)

    def _authorize_from_callback(
        self, attempt: _SiwcAttempt, params: Dict[str, str]
    ) -> Tuple[bool, str]:
        generation, cancel_event = attempt.generation, attempt.cancel_event

        def fail(message: str) -> Tuple[bool, str]:
            LOG.warning("SIWC login failed: %s", message)
            self._fail(generation, cancel_event, message)
            return False, message

        error = params.get("error")
        if error:
            # access_denied (or any other error): stop, never exchange.
            if error == "invalid_client" and attempt.saved_client_id:
                # The issuer rejected the saved registration at the authorize
                # step. Forget it (the host id stays) so the next sign-in
                # registers afresh instead of reusing a dead id forever.
                # Only `invalid_client` says the registration itself is dead;
                # `unauthorized_client` may be an account policy, and
                # forgetting the id on it would pile up duplicate
                # registrations for the same host.
                self._forget_client_id(attempt.saved_client_id)
                return fail(
                    "OpenAI が保存済みのアプリ登録 (client_id) を受け付けませんでした "
                    f"({error})。保存していた登録を消したので、次のログインでは"
                    "新しく登録し直します。もう一度ログインしてください。"
                )
            if error == "access_denied":
                return fail("ChatGPT でのサインインが拒否またはキャンセルされました。")
            return fail(f"OpenAI 認証エラー: {params.get('error_description') or error}")
        code = params.get("code")
        if not code:
            return fail("認可コードが返ってきませんでした。")

        callback_client_id = params.get("client_id") or None
        if attempt.saved_client_id:
            # Re-authorization: the client id stays the one tied to this
            # request; the callback may omit it but must not change it.
            if callback_client_id and callback_client_id != attempt.saved_client_id:
                return fail(
                    "OpenAI が返した client_id が、この SAIVerse に登録済みのものと一致しません。"
                )
            issued_client_id = attempt.saved_client_id
        else:
            if not callback_client_id:
                return fail(
                    "SAIVerse のクライアント登録が完了しませんでした "
                    "(client_id が返ってきませんでした)。もう一度お試しください。"
                )
            issued_client_id = callback_client_id
            # First registration: the callback passed the state check, so this
            # issued id is the registration OpenAI made for this host. Keep it
            # now, before the exchange — per the spec, an attempt that fails
            # after this point (e.g. invalid_grant) re-authorizes with the
            # same issued id rather than registering again.
            self._remember_client_id(issued_client_id)

        if self._is_stale(generation, cancel_event):
            return False, "このログイン試行は取り消されました。"
        try:
            data = self._exchange_code(
                issued_client_id, code, attempt.verifier, attempt.redirect_uri
            )
        except SiwcInvalidClient:
            # The issuer no longer accepts this registration. Forget it (the
            # host id stays) so the next sign-in registers afresh.
            self._forget_client_id(issued_client_id)
            return fail(
                "OpenAI がこの SAIVerse のアプリ登録 (client_id) を受け付けませんでした "
                "(invalid_client)。保存していた登録を消したので、次のログインでは"
                "新しく登録し直します。もう一度ログインしてください。"
            )
        except SiwcOAuthError as exc:
            return fail(str(exc))

        # From here on OpenAI has minted a token set. Any path that does not
        # persist it must revoke it, or a live grant is left behind that
        # nobody holds and the user cannot see.
        try:
            tokens = _validate_siwc_exchange(data, issued_client_id, attempt.nonce)
        except SiwcAudienceMismatch as exc:
            if not attempt.saved_client_id:
                # First registration: the ID token says the real client is not
                # the one the callback named, so the id saved above is wrong.
                self._forget_client_id(issued_client_id)
            result = fail(str(exc))
            self._revoke_unpersisted(data, issued_client_id)
            return result
        except SiwcOAuthError as exc:
            result = fail(str(exc))
            self._revoke_unpersisted(data, issued_client_id)
            return result
        except Exception as exc:  # noqa: BLE001
            # Validation must never be the path that leaves a minted grant
            # behind: a malformed response that trips anything else is
            # failed and revoked exactly like a rejected one. Only the type
            # is reported — the message could echo response content.
            LOG.warning(
                "SIWC exchange response could not be validated: %s", type(exc).__name__
            )
            result = fail(
                "トークン交換の応答を確認できませんでした "
                f"({type(exc).__name__})。もう一度お試しください。"
            )
            self._revoke_unpersisted(data, issued_client_id)
            return result

        tokens.update(
            {
                "client_id": issued_client_id,
                "ext_agent_host_id": attempt.host_id,
                "resource": SIWC_RESOURCE,
                "source": SIWC_SOURCE,
            }
        )
        account_id = extract_account_id(
            tokens.get("access_token", ""), tokens.get("id_token", "")
        )
        try:
            committed = self._commit(
                generation,
                cancel_event,
                attempt.logout_marker,
                tokens,
                success_state={
                    "state": LOGIN_STATE_SUCCESS,
                    "attempt_id": generation,
                    "account_id": account_id,
                },
            )
        except Exception:
            # Usually the store lock timed out and nothing was written. But an
            # error can also surface after the file was replaced (e.g. while
            # releasing the lock), so revoke only if the store does not hold
            # this token set — revoking saved tokens would kill the login.
            if not self._store_holds_refresh_token(tokens.get("refresh_token")):
                self._revoke_unpersisted(tokens, issued_client_id)
            raise
        if not committed:
            # Cancelled, superseded or logged out: revoke outside every lock.
            self._revoke_unpersisted(tokens, issued_client_id)
            return False, "このログイン試行は取り消されたため、トークンは保存しませんでした。"
        LOG.info("SIWC login succeeded; tokens written to SAIVerse store")
        return True, "サインインが完了しました。"

    @staticmethod
    def _remember_client_id(client_id: str) -> None:
        try:
            save_siwc_issued_client_id(client_id)
        except OSError as exc:
            # Tokens carry the client id, so refresh still works; only the
            # next sign-in falls back to a fresh registration.
            LOG.warning("could not save the issued SIWC client id: %s", exc)

    @staticmethod
    def _store_holds_refresh_token(refresh_token: Any) -> bool:
        """Whether the SAIVerse store currently holds this refresh token.

        Unlocked read: only used to decide whether a failed commit had in fact
        written. When the store cannot be read we answer True, so an
        unreadable store never leads to revoking tokens that may be saved.
        """
        if not isinstance(refresh_token, str) or not refresh_token:
            return False
        path = get_saiverse_store_path()
        if not path.exists():
            return False
        try:
            stored = (read_auth_store(path).get("tokens") or {}).get("refresh_token")
        except Exception:  # noqa: BLE001
            return True
        return isinstance(stored, str) and secrets.compare_digest(
            stored.encode("utf-8"), refresh_token.encode("utf-8")
        )

    @staticmethod
    def _forget_client_id(client_id: str) -> None:
        try:
            clear_siwc_issued_client_id(client_id)
        except OSError as exc:
            LOG.warning("could not clear the rejected SIWC client id: %s", exc)

    @staticmethod
    def _revoke_unpersisted(data: Any, client_id: str) -> None:
        """Best-effort revocation of a minted token set that will not be saved.

        Called outside every lock. Never logs token values.
        """
        refresh_token = data.get("refresh_token") if isinstance(data, dict) else None
        if not isinstance(refresh_token, str) or not refresh_token:
            return
        if not revoke_siwc_refresh_token(refresh_token, client_id):
            LOG.warning(
                "SIWC login: a token set was issued but not saved, and revoking it "
                "failed; it stays valid at OpenAI until it expires or is unlinked "
                "from the ChatGPT settings"
            )

    @staticmethod
    def _exchange_code(
        client_id: str, code: str, verifier: str, redirect_uri: str
    ) -> Dict[str, Any]:
        session = _http_session()
        try:
            resp = session.post(
                SIWC_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": redirect_uri,
                    "resource": SIWC_RESOURCE,
                },
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            raise SiwcOAuthError(f"トークン交換に失敗しました: {exc}") from exc
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        if resp.status_code != 200:
            LOG.warning("SIWC token exchange failed: %s", _describe_response(resp))
            try:
                error_body = resp.json()
            except Exception:  # noqa: BLE001
                error_body = None
            if isinstance(error_body, dict) and error_body.get("error") == "invalid_client":
                raise SiwcInvalidClient(
                    f"トークン交換が invalid_client (status={resp.status_code}) で拒否されました。"
                )
            raise SiwcOAuthError(f"トークン交換が status={resp.status_code} で失敗しました。")
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SiwcOAuthError("トークン交換の応答が JSON ではありませんでした。") from exc
        if not isinstance(data, dict) or not _is_nonempty_str(data.get("access_token")):
            # The endpoint answered 200, so a refresh token it did mint is a
            # live grant nobody will hold: revoke it before giving up (only a
            # usable string is sent; `_revoke_unpersisted` skips anything else).
            SiwcOAuthManager._revoke_unpersisted(data, client_id)
            raise SiwcOAuthError("トークン交換の応答に access_token がありません。")
        return data


SIWC_LOGIN_MANAGER = SiwcOAuthManager()


# ---------------------------------------------------------------------------
# Logout (both flows)
# ---------------------------------------------------------------------------

def logout_all_and_delete_store(
    managers: Optional[Sequence[_LoginAttemptManager]] = None,
) -> Dict[str, Any]:
    """Logout: stop every in-flight login, delete the store, revoke SIWC tokens.

    Three phases, and no phase holds the locks of another:

    1. Under the managers' locks only (always taken in the same order:
       device, then SIWC — no other path holds two manager locks) every
       in-flight attempt is invalidated: generation bumped, cancel event set,
       leases cleared, a SIWC listener (and the connection it is serving)
       closed. The manager locks are then RELEASED.
    2. Under the store lock only, the persistent logout marker is renewed,
       the store record is read and the store file is deleted. Renewing the
       marker kills attempts in OTHER processes sharing this ~/.saiverse too
       (their commits re-check the marker under the store lock).
    3. Outside every lock, a SIWC refresh token is revoked best-effort at the
       issuer's revocation endpoint (failure is logged and reported as
       revoked=False). Revoking after the deletion is safe: refresh re-reads
       the store file under the store lock, so once the file is gone no
       refresher can rotate the captured token again — the token we revoke
       is the live one.

    Why phase 2 does not need the manager locks: the in-process attempts are
    already dead after phase 1 (their commits re-check the generation under
    the manager lock). A start() that slips in between phase 1 and phase 2
    snapshots the OLD marker, so its commit is refused by the renewed marker
    and the attempt ends in the logout error state; a start() after phase 2
    is a legitimate new login. And since the manager locks are free while
    phase 2 waits for the store lock — which a refresh deliberately holds
    across its network round-trip (rotating refresh tokens must not be spent
    by two processes) — status polling and cancel never block behind a slow
    refresh.

    If the store lock cannot be acquired in time, filelock's Timeout
    propagates to the caller; the attempts were already invalidated in
    phase 1, and the store is left as it was (the API maps this to 503).

    Returns {removed: bool, revoked: True | False | None}; None means there
    was no SIWC token to revoke. Never touches ~/.codex. The SIWC client
    record (host id + issued client id) is deliberately kept for the next
    sign-in.
    """
    if managers is None:
        managers = (LOGIN_MANAGER, SIWC_LOGIN_MANAGER)
    # Phase 1: manager locks only.
    with contextlib.ExitStack() as manager_locks:
        for manager in managers:
            manager_locks.enter_context(manager._lock)
        for manager in managers:
            manager._invalidate_locked()

    # Phase 2: store lock only.
    with saiverse_store_lock():
        _renew_logout_marker_locked()
        record = _read_store_record_locked()
        removed = _delete_store_file_locked()

    # Phase 3: outside every lock from here on.
    revoked: Optional[bool] = None
    if is_siwc_record(record):
        tokens = record.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        refresh_token = tokens.get("refresh_token")
        client_id = resolve_siwc_client_id(tokens)
        if isinstance(refresh_token, str) and refresh_token and client_id:
            revoked = revoke_siwc_refresh_token(refresh_token, client_id)
        else:
            revoked = False
        if not revoked:
            LOG.warning(
                "SIWC sign-out: the refresh token was not revoked at OpenAI; "
                "local tokens were cleared anyway"
            )
    return {"removed": removed, "revoked": revoked}


# ---------------------------------------------------------------------------
# Status for the UI
# ---------------------------------------------------------------------------

def auth_status() -> Dict[str, Any]:
    """What the settings UI shows: which store is active and its health.

    Never includes token values.
    """
    store = resolve_active_store()
    cli_available = get_cli_auth_file().exists()
    if store is None:
        return {"logged_in": False, "store": None, "cli_available": cli_available}
    try:
        data = read_auth_store(store.path)
    except Exception as exc:  # noqa: BLE001
        return {
            "logged_in": False,
            "store": store.kind,
            "cli_available": cli_available,
            "error": f"トークンファイルを読めませんでした: {exc}",
        }
    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token") or ""
    expiry = access_token_expiry(access_token)
    return {
        "logged_in": bool(access_token),
        "store": store.kind,
        "is_siwc": is_siwc_record(data),
        "cli_available": cli_available,
        "account_id": tokens.get("account_id")
        or extract_account_id(access_token, tokens.get("id_token") or ""),
        "access_token_expires_at": expiry.isoformat() if expiry else None,
        "has_refresh_token": bool(tokens.get("refresh_token")),
    }


__all__ = [
    "CODEX_OAUTH_ISSUER",
    "CODEX_OAUTH_CLIENT_ID",
    "CODEX_REFRESH_TOKEN_URL",
    "CODEX_ORIGINATOR",
    "CODEX_CLI_VERSION",
    "CODEX_IMPERSONATE",
    "SIWC_AUTHORIZE_URL",
    "SIWC_TOKEN_URL",
    "SIWC_DYNAMIC_CLIENT_ID",
    "SIWC_APP_NAME_HINT",
    "SIWC_DEFAULT_CALLBACK_PORT",
    "SIWC_CALLBACK_PATH",
    "SIWC_SCOPE",
    "SIWC_RESOURCE",
    "SIWC_DIRECT_SCOPE",
    "SIWC_RESPONSES_URL",
    "SIWC_OPENID_CONFIGURATION_URL",
    "SIWC_CLIENT_FILENAME",
    "SIWC_SOURCE",
    "NO_AUTH_HINT",
    "AuthStore",
    "STORE_KIND_SAIVERSE",
    "STORE_KIND_CODEX_CLI",
    "build_user_agent",
    "get_saiverse_store_path",
    "get_cli_auth_file",
    "resolve_active_store",
    "read_auth_store",
    "write_auth_store",
    "saiverse_store_lock",
    "persist_saiverse_login",
    "read_logout_marker",
    "LoginInvalidatedByLogout",
    "decode_jwt_payload",
    "extract_account_id",
    "access_token_expiry",
    "is_siwc_token",
    "is_siwc_record",
    "resolve_siwc_client_id",
    "siwc_scope_grants_direct_use",
    "get_siwc_client_record_path",
    "read_siwc_client_record",
    "ensure_siwc_client_record",
    "save_siwc_issued_client_id",
    "clear_siwc_issued_client_id",
    "revoke_siwc_refresh_token",
    "CodexDeviceLoginError",
    "CodexDeviceLoginManager",
    "LOGIN_MANAGER",
    "SiwcOAuthError",
    "SiwcCallbackRejected",
    "SiwcInvalidClient",
    "SiwcAudienceMismatch",
    "SiwcOAuthManager",
    "SIWC_LOGIN_MANAGER",
    "logout_all_and_delete_store",
    "auth_status",
]
