"""コア記憶 (記憶アーキv2 ゾーン A) の既存項目を書き換えるスペル。

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


def core_memory_update(memory_id, content: str) -> str:
    """既存のコア記憶を書き換える。memory_id は ``c:3`` / ``3`` どちらでも可。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    mid = parse_core_memory_id(memory_id)
    if mid is None:
        return f"Error: memory_id の形式が正しくありません: {memory_id}（例: c:3）"

    text = (content or "").strip()
    if not text:
        return "Error: content は空にできません。"

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    from sai_memory.core_memory import total_core_memory_chars, update_core_memory

    with adapter._db_lock:
        ok = update_core_memory(adapter.conn, mid, text)
        total = total_core_memory_chars(adapter.conn)

    if not ok:
        return f"コア記憶 c:{mid} は見つかりませんでした。"

    budget = resolve_core_memory_budget(persona_id)
    note = format_budget_note(total, budget)
    return (
        f"コア記憶 c:{mid} を書き換えました。現在 {total:,} 字 / 目安 {budget:,} 字。"
        f"head への反映は次の記憶整理（Metabolism）からです。{note}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="core_memory_update",
        description=(
            "コア記憶の既存の1項目を新しい内容に書き換えます。"
            "memory_id は c:3 のような参照で指定します（数字だけでも構いません）。"
            "反映は次の記憶整理（Metabolism）からです。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "書き換えるコア記憶の参照（例: c:3 または 3）",
                },
                "content": {
                    "type": "string",
                    "description": "新しい本文",
                },
            },
            "required": ["memory_id", "content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="コア記憶を書き換える",
    )
