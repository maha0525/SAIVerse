"""item_view — View item details. For bags, shows contents list."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def item_view(item_id: str = "", item_ids: str = "") -> str:
    """
    View the full content of items. For bags, shows the contents list.

    Args:
        item_id: Single item ID (for backward compatibility).
        item_ids: Comma-separated item IDs to view (max 5).

    Returns:
        - picture: File path for display
        - document: Full text content of the file
        - bag: Contents list with names, IDs, and descriptions
        - object: Description text
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set. Use tools.context.persona_context().")

    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available; item_view cannot be executed.")

    # Merge item_id and item_ids into a single list
    raw_ids = []
    if item_ids:
        raw_ids = [s.strip() for s in item_ids.split(",") if s.strip()]
    if item_id and item_id not in raw_ids:
        raw_ids.insert(0, item_id)

    if not raw_ids:
        raise RuntimeError("閲覧するアイテムIDが指定されていません。")

    ids = [manager.resolve_item_ref_for_persona(persona_id, ref) for ref in raw_ids]
    return manager.view_items_for_persona(persona_id, ids)


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_view",
        description=(
            "View item details. For pictures: shows the image. "
            "For documents: shows full text. For bags: shows contents list. "
            "Supports viewing multiple items at once (max 5)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Single item ID to view.",
                },
                "item_ids": {
                    "type": "string",
                    "description": "Comma-separated item IDs to view (max 5).",
                },
            },
        },
        result_type="string",
        spell=True,
        spell_display_name="アイテム閲覧",
    )
