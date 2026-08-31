"""item_annotate — アイテムの名前 / 概要を編集する統合スペル。

旧 item_change_name (名前変更) と item_write_description (概要書き換え) を
1本に統合したもの。name / description は両方任意で、指定した方だけ更新する
(片方以上は必須)。ファイル名そのままの意味のないアイテム名や、不正確・不十分な
概要を、管理しやすい形に直すために使う。
"""
from __future__ import annotations

from typing import Optional

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema


def item_annotate(
    item_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Update an item's name and/or description.

    Args:
        item_id: ID of the item (raw ID or item:N ref).
        name: New name. Omit to leave the name unchanged.
        description: New description (概要). Omit to leave it unchanged.

    Returns:
        Confirmation message describing what changed.
    """
    persona_id = get_active_persona_id()
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")
    if not item_id:
        raise RuntimeError("item_id が指定されていません。")

    name_given = name is not None and name.strip() != ""
    desc_given = description is not None
    if not name_given and not desc_given:
        raise RuntimeError("name か description のいずれかを指定してください。")

    if persona_id:
        item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)
    item = manager.items.get(item_id)
    if not item:
        raise RuntimeError(f"アイテム '{item_id}' が見つかりません。")

    old_name = item.get("name", item_id)
    changes = []
    if name_given:
        manager.update_item_name(item_id, name.strip())
        changes.append(f"名前を「{old_name}」→「{name.strip()}」に変更")
    if desc_given:
        manager.update_item_description(item_id, description.strip())
        changes.append("概要を更新")

    display_name = name.strip() if name_given else old_name
    return f"「{display_name}」の" + "、".join(changes) + "しました。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="item_annotate",
        description=(
            "Update an item's name and/or description (概要). Provide name, "
            "description, or both (at least one is required). "
            "Use this when an item's name is a meaningless string (e.g. a raw "
            "filename like '20260401025103-01KN2E0MDX07MH2C234DMP2AM1.jpg') or "
            "its description is inaccurate. Actively rename/annotate items to "
            "keep them easy to manage."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "ID of the item to annotate (ID or item:N ref).",
                },
                "name": {
                    "type": "string",
                    "description": "New name for the item. Omit to keep the current name.",
                },
                "description": {
                    "type": "string",
                    "description": "New description (概要). Omit to keep the current description.",
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="アイテム名・概要の編集",
    )
