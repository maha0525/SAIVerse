"""Chronicle downstream context: raw messages (Lv1) or child entries (Lv2+)."""

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


def chronicle_context_down(entry_id: str) -> str:
    """Get the downstream content for a Chronicle entry.

    For a Lv1 entry: returns all raw source messages that the Chronicle
    summarizes. This lets you fully recall the actual conversation.

    For a Lv2+ entry: returns all child Chronicle entries (one level down)
    with their full content and URIs. You can then call chronicle_context_down
    on any of the child entries to drill further.

    Note: For Lv1 entries with many messages this response can be large.

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
        "【Chronicle下流参照】",
        f"参照元: Lv{entry.level} | {_fmt_time(entry.start_time)} ~ {_fmt_time(entry.end_time)} | {entry.message_count}件",
        f"URI: {entry_uri}",
        "",
        entry.content,
        "",
    ]

    if not entry.source_ids:
        lines.append("(ソースが見つかりません)")
        return "\n".join(lines)

    if entry.level == 1:
        # Raw messages
        from sai_memory.memory.storage import get_message
        lines.append(f"--- 元のメッセージ ({len(entry.source_ids)}件) ---")

        with adapter._db_lock:
            messages = []
            for msg_id in entry.source_ids:
                msg = get_message(adapter.conn, msg_id)
                if msg:
                    messages.append(msg)

        messages.sort(key=lambda m: m.created_at)
        for msg in messages:
            ts = datetime.fromtimestamp(msg.created_at).strftime("%Y-%m-%d %H:%M:%S")
            role = msg.role if msg.role != "model" else "assistant"
            content = (msg.content or "").strip()
            lines.append(f"[{ts}] [{role}]: {content}")

        if not messages:
            lines.append("(元のメッセージを取得できませんでした)")

    else:
        # Child Chronicle entries
        with adapter._db_lock:
            children = get_children(adapter.conn, entry.id)

        lines.append(f"--- 子エントリ (Lv{entry.level - 1}, {len(children)}件) ---")
        if not children:
            lines.append("(子エントリが見つかりません)")
        else:
            for child in children:
                child_uri = f"saiverse://self/chronicle/entry/{child.id}"
                lines.append(f"[{child.id}] {_fmt_time(child.start_time)} ~ {_fmt_time(child.end_time)} | {child.message_count}件")
                lines.append(f"URI: {child_uri}")
                lines.append(child.content)
                lines.append("")

    return "\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="chronicle_context_down",
        description=(
            "指定したChronicleエントリの下流コンテンツを取得します。"
            "Lv1エントリに対して使うと、そのChronicleがまとめている生のメッセージ全件を返します。"
            "Lv2以上に対して使うと、子ChronicleエントリのURIと全文を返します。"
            "実際のやり取りを完全に思い出したいときや、段階的に掘り下げていくときに使います。"
            "注意: Lv1で使うとメッセージ件数によって応答が大きくなります。"
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
        spell_display_name="Chronicle下流参照",
    )
