"""アドオンパラメータ読み取り API。

拡張パックの Python コードからアドオン設定（UI で変更したパラメータ等）を
読み取るためのヘルパー。

使い方:
    from saiverse.addon_config import get_params, is_addon_enabled

    # グローバル設定（全ペルソナ共通）
    params = get_params("saiverse-voice-tts")
    # → {"auto_speak": True, "server_side_playback": True, "_enabled": True}

    # ペルソナ上書きをマージした最終値
    params = get_params("saiverse-voice-tts", persona_id="Yui_city_a")

    # 有効状態だけ確認したい場合
    if not is_addon_enabled("saiverse-voice-tts"):
        return
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


def _load_manifest_defaults(addon_name: str) -> Dict[str, Any]:
    """addon.json の params_schema からデフォルト値を収集する。

    addon.json が存在しない、または読み取れない場合は空辞書を返す。
    """
    from saiverse.data_paths import EXPANSION_DATA_DIR

    manifest_path = EXPANSION_DATA_DIR / addon_name / "addon.json"
    if not manifest_path.exists():
        return {}

    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        defaults: Dict[str, Any] = {}
        for param in data.get("params_schema", []):
            key = param.get("key")
            if key and "default" in param:
                defaults[key] = param["default"]
        return defaults
    except Exception:
        LOGGER.warning("addon_config: failed to load manifest for %s", addon_name)
        return {}


def _get_session():
    from database.session import SessionLocal
    return SessionLocal()


def get_params(
    addon_name: str,
    persona_id: Optional[str] = None,
) -> Dict[str, Any]:
    """アドオンの最終パラメータを返す。

    優先順位（高い方が優先）:
      1. ペルソナ固有設定 (AddonPersonaConfig) — persona_id が指定された場合のみ
      2. グローバル設定 (AddonConfig)
      3. addon.json の params_schema デフォルト値

    戻り値の辞書には以下の特殊キーが含まれる:
      _enabled : bool — アドオンが有効かどうか

    Args:
        addon_name: アドオン名 (expansion_data/ 下のディレクトリ名)
        persona_id: ペルソナ固有設定を取得する場合のペルソナID

    Returns:
        マージ済みのパラメータ辞書。アドオン自体が DB に未登録でも
        addon.json のデフォルト値を返す（エラーにならない）。
    """
    from database.models import AddonConfig, AddonPersonaConfig

    # --- ベース: addon.json のデフォルト ---
    result: Dict[str, Any] = _load_manifest_defaults(addon_name)
    is_enabled: bool = True  # DBに未登録なら有効扱い

    db = _get_session()
    try:
        # --- グローバル設定で上書き ---
        config = db.query(AddonConfig).filter(AddonConfig.addon_name == addon_name).first()
        if config is not None:
            is_enabled = bool(config.is_enabled)
            if config.params_json:
                try:
                    result.update(json.loads(config.params_json))
                except (json.JSONDecodeError, TypeError):
                    LOGGER.warning(
                        "addon_config: invalid params_json for addon '%s'", addon_name
                    )

        # --- ペルソナ固有設定でさらに上書き ---
        if persona_id:
            persona_config = (
                db.query(AddonPersonaConfig)
                .filter(
                    AddonPersonaConfig.addon_name == addon_name,
                    AddonPersonaConfig.persona_id == persona_id,
                )
                .first()
            )
            if persona_config and persona_config.params_json:
                try:
                    result.update(json.loads(persona_config.params_json))
                except (json.JSONDecodeError, TypeError):
                    LOGGER.warning(
                        "addon_config: invalid persona params_json for addon '%s' / persona '%s'",
                        addon_name,
                        persona_id,
                    )
    finally:
        db.close()

    result["_enabled"] = is_enabled
    return result


def is_addon_enabled(addon_name: str) -> bool:
    """アドオンが有効かどうかを返す。

    DB に未登録（一度も管理UIを開いていない）場合は有効扱いとする。
    """
    from database.models import AddonConfig

    db = _get_session()
    try:
        config = db.query(AddonConfig).filter(AddonConfig.addon_name == addon_name).first()
        return config.is_enabled if config is not None else True
    finally:
        db.close()
