"""コア記憶 (記憶アーキv2 ゾーン A) から項目を消すスペル。

詳細: docs/intent/memory_architecture_v2.md §5
"""
from __future__ import annotations

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import (
    format_budget_note,
    parse_core_memory_id,
    resolve_core_memory_budget,
)


def core_memory_remove(memory_id) -> str:
    """コア記憶を1件削除する。memory_id は ``c:3`` / ``3`` どちらでも可。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    mid = parse_core_memory_id(memory_id)
    if mid is None:
        return f"Error: memory_id の形式が正しくありません: {memory_id}（例: c:3）"

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.core_memory import remove_core_memory, total_core_memory_chars

    with adapter._db_lock:
        ok = remove_core_memory(adapter.conn, mid)
        total = total_core_memory_chars(adapter.conn)

    if not ok:
        return f"コア記憶 c:{mid} は見つかりませんでした。"

    budget = resolve_core_memory_budget(persona_id)
    note = format_budget_note(total, budget)
    return (
        f"コア記憶 c:{mid} を削除しました。現在 {total:,} 字 / 目安 {budget:,} 字。"
        f"head への反映は次の記憶整理（Metabolism）からです。{note}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="core_memory_remove",
        description=(
            "コア記憶から1項目を削除します。"
            "memory_id は c:3 のような参照で指定します（数字だけでも構いません）。"
            "反映は次の記憶整理（Metabolism）からです。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "削除するコア記憶の参照（例: c:3 または 3）",
                },
            },
            "required": ["memory_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="コア記憶から消す",
    )
