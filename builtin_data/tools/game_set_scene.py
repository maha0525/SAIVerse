"""Ruler 専用: ゲームのシーン状態を変更する。

設計意図: temp/region_rpg_intent.md §D, §E-1 (リポジトリ外管理)
"""
import logging

from tools.core import ToolSchema

logger = logging.getLogger(__name__)


def game_set_scene(scene: str) -> str:
    """自分が Ruler を務める Region のシーン状態を変更する。

    Args:
        scene: シーン名。推奨値: exploration / conversation / battle / shopping / rest

    Returns:
        結果メッセージ
    """
    from builtin_data.tools._game_common import get_active_ruler_region

    manager, region, err = get_active_ruler_region()
    if err:
        return err

    lifecycle = getattr(manager, "game_lifecycle", None)
    if lifecycle is None:
        return "エラー: ゲームライフサイクル機能が利用できません。"

    result = lifecycle.set_scene(region.region_id, scene)
    if result.startswith("Error"):
        return f"シーン変更に失敗しました: {result}"
    return f"シーンを '{scene}' に変更しました。"


def schema() -> ToolSchema:
    from builtin_data.tools._game_common import persona_is_ruler
    return ToolSchema(
        name="game_set_scene",
        description=(
            "Change the current scene state of the game you rule "
            "(exploration / conversation / battle / shopping / rest, or a custom scene). "
            "The scene is shown on the game state and guides what actions are appropriate. "
            "Only available to the Ruler of a game Region, while a game is in progress."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": (
                        "シーン名。推奨値: 'exploration' (探索), 'conversation' (会話), "
                        "'battle' (戦闘), 'shopping' (買い物), 'rest' (休息)。自由値も可。"
                    ),
                },
            },
            "required": ["scene"],
        },
        result_type="string",
        spell=True,
        spell_display_name="シーン変更 (GM)",
        availability_check=persona_is_ruler,
    )
