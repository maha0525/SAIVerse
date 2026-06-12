import json
import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


class Region:
    """Building の上位グルーピングのランタイム表現。

    PARENT_REGION_ID の自己参照で 1 段の入れ子 (トップ Region > SubRegion) を表現する。
    region_type='game' のトップ Region は Ruler (GM ペルソナ)・ゲーム進行状態を持つ。
    すべての Region / SubRegion は入口 Building (entrance_building_id) を持つ。
    入口は親スコープに属し、このポインタで子スコープに結び付く。
    設計意図: docs/intent/region.md (汎用仕様) / temp/region_rpg_intent.md (RPG 固有、リポジトリ外管理)
    """

    def __init__(
        self,
        region_id: str,
        city_id: int,
        name: str,
        parent_region_id: Optional[str] = None,
        description: str = "",
        region_type: str = "generic",
        ruler_id: Optional[str] = None,
        entrance_building_id: Optional[str] = None,
        map_background_image: Optional[str] = None,
        state_json: Optional[str] = None,
        config_json: Optional[str] = None,
    ):
        self.region_id = region_id
        self.city_id = city_id
        self.name = name
        self.parent_region_id = parent_region_id
        self.description = description or ""
        self.region_type = region_type or "generic"
        self.ruler_id = ruler_id
        self.entrance_building_id = entrance_building_id
        self.map_background_image = map_background_image
        self.state: Dict[str, Any] = self._parse_json_field(state_json, "STATE_JSON")
        self.config: Dict[str, Any] = self._parse_json_field(config_json, "CONFIG_JSON")

    def _parse_json_field(self, raw: Optional[str], field_name: str) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                LOGGER.error(
                    "Region %s: %s is not a JSON object (got %s); treating as empty",
                    self.region_id, field_name, type(parsed).__name__,
                )
                return {}
            return parsed
        except json.JSONDecodeError:
            LOGGER.error(
                "Region %s: failed to parse %s; treating as empty",
                self.region_id, field_name, exc_info=True,
            )
            return {}

    @property
    def is_subregion(self) -> bool:
        return self.parent_region_id is not None

    @property
    def is_game_region(self) -> bool:
        """ゲーム空間のトップ Region か (SubRegion は game type でも GM 機能を持たない)。"""
        return self.region_type == "game" and not self.is_subregion
