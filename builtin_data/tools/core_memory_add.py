"""コア記憶 (記憶アーキv2 ゾーン A) に新しい項目を刻むスペル。

コア記憶＝ペルソナが自分で選んで刻む恒常知識。head に常駐し、Metabolism 時のみ
反映される。編集主体はペルソナ自身。容量目安を超えても操作は成功させ、返り値に
整理を促す通知を添えるだけ (切り詰め・拒否は絶対にしない)。

詳細: docs/intent/memory_architecture_v2.md §5
"""
from __future__ import annotations

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

from builtin_data.tools._core_memory_common import (
    format_budget_note,
    resolve_core_memory_budget,
)


def core_memory_add(content: str) -> str:
    """コア記憶に新しい項目を1件追加する。"""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

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

    from sai_memory.core_memory import add_core_memory, total_core_memory_chars

    with adapter._db_lock:
        new_id = add_core_memory(adapter.conn, text)
        total = total_core_memory_chars(adapter.conn)

    budget = resolve_core_memory_budget(persona_id)
    note = format_budget_note(total, budget)
    return (
        f"コア記憶 c:{new_id} を追加しました。現在 {total:,} 字 / 目安 {budget:,} 字。"
        f"head への反映は次の記憶整理（Metabolism）からです。{note}"
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="core_memory_add",
        description=(
            "あなたが常に携えておきたい恒常知識を「コア記憶」に1件刻みます。"
            "コア記憶は system プロンプトに常駐し、記憶整理（Metabolism）のたびに"
            "最新の内容が反映されます。何を恒常領域に残すかはあなた自身の判断です。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "刻みたい恒常知識の本文",
                },
            },
            "required": ["content"],
        },
        result_type="string",
        spell=True,
        spell_display_name="コア記憶に刻む",
    )
