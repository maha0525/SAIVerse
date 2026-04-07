"""Manage Memopedia pages: delete, move, set vividness, set important flag."""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

VALID_ACTIONS = {"delete", "move", "set_vividness", "set_important"}
VALID_VIVIDNESS = {"vivid", "rough", "faint", "buried"}


def memopedia_manage(
    action: str,
    page_id: str,
    new_parent_id: Optional[str] = None,
    vividness: Optional[str] = None,
    is_important: Optional[bool] = None,
) -> str:
    """Manage a Memopedia page (delete, move, change vividness, set important).

    Args:
        action: One of: delete, move, set_vividness, set_important
        page_id: Target page ID (or first chars for prefix match)
        new_parent_id: For move action: destination parent page ID
        vividness: For set_vividness: vivid/rough/faint/buried
        is_important: For set_important: true/false
    """
    if action not in VALID_ACTIONS:
        return f"不明なアクション: {action}（使用可能: {', '.join(sorted(VALID_ACTIONS))}）"

    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    from saiverse_memory import SAIMemoryAdapter
    persona_dir = get_active_persona_path()
    adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)

    if not adapter.is_ready():
        return "Memopedia: データベースにアクセスできません"

    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    init_memopedia_tables(adapter.conn)
    memopedia = Memopedia(adapter.conn)

    # Verify page exists
    page = memopedia.get_page(page_id)
    if not page:
        return f"ページが見つかりません: {page_id}"

    if action == "delete":
        if page.parent_id and page.parent_id.startswith("root_"):
            # Direct child of category root - warn about children
            children = page.children if hasattr(page, "children") else []
            if children:
                return (
                    f"警告: '{page.title}' には子ページがあります。"
                    f"削除すると子ページも全て削除されます。"
                    f"本当に削除する場合はもう一度このツールを呼んでください。"
                )
        result = memopedia.delete_page(
            page_id,
            edit_source="autonomy_manage",
        )
        if result:
            return f"ページ '{page.title}' を削除しました"
        return f"ページ '{page.title}' の削除に失敗しました"

    elif action == "move":
        if not new_parent_id:
            return "move アクションには new_parent_id が必要です"
        from sai_memory.memopedia.storage import move_pages_to_parent
        count = move_pages_to_parent(adapter.conn, [page_id], new_parent_id)
        if count > 0:
            return f"ページ '{page.title}' を移動しました (新しい親: {new_parent_id})"
        return f"ページ '{page.title}' の移動に失敗しました"

    elif action == "set_vividness":
        if not vividness or vividness not in VALID_VIVIDNESS:
            return f"set_vividness には vividness パラメータが必要です（{', '.join(sorted(VALID_VIVIDNESS))}）"
        from sai_memory.memopedia.storage import update_page
        result = update_page(adapter.conn, page_id, vividness=vividness)
        if result:
            return f"ページ '{page.title}' の鮮明度を '{vividness}' に変更しました"
        return "鮮明度の変更に失敗しました"

    elif action == "set_important":
        if is_important is None:
            return "set_important には is_important パラメータ (true/false) が必要です"
        result = memopedia.set_important(page_id, is_important)
        if result:
            flag = "重要" if is_important else "通常"
            return f"ページ '{page.title}' を{flag}に設定しました"
        return "重要フラグの変更に失敗しました"

    return f"未実装のアクション: {action}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_manage",
        description=(
            "Memopediaページの管理操作を行います。"
            "ページの削除、移動（親ページ変更）、鮮明度変更、重要フラグの設定が可能です。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["delete", "move", "set_vividness", "set_important"],
                    "description": "実行する操作",
                },
                "page_id": {
                    "type": "string",
                    "description": "対象ページのID",
                },
                "new_parent_id": {
                    "type": "string",
                    "description": "moveアクション時: 移動先の親ページID",
                },
                "vividness": {
                    "type": "string",
                    "enum": ["vivid", "rough", "faint", "buried"],
                    "description": "set_vividnessアクション時: 新しい鮮明度",
                },
                "is_important": {
                    "type": "boolean",
                    "description": "set_importantアクション時: 重要フラグ (true/false)",
                },
            },
            "required": ["action", "page_id"],
        },
        result_type="string",
    )
