from __future__ import annotations

from typing import Optional

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def move_persona(building_id: str, persona_id: Optional[str] = None) -> str:
    active_persona_id = get_active_persona_id()
    target_persona_id = persona_id or active_persona_id

    if not target_persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context() or pass persona_id.")

    # Prevent moving someone else when running inside a persona context
    if active_persona_id and target_persona_id != active_persona_id:
        raise ValueError("別のペルソナを移動させることはできません。")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; move_persona cannot be executed.")

    if building_id not in getattr(manager, "building_map", {}):
        raise ValueError(f"建物 '{building_id}' が見つかりません。")

    persona_obj = getattr(manager, "personas", {}).get(target_persona_id)
    if not persona_obj:
        raise ValueError(f"Persona '{target_persona_id}' not found.")

    from_building = getattr(persona_obj, "current_building_id", None)
    if not from_building:
        raise ValueError("現在地を特定できませんでした。")

    if from_building == building_id:
        dest_name = getattr(manager.building_map.get(building_id), "name", building_id)
        return f"{persona_obj.persona_name} は既に {dest_name} にいます。"

    success, reason = manager._move_persona(target_persona_id, from_building, building_id)
    if not success:
        raise RuntimeError(f"移動できませんでした: {reason or '理由不明'}")

    persona_obj.current_building_id = building_id
    try:
        persona_obj._mark_entry(building_id)
    except Exception:
        pass

    src_name = getattr(manager.building_map.get(from_building), "name", from_building)
    dest_name = getattr(manager.building_map.get(building_id), "name", building_id)
    return f"{persona_obj.persona_name} を {src_name} から {dest_name} に移動しました。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="move_persona",
        description="Move the active persona to another building. (When called in persona context, persona_id must match the active persona.)",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Destination building identifier.",
                },
                "persona_id": {
                    "type": "string",
                    "description": "Persona ID (optional; defaults to the active persona and must match it in persona context).",
                },
            },
            "required": ["building_id"],
        },
        result_type="string",
    )
