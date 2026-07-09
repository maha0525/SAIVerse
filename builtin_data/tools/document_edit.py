"""document_edit — ドキュメントItemの内容を編集する統合スペル。

3つの編集操作を1本に集約している (旧 document_replace_content /
document_patch_content / document_append_content を統合):

- **部分置換 (patch)**: ``old_string`` / ``new_string`` を渡すと、文書内で
  一意にマッチする箇所だけを差し替える (外科的な修正向け)。
- **追記 (append)**: ``mode='append'`` + ``content`` で末尾に追記する。
- **全置換 (replace)**: ``content`` を渡すと全文を書き換える (推敲・書き直し向け)。

いずれも manager の document サービスを経由するので、``item:N`` 参照の解決と
アクセス制御 (自分のインベントリ or 現在いる建物のdocumentのみ)、編集後の
要約再生成が共通で効く。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def document_edit(
    item_id: str,
    content: Optional[str] = None,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    mode: Optional[str] = None,
) -> str:
    """Edit a document item's content (patch / append / replace).

    Args:
        item_id: Identifier of the document item (raw ID or ``item:N`` ref).
        content: Text for full replacement (default) or append (mode='append').
        old_string: Exact substring to find; presence selects patch mode.
        new_string: Replacement text for the matched substring (default '').
        mode: Optional explicit selector: 'patch' / 'append' / 'replace'.
              If omitted, inferred from the provided arguments.

    Returns:
        Confirmation message from the manager service.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona context is not set.")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("Manager context is not available.")

    item_id = manager.resolve_item_ref_for_persona(persona_id, item_id)

    # 操作の判定: 明示 mode > 引数からの推論。
    resolved_mode = (mode or "").strip().lower() or None
    if resolved_mode is None:
        if old_string is not None:
            resolved_mode = "patch"
        elif content is not None:
            resolved_mode = "replace"

    if resolved_mode == "patch":
        if old_string is None:
            return "部分置換 (patch) には old_string を指定してください。"
        return manager.patch_document_content(
            persona_id, item_id, old_string, new_string or "",
        )
    if resolved_mode == "append":
        if content is None:
            return "追記 (append) には content を指定してください。"
        return manager.append_document_content(persona_id, item_id, content)
    if resolved_mode == "replace":
        if content is None:
            return "全置換 (replace) には content を指定してください。"
        return manager.replace_document_content(persona_id, item_id, content)

    return (
        "編集内容を指定してください: 部分置換なら old_string(+new_string)、"
        "追記なら mode='append'+content、全置換なら content。"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="document_edit",
        description=(
            "Edit a document item. Three operations in one: "
            "(1) patch — give old_string (+new_string) to replace a single "
            "uniquely-matching substring; "
            "(2) append — set mode='append' with content to append to the end; "
            "(3) replace — give content alone to overwrite the whole document. "
            "Use document_read first to see the current content."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "Identifier of the document item (ID or item:N ref).",
                },
                "content": {
                    "type": "string",
                    "description": "Text for full replacement, or for append (with mode='append').",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact substring to find (must match one location). Selects patch mode.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement for old_string in patch mode. Omit to delete the match.",
                },
                "mode": {
                    "type": "string",
                    "description": "Explicit operation: 'patch' / 'append' / 'replace'. Inferred if omitted.",
                    "enum": ["patch", "append", "replace"],
                },
            },
            "required": ["item_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ドキュメント編集",
    )
