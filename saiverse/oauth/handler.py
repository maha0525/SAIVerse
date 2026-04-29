"""OAuth 2.0 PKCE フローハンドラ。

addon.json の ``oauth_flows`` セクションで宣言された OAuth 接続を、
コア側で一元的に処理する。Phase 1 は ``oauth2_pkce`` プロバイダのみ対応。

責務:
  - state / PKCE challenge 生成と pending state の管理（in-memory）
  - 認可URL組み立て
  - コールバック時の token endpoint 呼び出し
  - result_mapping に従って AddonPersonaConfig へトークン保存
  - Pull型 get_valid_token() で期限切れ時の自動リフレッシュ
  - 切断（保存トークンの削除）
  - 接続ステータス取得

設計判断:
  - state 管理は in-memory（サーバー再起動で揮発するが認可フローは数分で
    完了する短命トランザクションのため許容）
  - トークンリフレッシュは Pull型のみ。アドオン側で API 呼び出し直前に
    ``get_valid_token()`` を呼ぶ規約

Intent Doc: ``docs/intent/addon_extension_points.md`` セクション B
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import logging
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple

import httpx

LOGGER = logging.getLogger(__name__)

# state TTL（秒）。これを超えた pending state は callback 時に拒否し、
# 定期 GC でも掃除する。
_STATE_TTL_SECONDS = 600  # 10分


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OAuthError(Exception):
    """OAuth フロー処理中の汎用エラー。"""


class OAuthFlowNotFoundError(OAuthError):
    """addon.json に該当 flow_key が見つからない、またはアドオン未認識。"""


# ---------------------------------------------------------------------------
# In-memory pending state
# ---------------------------------------------------------------------------

@dataclass
class _PendingState:
    addon_name: str
    flow_key: str
    persona_id: str
    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.time)


_pending_states: Dict[str, _PendingState] = {}
_pending_lock = Lock()


def _gc_pending() -> None:
    """期限切れの pending state を破棄する（呼び出し時に lock 取得済み前提）。"""
    now = time.time()
    expired = [
        k for k, v in _pending_states.items()
        if now - v.created_at > _STATE_TTL_SECONDS
    ]
    for k in expired:
        del _pending_states[k]
    if expired:
        LOGGER.debug("oauth: GC removed %d expired pending state(s)", len(expired))


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _generate_code_verifier() -> str:
    """RFC 7636 §4.1 — 43〜128 文字の URL-safe ランダム文字列。"""
    return secrets.token_urlsafe(64)[:128]


def _code_challenge_from_verifier(verifier: str) -> str:
    """S256 challenge を生成する。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# addon.json / params 読み書き
# ---------------------------------------------------------------------------

def _load_addon_manifest(addon_name: str) -> Dict[str, Any]:
    """addon.json を直接読む（Pydantic を介さない、汎用 dict として返す）。"""
    from saiverse.data_paths import EXPANSION_DATA_DIR

    manifest_path = EXPANSION_DATA_DIR / addon_name / "addon.json"
    if not manifest_path.exists():
        raise OAuthFlowNotFoundError(f"Addon not found: {addon_name}")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"Failed to load addon.json for {addon_name}: {exc}") from exc


def _find_flow(manifest: Dict[str, Any], flow_key: str) -> Dict[str, Any]:
    """addon.json の oauth_flows から該当 flow を見つける。"""
    flows = manifest.get("oauth_flows", []) or []
    for flow in flows:
        if flow.get("key") == flow_key:
            return flow
    raise OAuthFlowNotFoundError(
        f"OAuth flow '{flow_key}' not declared in addon '{manifest.get('name')}'"
    )


def _get_flow(addon_name: str, flow_key: str) -> Dict[str, Any]:
    return _find_flow(_load_addon_manifest(addon_name), flow_key)


def _default_callback_path(addon_name: str, flow_key: str) -> str:
    return f"/api/oauth/callback/{addon_name}/{flow_key}"


def _resolve_callback_url(flow: Dict[str, Any], addon_name: str, base_url: str) -> str:
    """flow.callback_path（or default）と base_url を結合した絶対URLを返す。"""
    path = flow.get("callback_path") or _default_callback_path(addon_name, flow["key"])
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{base}{path}"


