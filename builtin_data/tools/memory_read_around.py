"""Read messages around a specific message ID for context expansion."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sai_memory.memory.storage import (
    get_message,
    get_window_with_anchor,
)
from tools.context import get_active_persona_id, open_persona_memory
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

    with open_persona_memory() as adapter:
        if not adapter.is_ready():
            raise RuntimeError(f"SAIMemory not ready for {persona_id}")

        # Get the anchor message
        with adapter._db_lock:
            anchor = get_message(adapter.conn, message_id)

        if not anchor:
            return f"(message not found: {message_id})"

        # 前と後を別々に取って anchor を挟む (正典順 created_at, rowid のまま —
        # created_at の比較で挿し込むと、同じ秒の後ろの行が anchor の前に並ぶ)
        with adapter._db_lock:
            all_msgs = get_window_with_anchor(adapter.conn, anchor, window, window)

    # Format as conversation log
    lines = ["```"]
    for msg in all_msgs:
        dt = datetime.fromtimestamp(msg.created_at)
        ts = dt.strftime("%Y-%m-%d %H:%M")
        role = msg.role if msg.role != "model" else "assistant"
        content = (msg.content or "").strip()
        marker = " <<<" if msg.id == message_id else ""
        lines.append(f"[{role}] {ts}: {content}{marker}")

    if len(lines) == 1:
        return "(no messages found)"

    lines.append("```")
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
