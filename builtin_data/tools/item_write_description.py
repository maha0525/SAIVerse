"""item_write_description — Overwrite an item's description."""
from __future__ import annotations

from tools.context import get_active_manager
from tools.core import ToolSchema


def item_write_description(item_id: str, description: str) -> str:
    """
    Overwrite the description (概要) of an item.

    Args:
        item_id: ID of the item to update.
        description: New description text.

    Returns:
        Confirmation message.
    """
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    if not item_id:
        raise RuntimeError("item_id が指定されていません。")

    item = manager.items.get(item_id)
    if not item:
        raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")

    manager.update_item_description(item_id, description.strip())
    item_name = item.get("name", item_id)
    return f"「{item_name}」の概要を更新しました。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_write_description",
        description=(
            "Update the description (概要) of an item. "
            "Use this when an item's name is a meaningless string, "
            "or its description is inaccurate or insufficient. "
            "Actively use this spell to make items easier to manage in the future."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the item whose description should be updated.",
                },
                "description": {
                    "type": "string",
                    "description": "New description text for the item.",
                },
            },
            "required": ["item_id", "description"],
        },
        result_type="string",
        spell=True,
        spell_display_name="アイテム概要の書き換え",
    )
