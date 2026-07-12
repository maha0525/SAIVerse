"""Edit a Memopedia fragment's content.

P4 庭仕事ワーカーの素材として内部専用化 (concept_consolidation.md)。
"""

from __future__ import annotations

from tools.context import get_active_persona_id, open_persona_memory
from tools.core import ToolSchema


def memopedia_edit_fragment(
    fragment_id: str,
    content: str,
) -> str:
    """Edit the content of an existing fragment.

    Use memopedia_list_fragments first to find the fragment ID.
    The embedding for this fragment will need to be regenerated.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    fid = fragment_id.strip()
    new_content = content.strip()
    if not new_content:
        return "Error: content は空にできません"

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")

        conn = adapter.conn
        with adapter._db_lock:
            cur = conn.execute(
                "SELECT id, content FROM memopedia_fragments WHERE id = ?", (fid,)
            )
            row = cur.fetchone()
            if row is None:
                return f"フラグメントが見つかりません: {fid}"

            conn.execute(
                "UPDATE memopedia_fragments SET content = ? WHERE id = ?",
                (new_content, fid),
            )
            # Invalidate old embedding so it gets regenerated on next batch
            conn.execute(
                "DELETE FROM memopedia_fragment_embeddings WHERE fragment_id = ?", (fid,)
            )
            conn.commit()

    return f"更新しました: {new_content[:60]}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_edit_fragment",
        description=(
            "Memopediaのフラグメント（断片知識）の内容を編集します。"
            "重複の統合や誤りの修正に使用してください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "fragment_id": {
                    "type": "string",
                    "description": "編集するフラグメントのID",
                },
                "content": {
                    "type": "string",
                    "description": "新しい内容",
                },
            },
            "required": ["fragment_id", "content"],
        },
        result_type="string",
        spell=False,
        spell_display_name="フラグメント編集",
    )
