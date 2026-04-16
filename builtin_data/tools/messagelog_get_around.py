"""Get chat messages around a specific timestamp."""

from __future__ import annotations

import datetime
from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.core import ToolSchema


def _parse_timestamp(value: str) -> int:
    """Parse a timestamp from Unix epoch or ISO 8601 string."""
    value = value.strip()
    if not value:
        raise ValueError("Empty timestamp")
    # Try parsing as integer string first
    try:
        return int(value)
    except ValueError:
        pass
    # Parse ISO 8601 (e.g. "2026-04-14T12:34:56", "2026-04-14 12:34:56")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {value!r}")


def messagelog_get_around(
    timestamp: str = "",
    count: int = 10,
    thread_id: Optional[str] = None,
) -> str:
    """Retrieve chat messages around a given timestamp.

    *timestamp* accepts either a Unix epoch (int) or an ISO 8601 string
    (e.g. ``"2026-04-14T12:34:56"``).  Returns *count* messages closest
    to the specified time in chronological order.
    """
    try:
        ts = _parse_timestamp(timestamp)
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}"
    if ts <= 0:
        return "Error: timestamp must be a positive value"

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

    from sai_memory.memory.storage import get_messages_around_timestamp

    messages = get_messages_around_timestamp(
        adapter.conn,
        timestamp=ts,
        count=count,
        thread_id=thread_id,
    )

    if not messages:
        dt_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        return f"No messages found near {dt_str} (timestamp={ts})"

    lines: list[str] = []
    for msg in messages:
        dt_str = datetime.datetime.fromtimestamp(msg.created_at).strftime("%Y-%m-%d %H:%M:%S")
        role = msg.role or "unknown"
        lines.append(f"[{dt_str}] ({role})\n{msg.content}")

    return "\n---\n".join(lines)


def schema() -> ToolSchema:
    return ToolSchema(
        name="messagelog_get_around",
        description=(
            "Retrieve chat messages around a specific timestamp. "
            "Accepts Unix epoch (integer) or ISO 8601 string (e.g. '2026-04-14T12:34:56'). "
            "Useful for reviewing past conversations at a known point in time."
        ),
        parameters={
            "type": "object",
            "properties": {
                "timestamp": {
                    "type": "string",
                    "description": "Timestamp to search around. Unix epoch (e.g. '1744612496') or ISO 8601 string (e.g. '2026-04-14T12:34:56')",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of messages to retrieve (default: 10)",
                    "default": 10,
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional thread ID to limit search scope",
                },
            },
            "required": ["timestamp"],
        },
        result_type="string",
        spell=True,
        spell_display_name="特定時刻のログ取得",
    )
