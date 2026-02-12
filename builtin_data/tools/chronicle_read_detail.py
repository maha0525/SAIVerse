"""Read a Chronicle entry's detail and optionally its source messages."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from sai_memory.arasuji.storage import get_entry, ArasujiEntry
from sai_memory.memory.storage import get_message
from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def chronicle_read_detail(
    entry_id: str,
    include_sources: bool = True,
    max_source_messages: int = 20,
) -> str:
    """Read a Chronicle entry and optionally its source messages/child entries.

    Args:
        entry_id: Chronicle entry UUID
        include_sources: Whether to include source messages (level 1) or child entries (level 2+)
        max_source_messages: Maximum source messages to include

    Returns:
        Formatted detail text
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
        return f"(Chronicle entry not found: {entry_id})"

    # Format header
    start = datetime.fromtimestamp(entry.start_time).strftime("%Y-%m-%d %H:%M") if entry.start_time else "?"
    end = datetime.fromtimestamp(entry.end_time).strftime("%Y-%m-%d %H:%M") if entry.end_time else "?"

    parts = [
        f"【Chronicle Entry】{entry.id}",
        f"Level: {entry.level} | {start} ~ {end} | {entry.message_count}件のメッセージ",
        f"Consolidated: {'Yes' if entry.is_consolidated else 'No'}",
        "",
        entry.content,
    ]

    if include_sources and entry.source_ids:
        parts.append("")

        if entry.level == 1:
            # Level 1: sources are message UUIDs
            parts.append(f"--- 元のメッセージ ({len(entry.source_ids)}件, 最大{max_source_messages}件表示) ---")

            count = 0
            with adapter._db_lock:
                for msg_id in entry.source_ids[:max_source_messages]:
                    msg = get_message(adapter.conn, msg_id)
                    if msg:
                        dt = datetime.fromtimestamp(msg.created_at)
                        ts = dt.strftime("%H:%M:%S")
                        role = msg.role if msg.role != "model" else "assistant"
                        content = (msg.content or "").strip()
                        parts.append(f"[{ts}] [{role}]: {content}")
                        count += 1

            if count == 0:
                parts.append("(元のメッセージを取得できませんでした)")
        else:
            # Level 2+: sources are child arasuji entry UUIDs
            parts.append(f"--- 子エントリ ({len(entry.source_ids)}件) ---")

            with adapter._db_lock:
                for child_id in entry.source_ids:
                    child = get_entry(adapter.conn, child_id)
                    if child:
                        c_start = datetime.fromtimestamp(child.start_time).strftime("%Y-%m-%d %H:%M") if child.start_time else "?"
                        c_end = datetime.fromtimestamp(child.end_time).strftime("%Y-%m-%d %H:%M") if child.end_time else "?"
                        parts.append(f"[{child.id}] Lv.{child.level} | {c_start} ~ {c_end} | {child.message_count}msg")
                        # Show full child content
                        parts.append(f"  {child.content}")
                        parts.append("")

    return "\n".join(parts)


def schema() -> ToolSchema:
    return ToolSchema(
        name="chronicle_read_detail",
        description=(
            "Read a Chronicle (arasuji) entry in detail, including its source messages "
            "(for level 1) or child summary entries (for level 2+). "
            "Use after chronicle_search to drill into a specific entry."
        ),
        parameters={
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The Chronicle entry UUID to read",
                },
                "include_sources": {
                    "type": "boolean",
                    "description": "Include source messages (level 1) or child entries (level 2+). Default: true.",
                    "default": True,
                },
                "max_source_messages": {
                    "type": "integer",
                    "description": "Max source messages to include for level-1 entries. Default: 20.",
                    "default": 20,
                },
            },
            "required": ["entry_id"],
        },
        result_type="string",
    )
