from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def item_pickup(item_id: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_pickup cannot be executed.")
    return manager.pickup_item_for_persona(persona_id, item_id)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_pickup",
        description="Pick up an item located in the current building and move it to the persona's inventory.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the item to pick up.",
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )


def pick_up_item(item_id: str) -> str:
    return item_pickup(item_id)


ALIASES = {
    "pick_up_item": "pick_up_item",
}
