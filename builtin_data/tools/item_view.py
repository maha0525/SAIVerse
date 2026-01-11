from __future__ import annotations

from pathlib import Path

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def item_view(item_id: str) -> str:
    """
    View the full content of a picture or document item.

    Args:
        item_id: Identifier of the item to view.

    Returns:
        - picture: File path for display
        - document: Full text content of the file
        - object: Error message (not supported)
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_view cannot be executed.")

    return manager.view_item_for_persona(persona_id, item_id)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_view",
        description="View the full content of a picture or document item. Returns image path for pictures and full text for documents.",
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the item to view.",
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
    )
