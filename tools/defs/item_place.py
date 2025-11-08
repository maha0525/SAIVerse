from __future__ import annotations

from typing import Optional

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def item_place(item_id: str, building_id: Optional[str] = None) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_place cannot be executed.")
    return manager.place_item_from_persona(persona_id, item_id, building_id=building_id)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_place",
        description="Place an item from the persona's inventory into the current building (or a specified building).",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the item to place.",
                },
                "building_id": {
                    "type": "string",
                    "description": "Optional target building identifier. Defaults to the persona's current location.",
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )
