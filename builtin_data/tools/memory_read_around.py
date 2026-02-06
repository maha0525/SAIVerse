"""Read messages around a specific message ID for context expansion."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.storage import (
    get_message,
    get_messages_around,
)
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def memory_read_around(
    message_id: str,
    window: int = 10,
) -> str:
    """Read messages surrounding a specific message ID.

    Returns the conversation context around the target message,
    formatted as a timestamped conversation log.

    - message_id: the ID of the center message
    - window: number of messages before and after to include
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

    # Get the anchor message
    with adapter._db_lock:
        anchor = get_message(adapter.conn, message_id)

    if not anchor:
        return f"(message not found: {message_id})"

    # Get surrounding messages using efficient rowid-based query
    with adapter._db_lock:
        surrounding = get_messages_around(
            adapter.conn,
            thread_id=anchor.thread_id,
            message_id=message_id,
            before=window,
            after=window,
        )

    # surrounding does NOT include the anchor; insert it at the right position
    # Find where to insert: after all "before" messages (those with created_at <= anchor)
    insert_idx = 0
    for i, msg in enumerate(surrounding):
        if msg.created_at <= anchor.created_at and msg.id != anchor.id:
            insert_idx = i + 1
        elif msg.created_at > anchor.created_at:
            break
        else:
            insert_idx = i + 1

    all_msgs = surrounding[:insert_idx] + [anchor] + surrounding[insert_idx:]

    # Format as conversation log
    lines = []
    for msg in all_msgs:
        dt = datetime.fromtimestamp(msg.created_at)
        ts = dt.strftime("%Y-%m-%d %H:%M")
        role = msg.role if msg.role != "model" else "assistant"
        content = (msg.content or "").strip()
        marker = " <<<" if msg.id == message_id else ""
        lines.append(f"[{role}] {ts}: {content}{marker}")

    if not lines:
        return "(no messages found)"

    return "\n\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_read_around",
        description=(
            "Read the conversation context around a specific message. "
            "Use this after memory_search_brief to expand context around selected hits. "
            "The target message is marked with <<< in the output."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message ID to center the context around",
                },
                "window": {
                    "type": "integer",
                    "description": "Number of messages before and after to include (default: 10)",
                },
            },
            "required": ["message_id"],
        },
        result_type="string",
    )