def _load_persona_params(addon_name: str, persona_id: str) -> Dict[str, Any]:
    from database.models import AddonPersonaConfig
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        if row is None or not row.params_json:
            return {}
        try:
            return json.loads(row.params_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    finally:
        db.close()


def _merge_persona_params(addon_name: str, persona_id: str, updates: Dict[str, Any]) -> None:
    """既存 AddonPersonaConfig.params_json に updates をマージして保存する。

    None を渡すとそのキーが削除される。
    """
    from database.models import AddonPersonaConfig
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        if row is None:
            params: Dict[str, Any] = {}
        else:
            try:
                params = json.loads(row.params_json) if row.params_json else {}
            except (json.JSONDecodeError, TypeError):
                params = {}

        for k, v in updates.items():
            if v is None:
                params.pop(k, None)
            else:
                params[k] = v

        params_json = json.dumps(params, ensure_ascii=False)

        if row is None:
            row = AddonPersonaConfig(
                addon_name=addon_name,
                persona_id=persona_id,
                params_json=params_json,
            )
            db.add(row)
        else:
            row.params_json = params_json

        db.commit()
        LOGGER.debug(
            "oauth: persona params updated addon=%s persona=%s keys=%s",
            addon_name, persona_id, list(updates.keys()),
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _get_global_param(addon_name: str, key: str) -> Optional[str]:
    """グローバル AddonConfig + addon.json のデフォルトから値を取得する。"""
    from saiverse.addon_config import get_params

    params = get_params(addon_name)
    value = params.get(key)
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_authorize_url(
    addon_name: str,
    flow_key: str,
    persona_id: str,
    base_url: str,
) -> str:
    """認可URLを組み立てて返す。pending state を内部に登録する。

    Args:
        addon_name: アドオン名
        flow_key: addon.json の oauth_flows[].key
        persona_id: 認可対象のペルソナID
        base_url: コールバックURL生成のためのサーバーベースURL
                  (例: ``http://127.0.0.1:8000``)

    Returns:
        ブラウザに開かせる認可URL（state / code_challenge 付き）

    Raises:
        OAuthFlowNotFoundError: flow_key が宣言されていない
        OAuthError: client_id_param が解決できないなど
    """
    flow = _get_flow(addon_name, flow_key)

    provider = flow.get("provider", "")
    if provider != "oauth2_pkce":
        raise OAuthError(
            f"Unsupported OAuth provider '{provider}' for addon '{addon_name}' "
            f"flow '{flow_key}'. Phase 1 supports only 'oauth2_pkce'."
        )

    client_id_param = flow.get("client_id_param")
    if not client_id_param:
        raise OAuthError(
            f"oauth_flows.{flow_key}.client_id_param is required"
        )

    client_id = _get_global_param(addon_name, client_id_param)
    if not client_id:
        raise OAuthError(
            f"client_id not configured: addon '{addon_name}' param "
            f"'{client_id_param}' is empty. Set it in the addon settings first."
        )

    # PKCE
    code_verifier = _generate_code_verifier()
    code_challenge = _code_challenge_from_verifier(code_verifier)

    # state
    state = secrets.token_urlsafe(32)
    redirect_uri = _resolve_callback_url(flow, addon_name, base_url)

    with _pending_lock:
        _gc_pending()
        _pending_states[state] = _PendingState(
            addon_name=addon_name,
            flow_key=flow_key,
            persona_id=persona_id,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )

    scopes = flow.get("scopes", []) or []
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)

    authorize_url = flow["authorize_url"]
    sep = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{sep}{urllib.parse.urlencode(params)}"


def exchange_code(
    addon_name: str,
    flow_key: str,
    code: str,
    state: str,
) -> Dict[str, Any]:
    """コールバックで受け取った code を token endpoint で交換し、
    result_mapping に従って AddonPersonaConfig に保存する。

    Args:
        addon_name: アドオン名（callback path から取得）
        flow_key: コールバックパスから取得した flow キー
        code: 認可サーバーから返された authorization code
        state: 認可サーバーから返された state

    Returns:
        ``{"persona_id": "...", "username": "...", "saved_keys": [...]}``
        の dict（フロント表示用）

    Raises:
        OAuthError: state不正、token endpoint失敗、設定不備など
    """
    # state を pop（ワンタイム）
    with _pending_lock:
        _gc_pending()
        pending = _pending_states.pop(state, None)

    if pending is None:
        raise OAuthError("Invalid or expired OAuth state")

    if pending.addon_name != addon_name or pending.flow_key != flow_key:
        raise OAuthError(
            f"OAuth state mismatch: expected ({pending.addon_name}, "
            f"{pending.flow_key}), got ({addon_name}, {flow_key})"
        )

    flow = _get_flow(addon_name, flow_key)
    client_id = _get_global_param(addon_name, flow["client_id_param"])
    if not client_id:
        raise OAuthError(
            f"client_id not configured for addon '{addon_name}'"
        )

    client_secret_param = flow.get("client_secret_param")
    client_secret = (
        _get_global_param(addon_name, client_secret_param)
        if client_secret_param else None
    )

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "code_verifier": pending.code_verifier,
    }

    # Confidential client (client_secret あり) は HTTP Basic Auth ヘッダーで認証する
    # X (Twitter) や一部プロバイダは body の client_secret では認証されず 401 を返す。
    # Public client (client_secret 無し) は body に client_id のみ。
    auth: Optional[Tuple[str, str]] = None
    if client_secret:
        auth = (client_id, client_secret)
    else:
        data["client_id"] = client_id

    LOGGER.info(
        "oauth: exchanging code addon=%s flow=%s persona=%s auth=%s",
        addon_name, flow_key, pending.persona_id,
        "basic" if auth else "public",
    )

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                flow["token_url"],
                data=data,
                headers={"Accept": "application/json"},
                auth=auth,
            )
    except httpx.HTTPError as exc:
        raise OAuthError(f"Token endpoint request failed: {exc}") from exc

    if response.status_code >= 400:
        raise OAuthError(
            f"Token endpoint returned {response.status_code}: {response.text[:500]}"
        )

    try:
        tokens = response.json()
    except ValueError as exc:
        raise OAuthError(f"Token endpoint returned non-JSON: {exc}") from exc

    # result_mapping を適用して params に保存する dict を組み立てる
    result_mapping: Dict[str, str] = flow.get("result_mapping", {}) or {}
    updates: Dict[str, Any] = {}
    for source_key, target_key in result_mapping.items():
        if source_key == "expires_at":
            # 特殊: expires_in (秒) から絶対時刻を計算して保存する
            expires_in = tokens.get("expires_in")
            if expires_in is not None:
                try:
                    updates[target_key] = time.time() + float(expires_in)
                except (TypeError, ValueError):
                    LOGGER.warning("oauth: invalid expires_in: %r", expires_in)
        elif source_key in tokens:
            updates[target_key] = tokens[source_key]

    # post_authorize_handler を呼ぶ（あれば）
    handler_spec = flow.get("post_authorize_handler")
    if handler_spec:
        try:
            current_params = _load_persona_params(addon_name, pending.persona_id)
            # updates も含めた最新値で渡す
            current_params = {**current_params, **updates}
            extra = _invoke_post_authorize_handler(
                addon_name, handler_spec, pending.persona_id, tokens, current_params,
            )
            if extra:
                updates.update(extra)
        except Exception:
            LOGGER.exception(
                "oauth: post_authorize_handler failed for addon=%s flow=%s",
                addon_name, flow_key,
            )
            # ハンドラ失敗してもトークン保存は続行（接続自体は成功させる）

    _merge_persona_params(addon_name, pending.persona_id, updates)

    LOGGER.info(
        "oauth: connection established addon=%s flow=%s persona=%s saved_keys=%s",
        addon_name, flow_key, pending.persona_id, list(updates.keys()),
    )

    return {
        "persona_id": pending.persona_id,
        "addon_name": addon_name,
        "flow_key": flow_key,
        "saved_keys": list(updates.keys()),
    }


