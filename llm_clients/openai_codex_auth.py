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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from curl_cffi import requests as cffi_requests

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
    """Atomic write (tmp + rename), matching Codex CLI's pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp_path, path)


def delete_saiverse_store() -> bool:
    """Logout: remove SAIVerse's own store (never touches ~/.codex).

    Returns True if a store file was actually removed.
    """
    path = get_saiverse_store_path()
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    lock_path = path.with_suffix(".json.lock")
    if lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:  # held by a concurrent request — harmless leftover
            pass
    return removed


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
LOGIN_STATE_WAITING = "waiting"  # user_code issued, waiting for browser step
LOGIN_STATE_SUCCESS = "success"
LOGIN_STATE_ERROR = "error"


class CodexDeviceLoginError(RuntimeError):
    pass


def _http_session() -> Any:
    return cffi_requests.Session(impersonate=CODEX_IMPERSONATE)


def _auth_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "originator": CODEX_ORIGINATOR,
        "User-Agent": build_user_agent(),
    }


class CodexDeviceLoginManager:
    """Runs at most one device-code login at a time, in a background thread.

    The API layer calls `start()` (returns the user_code to display) and then
    polls `status()`. Tokens never appear in any status payload.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {"state": LOGIN_STATE_IDLE}
        self._cancel_requested = False
        self._thread: Optional[threading.Thread] = None

    # -- public API ---------------------------------------------------------

    def start(self) -> Dict[str, Any]:
        """Request a user_code and launch the background poll.

        Idempotent while a login is in progress: returns the current
        user_code instead of starting a second flow.
        """
        with self._lock:
            if self._state.get("state") == LOGIN_STATE_WAITING:
                return dict(self._state)
            self._cancel_requested = False
            device = self._request_usercode()
            self._state = {
                "state": LOGIN_STATE_WAITING,
                "user_code": device["user_code"],
                "verification_url": DEVICE_VERIFICATION_URL,
                "started_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            self._thread = threading.Thread(
                target=self._poll_until_done,
                args=(device["device_auth_id"], device["user_code"], device["interval"]),
                name="codex-device-login",
                daemon=True,
            )
            self._thread.start()
            return dict(self._state)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def cancel(self) -> None:
        """Abandon the wait. The user_code simply expires on OpenAI's side."""
        with self._lock:
            self._cancel_requested = True
            if self._state.get("state") == LOGIN_STATE_WAITING:
                self._state = {"state": LOGIN_STATE_IDLE}

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

    def _poll_until_done(self, device_auth_id: str, user_code: str, interval: int) -> None:
        try:
            code_resp = self._poll_for_authorization(device_auth_id, user_code, interval)
            if code_resp is None:  # cancelled
                return
            tokens = self._exchange_code(code_resp)
            self._persist_login(tokens)
            with self._lock:
                self._state = {
                    "state": LOGIN_STATE_SUCCESS,
                    "account_id": extract_account_id(
                        tokens.get("access_token", ""), tokens.get("id_token", "")
                    ),
                }
            LOG.info("Codex device login succeeded; tokens written to SAIVerse store")
        except CodexDeviceLoginError as exc:
            LOG.warning("Codex device login failed: %s", exc)
            with self._lock:
                if not self._cancel_requested:
                    self._state = {"state": LOGIN_STATE_ERROR, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            LOG.exception("Codex device login crashed")
            with self._lock:
                if not self._cancel_requested:
                    self._state = {
                        "state": LOGIN_STATE_ERROR,
                        "error": f"予期しないエラー: {exc}",
                    }

    def _poll_for_authorization(
        self, device_auth_id: str, user_code: str, interval: int
    ) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + DEVICE_LOGIN_MAX_WAIT_SECONDS
        session = _http_session()
        try:
            while time.monotonic() < deadline:
                time.sleep(interval)
                with self._lock:
                    if self._cancel_requested:
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
            raise CodexDeviceLoginError("トークン交換の応答が JSON ではありませんでした。") from exc
        if not tokens.get("access_token"):
            raise CodexDeviceLoginError("トークン交換の応答に access_token がありません。")
        return tokens

    def _persist_login(self, tokens: Dict[str, Any]) -> None:
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
    "delete_saiverse_store",
    "decode_jwt_payload",
    "extract_account_id",
    "access_token_expiry",
    "CodexDeviceLoginError",
    "CodexDeviceLoginManager",
    "LOGIN_MANAGER",
    "auth_status",
]
