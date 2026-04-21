"""Chronicle upstream context: get parent entry and all sibling entries."""

from __future__ import annotations

from datetime import datetime

from sai_memory.arasuji.storage import get_entry, get_children
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def _fmt_time(ts) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def chronicle_context_up(entry_id: str) -> str:
    """Get the upstream context for a Chronicle entry.

    Returns the parent (higher-level) Chronicle and all sibling entries at the
    same level that belong to the same parent. This lets you understand the
    broader context around the specified entry, and navigate further up or
    sideways using the returned URIs.

    For a Lv1 entry: returns the Lv2 parent summary + all Lv1 siblings.
    For a Lv2 entry: returns the Lv3 parent summary + all Lv2 siblings.

    If the entry has no parent yet (not yet consolidated), reports that fact.

    Args:
        entry_id: Chronicle entry UUID (from memory_recall_unified or chronicle_search).
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

    with adapter._db_lock:
        entry = get_entry(adapter.conn, entry_id)

    if not entry:
        return f"(Chronicle entry が見つかりません: {entry_id})"

    entry_uri = f"saiverse://self/chronicle/entry/{entry.id}"
    lines = [
        "【Chronicle上流参照】",
        f"参照元: Lv{entry.level} | {_fmt_time(entry.start_time)} ~ {_fmt_time(entry.end_time)} | {entry.message_count}件",
        f"URI: {entry_uri}",
        "",
    ]

    if not entry.parent_id:
        lines.append("(このエントリはまだ上位Chronicleにまとめられていません)")
        return "\n".join(lines)

    with adapter._db_lock:
        parent = get_entry(adapter.conn, entry.parent_id)

    if not parent:
        lines.append(f"(親エントリの取得に失敗しました: {entry.parent_id})")
        return "\n".join(lines)

    parent_uri = f"saiverse://self/chronicle/entry/{parent.id}"
    lines += [
        f"--- 親エントリ (Lv{parent.level}) ---",
        f"URI: {parent_uri}",
        f"期間: {_fmt_time(parent.start_time)} ~ {_fmt_time(parent.end_time)} | {parent.message_count}件",
        "",
        parent.content,
        "",
    ]

    with adapter._db_lock:
        siblings = get_children(adapter.conn, parent.id)

    # Remove self from siblings display to avoid duplication
    siblings = [s for s in siblings if s.id != entry.id]

    lines.append(f"--- 兄弟エントリ (Lv{entry.level}, 自身を除く {len(siblings)}件) ---")
    if not siblings:
        lines.append("(他のエントリはありません)")
    else:
        for sib in siblings:
            sib_uri = f"saiverse://self/chronicle/entry/{sib.id}"
            lines.append(f"[{sib.id}] {_fmt_time(sib.start_time)} ~ {_fmt_time(sib.end_time)} | {sib.message_count}件")
            lines.append(f"URI: {sib_uri}")
            lines.append(sib.content)
            lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="chronicle_context_up",
        description=(
            "指定したChronicleエントリの上流コンテキストを取得します。"
            "親エントリ（上位レベルの要約）の全文と、同じ親に属する兄弟エントリ全件の"
            "全文とURIを返します。周辺の状況を把握し、さらに上位や横のエントリへ"
            "ナビゲートするための足がかりになります。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "Chronicle entry UUID（memory_recall_unified や chronicle_search の結果から）",
                },
            },
            "required": ["entry_id"],
        },
        result_type="string",
        spell=True,
        spell_display_name="Chronicle上流参照",
    )
