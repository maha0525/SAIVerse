"""アドオン管理 API エンドポイント。

アドオンの一覧取得・有効無効切り替え・パラメータ管理を提供する。
フロントエンドの AddonManagerModal から呼ばれる。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
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
    type: str  # "toggle" | "dropdown" | "slider" | "file"
    default: Any = None
    persona_configurable: bool = False
    # dropdown 用
    options: Optional[List[str]] = None
    # slider 用
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    value_type: Optional[str] = None  # "int" | "float"


class AddonUiBubbleButton(BaseModel):
    id: str
    icon: str
    label: str
    tool: Optional[str] = None
    metadata_key: Optional[str] = None
    show_when: Optional[str] = None  # "metadata_exists" | "always"


class AddonUiInputButton(BaseModel):
    id: str
    icon: str
    label: str
    tool: Optional[str] = None
    behavior: Optional[str] = None  # "replace_input" | "append_input"


class AddonUiExtensions(BaseModel):
    bubble_buttons: List[AddonUiBubbleButton] = []
    input_buttons: List[AddonUiInputButton] = []


class AddonManifest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    version: str = ""
    params_schema: List[AddonParamSchema] = []
    ui_extensions: AddonUiExtensions = AddonUiExtensions()


class AddonInfo(BaseModel):
    addon_name: str
    display_name: str
    description: str
    version: str
    is_enabled: bool
    params_schema: List[AddonParamSchema]
    params: Dict[str, Any]
    ui_extensions: AddonUiExtensions


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
        )
    finally:
        db.close()


@router.put("/{addon_name}/enabled")
def set_addon_enabled(addon_name: str, body: SetEnabledRequest, _manager=Depends(get_manager)):
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
        return {"addon_name": addon_name, "is_enabled": body.is_enabled}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
    """ペルソナ固有のアドオンパラメータを更新する。"""
    from database.models import AddonPersonaConfig
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    db = _get_session()
    try:
        stmt = sqlite_insert(AddonPersonaConfig).values(
            addon_name=addon_name,
            persona_id=persona_id,
            params_json=json.dumps(body.params, ensure_ascii=False),
        ).on_conflict_do_update(
            index_elements=["addon_name", "persona_id"],
            set_={"params_json": json.dumps(body.params, ensure_ascii=False)},
        )
        db.execute(stmt)
        db.commit()
        LOGGER.info("addon: updated persona config for %s / %s", addon_name, persona_id)
        return {"addon_name": addon_name, "persona_id": persona_id, "params": body.params}
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
