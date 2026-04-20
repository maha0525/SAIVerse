"""item_change_name — Rename an item."""
from __future__ import annotations

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def item_change_name(item_id: str, name: str) -> str:
    """
    Rename an item.

    Args:
        item_id: ID of the item to rename.
        name: New name for the item.

    Returns:
        Confirmation message.
    """
    persona_id = get_active_persona_id()
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    if not item_id:
        raise RuntimeError("item_id が指定されていません。")
    if not name or not name.strip():
        raise RuntimeError("name が空です。")

    if persona_id:
        item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)
    item = manager.items.get(item_id)
    if not item:
        raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")

    old_name = item.get("name", item_id)
    manager.update_item_name(item_id, name.strip())
    return f"アイテムの名前を「{old_name}」→「{name.strip()}」に変更しました。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_change_name",
        description=(
            "Rename an item. "
            "Use this when an item's name is a meaningless string (e.g. a raw filename like "
            "'20260401025103-01KN2E0MDX07MH2C234DMP2AM1.jpg') or otherwise hard to identify. "
            "Actively use this spell to give items descriptive names for easier management."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the item to rename.",
                },
                "name": {
                    "type": "string",
                    "description": "New name for the item.",
                },
            },
            "required": ["item_id", "name"],
        },
        result_type="string",
        spell=True,
        spell_display_name="アイテム名の変更",
    )
