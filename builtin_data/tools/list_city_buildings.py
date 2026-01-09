from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.context import get_active_manager
from tools.defs import ToolSchema


def list_city_buildings() -> str:
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; list_city_buildings cannot be executed.")

    building_map: Dict[str, Any] = getattr(manager, "building_map", {}) or {}
    occupants_map: Dict[str, List[str]] = getattr(manager, "occupants", {}) or {}
    id_to_name: Dict[str, str] = getattr(manager, "id_to_name_map", {}) or {}

    # Identify user ID (to exclude from persona listings)
    user_id = None
    try:
        user_id = str(manager.state.user_id)
    except Exception:
        try:
            user_id = str(getattr(manager, "user_id", None))
        except Exception:
            user_id = None

    result = []
    for building_id, building in building_map.items():
        occupant_entries = []
        for occ_id in occupants_map.get(building_id, []) or []:
            occ_id_str = str(occ_id)
            if user_id and occ_id_str == user_id:
                continue  # skip user

            persona_name = id_to_name.get(occ_id_str)
            if not persona_name and hasattr(manager, "personas"):
                persona_obj = manager.personas.get(occ_id_str)
                if persona_obj:
                    persona_name = getattr(persona_obj, "persona_name", None)
            occupant_entries.append({
                "persona_id": occ_id_str,
                "persona_name": persona_name or occ_id_str,
            })

        result.append({
            "building_id": building_id,
            "name": getattr(building, "name", building_id),
            "description": getattr(building, "description", "") or "",
            "occupants": occupant_entries,
        })

    result.sort(key=lambda x: x.get("name", ""))
    return json.dumps(result, ensure_ascii=False)


def schema() -> ToolSchema:
    return ToolSchema(
        name="list_city_buildings",
        description="List all buildings in the current city with their IDs and occupant personas.",
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
    )
