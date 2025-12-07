from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.defs import ToolSchema


def item_use(item_id: str, action_json: str) -> str:
    """
    Use an item to apply effects.

    Args:
        item_id: Identifier of the item to use.
        action_json: JSON string with action details.

    Action JSON schema:
        {
            "action_type": "update_description" | "patch_content",
            "description": "...",       # For update_description
            "patch": "..."              # For patch_content (document only)
        }
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_use cannot be executed.")
    return manager.use_item_for_persona(persona_id, item_id, action_json)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_use",
        description="Use an item from the persona's inventory to apply effects. Supports updating description for object/picture/document items, and patching content for document items.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the item to use.",
                },
                "action_json": {
                    "type": "string",
                    "description": "JSON string specifying the action. Schema: {\"action_type\": \"update_description\" | \"patch_content\", \"description\": \"...\" (for update_description), \"patch\": \"...\" (for patch_content on documents)}",
                },
            },
            "required": ["item_id", "action_json"],
        },
        result_type="string",
    )
