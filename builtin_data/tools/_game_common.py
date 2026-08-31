"""game Region (Region RPG) ツール群の共通ヘルパー。

先頭がアンダースコアで schema() を持たないため、ツールとしては登録されない。
設計意図: temp/region_rpg_intent.md §F (リポジトリ外管理)
"""
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def get_active_ruler_region() -> Tuple[Optional[Any], Optional[Any], Optional[str]]:
    """実行中ペルソナが Ruler を務める game Region を解決する。

    Returns:
        (manager, region, None) on success / (None, None, error_message) on failure
    """
    from tools.context import get_active_manager, get_active_persona_id

    manager = get_active_manager()
    persona_id = get_active_persona_id()
    if manager is None:
        return None, None, "エラー: マネージャーが見つかりません。"
    if not persona_id:
        return None, None, "エラー: 実行中のペルソナを特定できません。"
    for region in manager.regions.values():
        if region.is_game_region and str(region.ruler_id) == str(persona_id):
            return manager, region, None
    return None, None, "エラー: このツールは game Region の Ruler のみ使用できます。"


def persona_is_ruler(persona_id: Optional[str]) -> bool:
    """ToolSchema.availability_check 用: ペルソナが Ruler なら True。

    capture 時 (システムプロンプト構築時) に呼ばれるため、tools.context ではなく
    app_state 経由で manager を解決する。判定不能時は保守的に False。
    """
    if not persona_id:
        return False
    try:
        from saiverse import app_state
        manager = getattr(app_state, "manager", None)
        if manager is None:
            return False
        persona = manager.personas.get(persona_id)
        return getattr(persona, "persona_role", None) == "ruler"
    except Exception:
        logger.exception("[game_common] persona_is_ruler check failed for %s", persona_id)
        return False


def extract_id_from_result(result: str) -> Optional[str]:
    """admin CRUD の結果文字列 "... (ID: xxx) ..." から ID を抜き出す。

    create_building.py の既存パターンを踏襲した文字列パース。
    """
    if "ID:" not in result:
        return None
    try:
        return result.split("ID:")[1].split(")")[0].strip()
    except (IndexError, AttributeError):
        logger.warning("[game_common] Failed to extract ID from result: %s", result)
        return None