def _invoke_post_authorize_handler(
    addon_name: str,
    spec: str,
    persona_id: str,
    tokens: Dict[str, Any],
    params: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """post_authorize_handler を ``module:function`` 形式で動的にロードして呼ぶ。

    モジュールは ``expansion_data/<addon>/<module>.py`` を指す。
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    if ":" not in spec:
        raise OAuthError(
            f"post_authorize_handler must be 'module:function', got '{spec}'"
        )
    module_name, func_name = spec.split(":", 1)

    module_path = EXPANSION_DATA_DIR / addon_name / f"{module_name}.py"
    if not module_path.exists():
        raise OAuthError(
            f"post_authorize_handler module not found: {module_path}"
        )

    sys_module_key = f"_addon_{addon_name.replace('-', '_')}_oauth_handler_{module_name}"
    spec_obj = importlib.util.spec_from_file_location(sys_module_key, module_path)
    if spec_obj is None or spec_obj.loader is None:
        raise OAuthError(f"Failed to load handler module: {module_path}")
    module = importlib.util.module_from_spec(spec_obj)
    sys.modules[sys_module_key] = module
    spec_obj.loader.exec_module(module)

    func = getattr(module, func_name, None)
    if not callable(func):
        raise OAuthError(
            f"post_authorize_handler function '{func_name}' not found in {module_path}"
        )

    result = func(persona_id, tokens, params)
    if result is None:
        return None
    if not isinstance(result, dict):
        raise OAuthError(
            f"post_authorize_handler must return dict or None, got {type(result).__name__}"
        )
    return result


def get_valid_token(
    addon_name: str,
    flow_key: str,
    persona_id: str,
) -> Optional[str]:
    """有効なアクセストークンを返す。期限切れならリフレッシュを試みる。

    アドオンのコードから API 呼び出し直前に呼ぶ Pull型インターフェース。

    Args:
        addon_name: アドオン名
        flow_key: oauth_flows[].key
        persona_id: 対象ペルソナID

    Returns:
        有効なアクセストークン文字列。トークン未保存・リフレッシュ失敗で None
    """
    flow = _get_flow(addon_name, flow_key)
    result_mapping: Dict[str, str] = flow.get("result_mapping", {}) or {}

    access_token_key = result_mapping.get("access_token")
    if not access_token_key:
        raise OAuthError(
            f"result_mapping.access_token must be declared for addon '{addon_name}' "
            f"flow '{flow_key}'"
        )

    refresh_token_key = result_mapping.get("refresh_token")
    expires_at_key = result_mapping.get("expires_at")

    params = _load_persona_params(addon_name, persona_id)
    access_token = params.get(access_token_key)
    if not access_token:
        return None

    # 期限切れチェック（60秒のバッファ）
    expires_at = params.get(expires_at_key) if expires_at_key else None
    needs_refresh = (
        expires_at is not None
        and isinstance(expires_at, (int, float))
        and expires_at - time.time() < 60
    )

    if not needs_refresh:
        return str(access_token)

    refresh_token = params.get(refresh_token_key) if refresh_token_key else None
    if not refresh_token:
        LOGGER.warning(
            "oauth: token expired but no refresh_token for addon=%s persona=%s",
            addon_name, persona_id,
        )
        return None

    LOGGER.info(
        "oauth: refreshing token addon=%s flow=%s persona=%s",
        addon_name, flow_key, persona_id,
    )

    client_id = _get_global_param(addon_name, flow["client_id_param"])
    client_secret = (
        _get_global_param(addon_name, flow["client_secret_param"])
        if flow.get("client_secret_param") else None
    )

    data = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token),
    }

    # 認証方式は token exchange と同じ（Confidential client は Basic Auth ヘッダー）
    auth: Optional[Tuple[str, str]] = None
    if client_id and client_secret:
        auth = (client_id, client_secret)
    elif client_id:
        data["client_id"] = client_id

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                flow["token_url"],
                data=data,
                headers={"Accept": "application/json"},
                auth=auth,
            )
    except httpx.HTTPError as exc:
        LOGGER.error("oauth: refresh request failed: %s", exc)
        return None

    if response.status_code >= 400:
        LOGGER.error(
            "oauth: refresh returned %d: %s",
            response.status_code, response.text[:500],
        )
        return None

    try:
        tokens = response.json()
    except ValueError:
        LOGGER.error("oauth: refresh returned non-JSON")
        return None

    updates: Dict[str, Any] = {}
    for source_key, target_key in result_mapping.items():
        if source_key == "expires_at":
            expires_in = tokens.get("expires_in")
            if expires_in is not None:
                try:
                    updates[target_key] = time.time() + float(expires_in)
                except (TypeError, ValueError):
                    pass
        elif source_key in tokens:
            updates[target_key] = tokens[source_key]

    # refresh_token は返ってこないこともある（その場合は既存値を維持）
    if refresh_token_key and refresh_token_key not in updates:
        updates.pop(refresh_token_key, None)

    _merge_persona_params(addon_name, persona_id, updates)

    new_access = updates.get(access_token_key) or access_token
    return str(new_access)


def get_status(
    addon_name: str,
    flow_key: str,
    persona_id: str,
) -> Dict[str, Any]:
    """接続ステータスを返す（フロントUI用）。

    Returns:
        ``{"connected": bool, "params": {...non-token, displayable keys...}}``
    """
    flow = _get_flow(addon_name, flow_key)
    result_mapping: Dict[str, str] = flow.get("result_mapping", {}) or {}
    access_token_key = result_mapping.get("access_token")

    params = _load_persona_params(addon_name, persona_id)
    connected = bool(access_token_key and params.get(access_token_key))

    # トークン本体は返さず、それ以外の保存キー（username 等）だけ返す
    token_keys = {
        result_mapping.get("access_token"),
        result_mapping.get("refresh_token"),
        result_mapping.get("expires_at"),
    }
    public_params = {k: v for k, v in params.items() if k not in token_keys and v is not None}

    return {
        "connected": connected,
        "params": public_params,
    }


def disconnect(
    addon_name: str,
    flow_key: str,
    persona_id: str,
) -> None:
    """保存されたトークン類を AddonPersonaConfig から削除する。

    result_mapping に列挙されたキー、および post_authorize_handler が
    保存した可能性のあるキーは削除しない（ユーザーがそれらも手動で削除
    したい場合は別途行う）。
    """
    flow = _get_flow(addon_name, flow_key)
    result_mapping: Dict[str, str] = flow.get("result_mapping", {}) or {}

    updates = {target_key: None for target_key in result_mapping.values()}
    if updates:
        _merge_persona_params(addon_name, persona_id, updates)

    LOGGER.info(
        "oauth: disconnected addon=%s flow=%s persona=%s",
        addon_name, flow_key, persona_id,
    )
