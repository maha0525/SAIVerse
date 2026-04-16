"""item_move — Move items between buildings, persona inventory, and bags."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def item_move(item_ids: str, destination_type: str, destination_id: str = "") -> str:
    """
    Move one or more items to a destination.

    Args:
        item_ids: Comma-separated item IDs to move (max 100).
        destination_type: "building", "persona", or "bag".
        destination_id: Target ID. For building: building_id.
            For persona: leave empty (moves to own inventory).
            For bag: the bag item's ID.

    Returns:
        Summary of moved items.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_move cannot be executed.")

    # Parse item IDs
    ids = [s.strip() for s in item_ids.split(",") if s.strip()]
    if not ids:
        raise RuntimeError("移動するアイテムIDが指定されていません。")

    return manager.move_item_for_persona(persona_id, ids, destination_type, destination_id)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_move",
        description=(
            "Move items to a building, your inventory, or inside a bag. "
            "Specify comma-separated item IDs and a destination."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_ids": {
                    "type": "string",
                    "description": "Comma-separated item IDs to move (max 100).",
                },
                "destination_type": {
                    "type": "string",
                    "enum": ["building", "persona", "bag"],
                    "description": "Destination type: 'building', 'persona' (own inventory), or 'bag'.",
                },
                "destination_id": {
                    "type": "string",
                    "description": (
                        "Destination ID: building_id for building, "
                        "leave empty for persona (own inventory), "
                        "or bag item_id for bag."
                    ),
                },
            },
            "required": ["item_ids", "destination_type"],
        },
        result_type="string",
        spell=True,
        spell_display_name="アイテム移動",
    )
