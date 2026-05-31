"""Delete a Memopedia fragment by ID."""

from __future__ import annotations

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memopedia_delete_fragment(
    fragment_id: str,
) -> str:
    """Delete a single fragment from Memopedia.

    Use memopedia_list_fragments first to find the fragment ID.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    fid = fragment_id.strip()
    conn = adapter.conn
    with adapter._db_lock:
        cur = conn.execute(
            "SELECT id, content, entity_id FROM memopedia_fragments WHERE id = ?",
            (fid,),
        )
        row = cur.fetchone()
        if row is None:
            return f"フラグメントが見つかりません: {fid}"

        content_preview = row[1][:60] if row[1] else ""
        conn.execute("DELETE FROM memopedia_fragment_embeddings WHERE fragment_id = ?", (fid,))
        conn.execute("DELETE FROM memopedia_fragments WHERE id = ?", (fid,))
        conn.commit()

    return f"���除しました: {content_preview}"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memopedia_delete_fragment",
        description=(
            "Memopediaのフラグメント（断片知識）を1件削除します。"
            "memopedia_list_fragments で確認したIDを指定してください。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "fragment_id": {
                    "type": "string",
                    "description": "削除するフラグメントのID",
                },
            },
            "required": ["fragment_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="フラグメント削除",
    )
