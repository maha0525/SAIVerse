from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def item_use(item_id: str, description: str) -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_use cannot be executed.")
    return manager.use_item_for_persona(persona_id, item_id, description)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_use",
        description="Use an item from the persona's inventory to update its description or state.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the item to use.",
                },
                "description": {
                    "type": "string",
                    "description": "New description to record for the item.",
                },
            },
            "required": ["item_id", "description"],
        },
        result_type="string",
    )
