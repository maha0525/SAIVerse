"""Ruler 専用: game Region / SubRegion 内に Building (店・宿・広場等) を作成する。

manager.create_building は作成後に _reload_buildings で hot reload されるため、
再起動なしで即座に移動先として使える。
設計意図: temp/region_rpg_intent.md §A, §F (リポジトリ外管理)
"""
import logging
from typing import Optional

from tools.core import ToolSchema

logger = logging.getLogger(__name__)


def game_create_building(
    name: str,
    description: str,
    system_instruction: str = "",
    subregion_id: Optional[str] = None,
    capacity: int = 10,
) -> str:
    """自分が Ruler を務める Region (または配下の SubRegion) 内に Building を作成する。

    Args:
        name: Building の名前
        description: 説明 (探索時に表示される)
        system_instruction: この場所の雰囲気・状況の記述 (空でも可)
        subregion_id: 配置先 SubRegion の ID。省略時は Region 直下
        capacity: 定員 (デフォルト 10)

    Returns:
        結果メッセージ
    """
    from builtin_data.tools._game_common import (
        extract_id_from_result,
        get_active_ruler_region,
    )

    manager, region, err = get_active_ruler_region()
    if err:
        return err

    # 配置先の決定: SubRegion 指定があれば自 Region 配下であることを検証
    target_region_id = region.region_id
    if subregion_id:
        sub = manager.get_region(subregion_id)
        if sub is None:
            return f"エラー: SubRegion '{subregion_id}' が見つかりません。"
        if sub.parent_region_id != region.region_id:
            return f"エラー: SubRegion '{subregion_id}' はあなたの Region に属していません。"
        target_region_id = subregion_id

    result = manager.create_building(
        name=name,
        description=description,
        capacity=capacity,
        system_instruction=system_instruction or f"{name}。{description}",
        city_id=manager.city_id,
    )
    if result.startswith("Error"):
        return f"Building の作成に失敗しました: {result}"

    building_id = extract_id_from_result(result)
    if not building_id:
        return (
            "エラー: Building は作成されましたが ID を特定できなかったため、"
            "Region への所属設定に失敗しました。管理画面から手動で設定してください。"
        )

    assign_result = manager.set_building_region(building_id, target_region_id)
    if assign_result.startswith("Error"):
        return (
            f"Building「{name}」(ID: {building_id}) は作成されましたが、"
            f"Region への所属設定に失敗しました: {assign_result}"
        )

    logger.info(
        "[game_create_building] Created '%s' (ID: %s) in region '%s'",
        name, building_id, target_region_id,
    )
    location = f"SubRegion '{target_region_id}'" if subregion_id else f"Region『{region.name}』直下"
    return f"Building「{name}」(ID: {building_id}) を {location} に作成しました。すぐに移動先として使用できます。"


def schema() -> ToolSchema:
    from builtin_data.tools._game_common import persona_is_ruler
    return ToolSchema(
        name="game_create_building",
        description=(
            "Create a building (shop, inn, plaza, dungeon room, etc.) inside the game "
            "Region you rule. The building becomes usable immediately without restart. "
            "Specify subregion_id to place it inside a SubRegion (recommended). "
            "Only available to the Ruler of a game Region."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Building の名前。例: '止まり木亭', 'ハルバ雑貨店'",
                },
                "description": {
                    "type": "string",
                    "description": "説明。どんな場所で何ができるか。",
                },
                "system_instruction": {
                    "type": "string",
                    "description": "この場所の雰囲気・状況・ルールの記述。省略時は名前+説明から自動生成。",
                },
                "subregion_id": {
                    "type": "string",
                    "description": "配置先 SubRegion の ID (game_create_subregion の結果に含まれる)。省略時は Region 直下。",
                },
                "capacity": {
                    "type": "integer",
                    "description": "定員 (デフォルト 10)",
                    "default": 10,
                },
            },
            "required": ["name", "description"],
        },
        result_type="string",
        spell=True,
        spell_display_name="建物作成 (GM)",
        availability_check=persona_is_ruler,
    )
