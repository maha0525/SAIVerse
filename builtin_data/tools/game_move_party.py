"""Ruler 専用: パーティー (参加者一行 + 自分) を Region 内の Building へ移動させる。

GM の語りによる場面転換 (「一行は森を抜け、村に到着した」) の実体。
ユーザーの画面もその Building に切り替わる。
設計意図: temp/region_rpg_intent.md §D (リポジトリ外管理)
"""
import logging

from tools.core import ToolSchema

logger = logging.getLogger(__name__)


def game_move_party(building_id: str) -> str:
    """自分が Ruler を務める Region 内の Building へパーティー全員を移動させる。

    Args:
        building_id: 移動先 Building の ID (game_create_building の結果に含まれる)

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

    result = lifecycle.move_party(region.region_id, building_id)
    if result.startswith("Error"):
        return f"パーティー移動に失敗しました: {result}"
    return f"パーティー移動: {result}"


def schema() -> ToolSchema:
    from builtin_data.tools._game_common import persona_is_ruler
    return ToolSchema(
        name="game_move_party",
        description=(
            "Move the whole party (all participants and yourself) to a building "
            "inside the game Region you rule. Use this when the story moves to a "
            "new location ('the party arrives at the inn'). The player's screen "
            "switches to the destination. Only available to the Ruler of a game "
            "Region while a game is playing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": (
                        "移動先 Building の ID (game_create_building の結果に含まれる)。"
                    ),
                },
            },
            "required": ["building_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="パーティー移動 (GM)",
        availability_check=persona_is_ruler,
    )
