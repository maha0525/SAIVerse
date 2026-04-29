"""アドオン管理 API エンドポイント。

アドオンの一覧取得・有効無効切り替え・パラメータ管理を提供する。
フロントエンドの AddonManagerModal から呼ばれる。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.deps import get_manager

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AddonParamSchema(BaseModel):
    key: str
    label: str
    description: Optional[str] = None
    type: str  # "toggle" | "text" | "password" | "number" | "file" | "dropdown" | "slider"
    default: Any = None
    persona_configurable: bool = False
    placeholder: Optional[str] = None  # text / number 用の placeholder
    # dropdown 用
    options: Optional[List[str]] = None
    # dropdown 用（動的）: 指定された場合、アドオン側エンドポイントから選択肢を取得する。
    # パスは "/audio-devices" のようなアドオンローカルパスでも、
    # "/api/addon/<name>/..." のような絶対パスでも可。
    options_endpoint: Optional[str] = None
    # number / slider 用
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    value_type: Optional[str] = None  # "int" | "float"
    # file 用
    accept: Optional[str] = None  # MIME type CSV: "audio/wav,audio/mpeg,video/mp4"
    max_size_mb: Optional[float] = None  # デフォルト 500MB。本体上限 1GB
    preview: Optional[str] = None  # "audio" | "image" | None


class AddonUiBubbleButton(BaseModel):
    id: str
    icon: str
    label: str
    action: Optional[str] = None  # "play_audio" 等のクライアント側ビルトインアクション
    tool: Optional[str] = None
    metadata_key: Optional[str] = None
    show_when: Optional[str] = None  # "metadata_exists" | "always"


class AddonUiInputButton(BaseModel):
    id: str
    icon: str
    label: str
    tool: Optional[str] = None
    behavior: Optional[str] = None  # "replace_input" | "append_input"


class AddonClientAction(BaseModel):
    """SSE イベント受信時にクライアント側で実行するアクションの宣言。

    本体側の action executor registry に登録された `action` 名に対応する関数が
    実行される。初期実装は ``play_audio`` のみ対応。

    フィールド:
      - ``id``: addon 内で一意な識別子（ログ用）
      - ``event``: 購読する SSE イベント名（例 ``audio_ready``）
      - ``action``: 実行する action executor 名（例 ``play_audio``）
      - ``source_metadata_key`` / ``fallback_metadata_key``: action が
        参照する metadata キー。executor が必要とするもののみ指定
      - ``requires_active_tab``: true のときアクティブクライアントタブでのみ実行
      - ``requires_enabled_param``: 指定した addon param が truthy のときのみ実行
      - ``on_failure_endpoint``: action 失敗時に POST する addon ローカルパス。
        失敗情報 (action_id / event / error_reason / message_id) を渡す
    """
    id: str
    event: str
    action: str  # "play_audio" 等（本体の executor registry に登録された名前）
    source_metadata_key: Optional[str] = None
    fallback_metadata_key: Optional[str] = None
    requires_active_tab: bool = False
    requires_enabled_param: Optional[str] = None
    on_failure_endpoint: Optional[str] = None


class AddonUiExtensions(BaseModel):
    bubble_buttons: List[AddonUiBubbleButton] = []
    input_buttons: List[AddonUiInputButton] = []
    client_actions: List[AddonClientAction] = []


class AddonOAuthFlow(BaseModel):
    """アドオンが提供する OAuth 認可フローの宣言。

    1つのアドオンで複数のOAuth接続を持てる（例: 同じアドオンが X と Mastodon
    両方の接続を提供する場合）。フローは常に per_persona であり、認可結果の
    トークン類は AddonPersonaConfig に保存される。

    フィールド:
      - ``key``: アドオン内で一意な識別子。コールバックパス算出などに使う
      - ``label``: AddonManager UI に表示する見出し
      - ``description``: UI 補助テキスト
      - ``provider``: OAuth フレーバー。Phase 1 は ``oauth2_pkce`` のみ対応
      - ``authorize_url`` / ``token_url``: OAuth エンドポイント
      - ``scopes``: 要求する scope のリスト
      - ``client_id_param``: グローバル ``params_schema`` のキー名を参照。
        ユーザー自身の Developer App credentials を使う設計
      - ``client_secret_param``: 同上。PKCE のみで secret 不要なケースは省略可
      - ``callback_path``: 省略時は ``/api/oauth/callback/<addon>/<key>`` が
        自動算出される。Developer Portal に登録済みのコールバックURLと一致
        させる必要があるため、外部要請で固定パスを使いたい場合のみ明示
      - ``result_mapping``: token endpoint レスポンスの各フィールドを
        ``AddonPersonaConfig.params_json`` のどのキーに保存するかの対応表。
        例: ``{"access_token": "x_access_token", "refresh_token": "x_refresh_token"}``
      - ``post_authorize_handler``: 認可成功後のフック。
        ``"module_name:function_name"`` 形式で ``expansion_data/<addon>/`` 配下の
        Python ファイルを参照。引数 ``(persona_id, tokens, params)`` を受け、
        追加で AddonPersonaConfig に保存する dict を返す
    """
    key: str
    label: str
    description: Optional[str] = None
    provider: str  # Phase 1: "oauth2_pkce" のみ
    authorize_url: str
    token_url: str
    scopes: List[str] = []
    client_id_param: str
    client_secret_param: Optional[str] = None
    callback_path: Optional[str] = None
    result_mapping: Dict[str, str]
    post_authorize_handler: Optional[str] = None


class AddonManifest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    version: str = ""
    params_schema: List[AddonParamSchema] = []
    ui_extensions: AddonUiExtensions = AddonUiExtensions()
    oauth_flows: List[AddonOAuthFlow] = []


class AddonInfo(BaseModel):
    addon_name: str
    display_name: str
    description: str
    version: str
    is_enabled: bool
    params_schema: List[AddonParamSchema]
    params: Dict[str, Any]
    ui_extensions: AddonUiExtensions
    oauth_flows: List[AddonOAuthFlow] = []


class SetEnabledRequest(BaseModel):
    is_enabled: bool


class UpdateParamsRequest(BaseModel):
    params: Dict[str, Any]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_expansion_data_dir() -> Path:
    from saiverse.data_paths import EXPANSION_DATA_DIR
    return EXPANSION_DATA_DIR


def _load_manifest(addon_dir: Path) -> Optional[AddonManifest]:
    """addon.json を読み込む。存在しない場合は None を返す。"""
    manifest_path = addon_dir / "addon.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        return AddonManifest(**data)
    except Exception:
        LOGGER.exception("addon: failed to load manifest at %s", manifest_path)
        return None


def _get_addon_dir(addon_name: str) -> Path:
    return _get_expansion_data_dir() / addon_name


def _get_or_create_config(db, addon_name: str):
    from database.models import AddonConfig
    row = db.query(AddonConfig).filter(AddonConfig.addon_name == addon_name).first()
    if row is None:
        row = AddonConfig(addon_name=addon_name, is_enabled=True, params_json=None)
        db.add(row)
        db.flush()
    return row


def _get_session():
    from database.session import SessionLocal
    return SessionLocal()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/", response_model=List[AddonInfo])
@router.get("", response_model=List[AddonInfo], include_in_schema=False)
def list_addons(_manager=Depends(get_manager)):
    """expansion_data/ 下のアドオン一覧を返す。

    addon.json が存在するディレクトリのみをアドオンとして認識する。
    """
    exp_dir = _get_expansion_data_dir()
    if not exp_dir.exists():
        return []

    db = _get_session()
    try:
        results: List[AddonInfo] = []
        for addon_dir in sorted(exp_dir.iterdir()):
            if not addon_dir.is_dir():
                continue
            manifest = _load_manifest(addon_dir)
            if manifest is None:
                continue

            addon_name = addon_dir.name
            config = _get_or_create_config(db, addon_name)
            db.commit()

            params: Dict[str, Any] = {}
            if config.params_json:
                try:
                    params = json.loads(config.params_json)
                except (json.JSONDecodeError, TypeError):
                    pass

            results.append(AddonInfo(
                addon_name=addon_name,
                display_name=manifest.display_name or addon_name,
                description=manifest.description,
                version=manifest.version,
                is_enabled=config.is_enabled,
                params_schema=manifest.params_schema,
                params=params,
                ui_extensions=manifest.ui_extensions,
                oauth_flows=manifest.oauth_flows,
            ))
        return results
    finally:
        db.close()


@router.get("/{addon_name}", response_model=AddonInfo)
def get_addon(addon_name: str, _manager=Depends(get_manager)):
    """指定アドオンの詳細を返す。"""
    addon_dir = _get_addon_dir(addon_name)
    if not addon_dir.exists():
        raise HTTPException(status_code=404, detail="Addon not found")

    manifest = _load_manifest(addon_dir)
    if manifest is None:
        raise HTTPException(status_code=404, detail="addon.json not found")

    db = _get_session()
    try:
        config = _get_or_create_config(db, addon_name)
        db.commit()

        params: Dict[str, Any] = {}
        if config.params_json:
            try:
                params = json.loads(config.params_json)
            except (json.JSONDecodeError, TypeError):
                pass

        return AddonInfo(
            addon_name=addon_name,
            display_name=manifest.display_name or addon_name,
            description=manifest.description,
            version=manifest.version,
            is_enabled=config.is_enabled,
            params_schema=manifest.params_schema,
            params=params,
            ui_extensions=manifest.ui_extensions,
            oauth_flows=manifest.oauth_flows,
        )
    finally:
        db.close()


@router.put("/{addon_name}/enabled")
def set_addon_enabled(addon_name: str, body: SetEnabledRequest, manager=Depends(get_manager)):
    """アドオンの有効/無効を切り替える。サーバー再起動不要で即時反映。"""
    addon_dir = _get_addon_dir(addon_name)
    if not addon_dir.exists():
        raise HTTPException(status_code=404, detail="Addon not found")

    db = _get_session()
    try:
        config = _get_or_create_config(db, addon_name)
        config.is_enabled = body.is_enabled
        db.commit()
        LOGGER.info("addon: %s enabled=%s", addon_name, body.is_enabled)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Notify MCP layer so that any MCP servers declared by this addon get
    # their refcount / lifecycle updated (start on enable, shutdown on
    # disable). See docs/intent/mcp_addon_integration.md §3.
    try:
        from tools.mcp_client import notify_addon_toggled_sync
        notify_addon_toggled_sync(addon_name, body.is_enabled)
    except Exception as exc:
        LOGGER.warning(
            "addon: MCP notification failed for '%s' (enabled=%s): %s",
            addon_name,
            body.is_enabled,
            exc,
        )

    # Notify integration layer so addon-provided BaseIntegration subclasses
    # are register/unregistered without requiring a restart.
    # See docs/intent/addon_extension_points.md §C.
    try:
        integration_manager = getattr(manager, "integration_manager", None)
        if integration_manager is not None:
            if body.is_enabled:
                from saiverse.addon_loader import register_addon_integrations
                register_addon_integrations(integration_manager, addon_name)
            else:
                from saiverse.addon_loader import unregister_addon_integrations
                unregister_addon_integrations(integration_manager, addon_name)
    except Exception as exc:
        LOGGER.warning(
            "addon: integration notification failed for '%s' (enabled=%s): %s",
            addon_name,
            body.is_enabled,
            exc,
        )

    # Broadcast to frontend subscribers as well so UIs can react
    try:
        from saiverse.addon_events import emit_addon_event
        emit_addon_event(
            "system",
            "addon_toggled",
            {"addon_name": addon_name, "is_enabled": body.is_enabled},
        )
    except Exception as exc:
        LOGGER.debug("addon: broadcast failed for '%s': %s", addon_name, exc)

    return {"addon_name": addon_name, "is_enabled": body.is_enabled}


@router.get("/{addon_name}/config")
def get_addon_config(addon_name: str, _manager=Depends(get_manager)):
    """アドオンのグローバルパラメータを返す。"""
    db = _get_session()
    try:
        config = _get_or_create_config(db, addon_name)
        db.commit()
        params: Dict[str, Any] = {}
        if config.params_json:
            try:
                params = json.loads(config.params_json)
            except (json.JSONDecodeError, TypeError):
                pass
        return {"addon_name": addon_name, "params": params}
    finally:
        db.close()


@router.put("/{addon_name}/config")
def update_addon_config(addon_name: str, body: UpdateParamsRequest, _manager=Depends(get_manager)):
    """アドオンのグローバルパラメータを更新する。"""
    db = _get_session()
    try:
        config = _get_or_create_config(db, addon_name)
        config.params_json = json.dumps(body.params, ensure_ascii=False)
        db.commit()
        LOGGER.info("addon: updated config for %s", addon_name)
        return {"addon_name": addon_name, "params": body.params}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/{addon_name}/config/persona/{persona_id}")
def get_addon_persona_config(addon_name: str, persona_id: str, _manager=Depends(get_manager)):
    """ペルソナ固有のアドオンパラメータを返す。存在しない場合は空辞書。"""
    from database.models import AddonPersonaConfig
    db = _get_session()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        params: Dict[str, Any] = {}
        if row and row.params_json:
            try:
                params = json.loads(row.params_json)
            except (json.JSONDecodeError, TypeError):
                pass
        return {"addon_name": addon_name, "persona_id": persona_id, "params": params}
    finally:
        db.close()


@router.put("/{addon_name}/config/persona/{persona_id}")
def update_addon_persona_config(
    addon_name: str,
    persona_id: str,
    body: UpdateParamsRequest,
    _manager=Depends(get_manager),
):
    """ペルソナ固有のアドオンパラメータを **merge** で更新する。

    完全上書きではなく既存 params_json に body.params をマージして保存する。
    これは OAuth handler 側の `_merge_persona_params` のセマンティクスに合わせるため。
    完全上書きにすると例えば「個別設定を作成」UI が default 値だけ送った時に
    OAuth トークン (x_access_token 等) を破壊する事故が起きる。

    body.params に値 ``None`` を渡すとそのキーは削除される。
    """
    from database.models import AddonPersonaConfig

    db = _get_session()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        existing: Dict[str, Any] = {}
        if row and row.params_json:
            try:
                existing = json.loads(row.params_json)
            except (json.JSONDecodeError, TypeError):
                LOGGER.warning(
                    "addon: invalid params_json for %s/%s, treating as empty",
                    addon_name, persona_id,
                )

        merged = dict(existing)
        for k, v in body.params.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        params_json = json.dumps(merged, ensure_ascii=False)

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
        LOGGER.info(
            "addon: merged persona config for %s/%s (keys=%s)",
            addon_name, persona_id, list(body.params.keys()),
        )
        return {"addon_name": addon_name, "persona_id": persona_id, "params": merged}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.delete("/{addon_name}/config/persona/{persona_id}")
def delete_addon_persona_config(
    addon_name: str,
    persona_id: str,
    _manager=Depends(get_manager),
):
    """ペルソナ固有のアドオンパラメータを削除する（デフォルトに戻す）。"""
    from database.models import AddonPersonaConfig

    db = _get_session()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        if row:
            db.delete(row)
            db.commit()
        return {"addon_name": addon_name, "persona_id": persona_id, "deleted": True}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@router.get("/messages/{message_id}/metadata")
def get_message_addon_metadata(
    message_id: str,
    addon: Optional[str] = None,
    _manager=Depends(get_manager),
):
    """メッセージに紐付くアドオンメタデータを返す。

    addon クエリパラメータを指定した場合はそのアドオンのみ。
    指定しない場合は全アドオンのメタデータをaddon名でグループ化して返す。
    """
    from database.models import AddonMessageMetadata

    db = _get_session()
    try:
        query = db.query(AddonMessageMetadata).filter(
            AddonMessageMetadata.message_id == message_id
        )
        if addon:
            query = query.filter(AddonMessageMetadata.addon_name == addon)

        rows = query.all()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row.addon_name not in result:
                result[row.addon_name] = {}
            try:
                result[row.addon_name][row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                result[row.addon_name][row.key] = row.value

        if addon:
            return {"message_id": message_id, "addon": addon, "metadata": result.get(addon, {})}
        return {"message_id": message_id, "metadata": result}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# File parameter endpoints (per-persona)
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_SYSTEM_MAX_SIZE_BYTES = 1024 * 1024 * 1024  # 1 GB


def _addon_files_base() -> Path:
    from saiverse.data_paths import get_saiverse_home
    return get_saiverse_home() / "user_data" / "addon_files"


def _validate_names(*names: str) -> None:
    for name in names:
        if not name or not _SAFE_NAME_RE.match(name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid name (alphanumeric, -, _ only): {name!r}",
            )


def _get_file_param_schema(addon_name: str, param_key: str) -> AddonParamSchema:
    from saiverse.data_paths import EXPANSION_DATA_DIR
    manifest_path = EXPANSION_DATA_DIR / addon_name / "addon.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail=f"Addon manifest not found: {addon_name}")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read addon manifest: {exc}")
    for param in data.get("params_schema", []):
        if param.get("key") == param_key and param.get("type") == "file":
            return AddonParamSchema(**param)
    raise HTTPException(
        status_code=404,
        detail=f"File parameter '{param_key}' not found in addon '{addon_name}'",
    )


def _resolve_file_dir(addon_name: str, persona_id: Optional[str], param_key: str) -> Path:
    base = _addon_files_base() / addon_name
    if persona_id:
        return base / "personas" / persona_id
    return base / "global"


def _ext_from_content_type(content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type)
    if ext:
        return ext
    mapping = {
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/flac": ".flac",
        "audio/ogg": ".ogg",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    return mapping.get(content_type, "")


@router.post("/{addon_name}/config/persona/{persona_id}/file/{param_key}")
async def upload_persona_file(
    addon_name: str,
    persona_id: str,
    param_key: str,
    file: UploadFile = File(...),
    _manager=Depends(get_manager),
):
    """ペルソナ別のファイルパラメータをアップロードする。

    addon.json の params_schema で type="file" かつ persona_configurable=true の
    パラメータにのみ使用可能。ファイルは ~/.saiverse/user_data/addon_files/ 配下に
    保存され、AddonPersonaConfig.params_json に保存先パスが書き込まれる。
    """
    _validate_names(addon_name, persona_id, param_key)
    schema = _get_file_param_schema(addon_name, param_key)

    if not schema.persona_configurable:
        raise HTTPException(status_code=400, detail="This parameter is not persona-configurable")

    # Content-type validation
    ct = file.content_type or "application/octet-stream"
    if schema.accept:
        allowed = [t.strip() for t in schema.accept.split(",")]
        if ct not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"File type '{ct}' not allowed. Accepted: {schema.accept}",
            )

    # Size limit
    max_bytes = int((schema.max_size_mb or 500) * 1024 * 1024)
    max_bytes = min(max_bytes, _SYSTEM_MAX_SIZE_BYTES)

    # Read file with size check
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File too large ({len(content)} bytes). Max: {max_bytes} bytes ({max_bytes / 1024 / 1024:.0f} MB)",
        )

    # Determine extension and save
    ext = _ext_from_content_type(ct)
    dest_dir = _resolve_file_dir(addon_name, persona_id, param_key)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{param_key}{ext}"

    # Atomic write: temp file then rename
    tmp_file = dest_file.with_suffix(dest_file.suffix + ".tmp")
    try:
        tmp_file.write_bytes(content)
        shutil.move(str(tmp_file), str(dest_file))
    except Exception as exc:
        tmp_file.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}")

    # Update AddonPersonaConfig.params_json
    from database.models import AddonPersonaConfig
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    db = _get_session()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        params: Dict[str, Any] = {}
        if row and row.params_json:
            try:
                params = json.loads(row.params_json)
            except (json.JSONDecodeError, TypeError):
                pass
        params[param_key] = str(dest_file)
        params_str = json.dumps(params, ensure_ascii=False)

        stmt = sqlite_insert(AddonPersonaConfig).values(
            addon_name=addon_name,
            persona_id=persona_id,
            params_json=params_str,
        ).on_conflict_do_update(
            index_elements=["addon_name", "persona_id"],
            set_={"params_json": params_str},
        )
        db.execute(stmt)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    LOGGER.info(
        "addon file uploaded: addon=%s persona=%s key=%s size=%d path=%s",
        addon_name, persona_id, param_key, len(content), dest_file,
    )
    return {
        "path": str(dest_file),
        "size": len(content),
        "content_type": ct,
    }


@router.get("/{addon_name}/config/persona/{persona_id}/file/{param_key}")
async def get_persona_file(
    addon_name: str,
    persona_id: str,
    param_key: str,
    _manager=Depends(get_manager),
):
    """ペルソナ別のファイルパラメータを取得(プレビュー/ダウンロード)する。"""
    _validate_names(addon_name, persona_id, param_key)
    _get_file_param_schema(addon_name, param_key)

    # Look up file path from AddonPersonaConfig
    from database.models import AddonPersonaConfig
    db = _get_session()
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        if not row or not row.params_json:
            raise HTTPException(status_code=404, detail="No persona config found")
        params = json.loads(row.params_json)
        file_path = params.get(param_key)
        if not file_path:
            raise HTTPException(status_code=404, detail="File parameter not set")
    finally:
        db.close()

    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    ct = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(
        path=str(path),
        media_type=ct,
        filename=path.name,
        content_disposition_type="inline",
    )


@router.delete("/{addon_name}/config/persona/{persona_id}/file/{param_key}")
async def delete_persona_file(
    addon_name: str,
    persona_id: str,
    param_key: str,
    _manager=Depends(get_manager),
):
    """ペルソナ別のファイルパラメータを削除する。"""
    _validate_names(addon_name, persona_id, param_key)

    # Remove from params_json
    from database.models import AddonPersonaConfig
    db = _get_session()
    file_path: Optional[str] = None
    try:
        row = (
            db.query(AddonPersonaConfig)
            .filter(
                AddonPersonaConfig.addon_name == addon_name,
                AddonPersonaConfig.persona_id == persona_id,
            )
            .first()
        )
        if row and row.params_json:
            params = json.loads(row.params_json)
            file_path = params.pop(param_key, None)
            row.params_json = json.dumps(params, ensure_ascii=False)
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Delete physical file
    if file_path:
        p = Path(file_path)
        if p.exists():
            p.unlink()
            LOGGER.info("addon file deleted: %s", p)

    return {"deleted": True}


# ---------------------------------------------------------------------------
# File parameter endpoints (global) — scaffold
# ---------------------------------------------------------------------------

@router.post("/{addon_name}/config/file/{param_key}")
async def upload_global_file(
    addon_name: str,
    param_key: str,
    file: UploadFile = File(...),
    _manager=Depends(get_manager),
):
    """グローバルのファイルパラメータをアップロードする（雛形）。"""
    raise HTTPException(status_code=501, detail="Global file upload not yet implemented")


@router.get("/{addon_name}/config/file/{param_key}")
async def get_global_file(
    addon_name: str,
    param_key: str,
    _manager=Depends(get_manager),
):
    """グローバルのファイルパラメータを取得する（雛形）。"""
    raise HTTPException(status_code=501, detail="Global file retrieval not yet implemented")


@router.delete("/{addon_name}/config/file/{param_key}")
async def delete_global_file(
    addon_name: str,
    param_key: str,
    _manager=Depends(get_manager),
):
    """グローバルのファイルパラメータを削除する（雛形）。"""
    raise HTTPException(status_code=501, detail="Global file deletion not yet implemented")
