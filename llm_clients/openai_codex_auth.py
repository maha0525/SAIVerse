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

Intent doc: docs/intent/codex_subscription_auth.md
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

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
    with saiverse_store_lock():
        if (
            expected_logout_marker is not None
            and read_logout_marker() != expected_logout_marker
        ):
            raise LoginInvalidatedByLogout(
                "logout happened after this login attempt started; tokens discarded"
            )
        write_auth_store(
            get_saiverse_store_path(),
            {
                "tokens": store_tokens,
                "last_refresh": datetime.now(tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "source": "saiverse_device_login",
            },
        )


def _delete_store_file_locked() -> bool:
    """Remove the store file. Caller must hold the store lock."""
    path = get_saiverse_store_path()
    if path.exists():
        path.unlink()
        return True
    return False


def delete_saiverse_store() -> bool:
    """Logout: remove SAIVerse's own store (never touches ~/.codex).

    Serializes on the store lock so a concurrent refresh cannot resurrect the
    file after we delete it. The lock file itself is deliberately kept —
    unlinking a held lock file lets a second process acquire a lock on a new
    inode and breaks mutual exclusion on POSIX.

    Returns True if a store file was actually removed.
    """
    with saiverse_store_lock():
        return _delete_store_file_locked()


# ---------------------------------------------------------------------------
# JWT helpers (payload decode without signature verification)
# ---------------------------------------------------------------------------

def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    if not token:
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


class CodexDeviceLoginManager:
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
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._state: Dict[str, Any] = {"state": LOGIN_STATE_IDLE}
        self._generation = 0
        self._current_cancel: Optional[threading.Event] = None
        self._leases: set[str] = set()
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

    # -- public API ---------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """Open (or join) a login attempt and return it with a fresh lease.

        While an attempt is waiting for the browser step, further start()
        calls join it: same attempt_id and user_code, new lease_id.
        Raises CodexDeviceLoginError when a logout raced this start.
        """
        # Snapshot outside any lock (single int read is atomic under the GIL).
        marker_before_queueing = self._snapshot_logout_marker()
        with self._start_lock:
            with self._lock:
                if self._snapshot_logout_marker() != marker_before_queueing:
                    raise CodexDeviceLoginError(
                        "ログアウトと重なったためログインを中断しました。"
                        "もう一度お試しください。"
                    )
                if self._state.get("state") == LOGIN_STATE_WAITING:
                    lease_id = uuid.uuid4().hex
                    self._leases.add(lease_id)
                    return {**self._state, "lease_id": lease_id}
                self._generation += 1
                generation = self._generation
                cancel_event = threading.Event()
                self._current_cancel = cancel_event
                lease_id = uuid.uuid4().hex
                self._leases = {lease_id}
                self._state = {"state": LOGIN_STATE_STARTING, "attempt_id": generation}

            # Network round-trip outside the manager lock so status() and
            # cancel() stay responsive while OpenAI is slow. The start lock is
            # still held: no second usercode request can race this one.
            try:
                device = self._request_usercode()
            except CodexDeviceLoginError:
                with self._lock:
                    if generation == self._generation:
                        self._state = {"state": LOGIN_STATE_IDLE}
                raise

            with self._lock:
                if generation != self._generation or cancel_event.is_set():
                    # Cancelled (or invalidated by logout) while the code was
                    # being issued — never spawn the poll thread. The
                    # user_code expires unused on OpenAI's side.
                    if generation == self._generation:
                        self._state = {"state": LOGIN_STATE_IDLE}
                    return {"state": LOGIN_STATE_IDLE, "attempt_id": generation}
                self._state = {
                    "state": LOGIN_STATE_WAITING,
                    "attempt_id": generation,
                    "user_code": device["user_code"],
                    "verification_url": DEVICE_VERIFICATION_URL,
                    "started_at": datetime.now(tz=timezone.utc).isoformat(),
                }
                thread = threading.Thread(
                    target=self._run,
                    args=(
                        generation,
                        cancel_event,
                        marker_before_queueing,
                        device["device_auth_id"],
                        device["user_code"],
                        device["interval"],
                    ),
                    name=f"codex-device-login-{generation}",
                    daemon=True,
                )
                thread.start()
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
            self._state = {"state": LOGIN_STATE_IDLE}
            return True

    def logout_and_delete_store(self) -> bool:
        """Logout as one critical section: invalidate any login, then delete.

        Bumping the generation (and setting the cancel event) under the
        manager lock stops any in-flight worker from committing after we
        delete — without this, a login that completes the browser step during
        logout would recreate the store right after it was removed. The store
        deletion happens while still holding the manager lock, in the same
        manager → store lock order the commit path uses, so the two cannot
        deadlock and cannot interleave.

        Renewing the persistent logout marker (under the store lock, together
        with the deletion) additionally kills every start() that was queued on
        the start lock when the logout happened — they re-check the marker
        after acquiring and abort — and every in-flight login attempt in OTHER
        processes sharing this ~/.saiverse, whose commits re-verify the marker
        under the store lock before persisting.
        """
        with self._lock:
            self._generation += 1
            if self._current_cancel is not None:
                self._current_cancel.set()
            self._leases = set()
            self._state = {"state": LOGIN_STATE_IDLE}
            with saiverse_store_lock():
                _renew_logout_marker_locked()
                return _delete_store_file_locked()

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

        The generation check and the store write happen under the manager lock
        as one unit, so a cancel() (or a newer start()) can never slip between
        "check passed" and "tokens written". The store write additionally takes
        the store file lock inside and re-verifies the persistent logout marker
        there (the cross-process guard); lock order is always manager → store,
        and no path takes them in the reverse order, so this cannot deadlock.
        """
        account_id = extract_account_id(
            tokens.get("access_token", ""), tokens.get("id_token", "")
        )
        with self._lock:
            if generation != self._generation or cancel_event.is_set():
                LOG.info(
                    "discarding tokens from stale/cancelled login attempt %d", generation
                )
                return
            try:
                persist_saiverse_login(tokens, expected_logout_marker=logout_marker)
            except LoginInvalidatedByLogout:
                # A logout (possibly in another SAIVerse process sharing this
                # ~/.saiverse) happened after this attempt started — honor it.
                LOG.info(
                    "discarding tokens from login attempt %d: logout happened "
                    "after it started",
                    generation,
                )
                self._state = {"state": LOGIN_STATE_IDLE}
                return
            self._state = {
                "state": LOGIN_STATE_SUCCESS,
                "attempt_id": generation,
                "account_id": account_id,
            }
        LOG.info("Codex device login succeeded; tokens written to SAIVerse store")

    def _fail(self, generation: int, cancel_event: threading.Event, message: str) -> None:
        with self._lock:
            if generation != self._generation or cancel_event.is_set():
                return
            self._state = {
                "state": LOGIN_STATE_ERROR,
                "attempt_id": generation,
                "error": message,
            }

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
        if not tokens.get("access_token"):
            raise CodexDeviceLoginError("トークン交換の応答に access_token がありません。")
        return tokens

# Single manager shared by the API layer.
LOGIN_MANAGER = CodexDeviceLoginManager()


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
    "delete_saiverse_store",
    "decode_jwt_payload",
    "extract_account_id",
    "access_token_expiry",
    "CodexDeviceLoginError",
    "CodexDeviceLoginManager",
    "LOGIN_MANAGER",
    "auth_status",
]
