"""Ruler 専用: game Region 内に SubRegion (街・ダンジョン等のエリア) を作成する。

設計意図: temp/region_rpg_intent.md §A, §F (リポジトリ外管理)
"""
import logging

from tools.core import ToolSchema

logger = logging.getLogger(__name__)


def game_create_subregion(name: str, description: str) -> str:
    """自分が Ruler を務める Region 内に SubRegion を作成する。

    Args:
        name: SubRegion の名前 (街・ダンジョン・エリアの名称)
        description: 概要 (雰囲気・特徴・役割)

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

    result = manager.create_region(
        name=name,
        description=description,
        region_type="game",
        city_id=manager.city_id,
        parent_region_id=region.region_id,
    )
    if result.startswith("Error"):
        return f"SubRegion の作成に失敗しました: {result}"

    subregion_id = extract_id_from_result(result)
    logger.info(
        "[game_create_subregion] Created '%s' (ID: %s) in region '%s'",
        name, subregion_id, region.region_id,
    )
    return (
        f"SubRegion「{name}」(ID: {subregion_id}) を Region『{region.name}』内に作成しました。\n"
        "建物を配置するには game_create_building を subregion_id 付きで使用してください。"
    )


def schema() -> ToolSchema:
    from builtin_data.tools._game_common import persona_is_ruler
    return ToolSchema(
        name="game_create_subregion",
        description=(
            "Create a SubRegion (an area such as a town, dungeon, or wilderness zone) "
            "inside the game Region you rule. SubRegions group buildings into a "
            "geographic area. Only available to the Ruler of a game Region."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "SubRegion (エリア) の名前。例: '霧降りの森', '忘れられた坑道'",
                },
                "description": {
                    "type": "string",
                    "description": "エリアの概要。雰囲気・特徴・どんな場所か。",
                },
            },
            "required": ["name", "description"],
        },
        result_type="string",
        spell=True,
        spell_display_name="エリア作成 (GM)",
        availability_check=persona_is_ruler,
    )
