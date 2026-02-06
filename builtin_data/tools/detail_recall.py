"""Detailed recall of messages by summary UUID or time range."""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Default expansion in minutes
DEFAULT_EXPAND_MINUTES = 30


def detail_recall(
    summary_uuid: Optional[str] = None,
    expand_before_minutes: int = 0,
    expand_after_minutes: int = 0,
    max_messages: int = 50,
) -> str:
    """Recall detailed messages from a specific time range.

    Can be used with:
    1. A summary UUID from get_since_last_user_conversation to get the original messages
    2. expand_before/after to extend the time range

    Args:
        summary_uuid: UUID from a previous summary (first 8 chars or full UUID)
        expand_before_minutes: Extend time range N minutes before
        expand_after_minutes: Extend time range N minutes after
        max_messages: Maximum messages to return

    Returns:
        Detailed message log with timestamps
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    adapter = getattr(persona, "sai_memory", None)
    if not adapter or not adapter.is_ready():
        return "メモリが利用できません"

    # Find the summary message by UUID
    if summary_uuid:
        summary_msg = _find_summary_by_uuid(adapter, summary_uuid)
        if not summary_msg:
            return f"要約ID '{summary_uuid}' が見つかりませんでした"

        # Get time range from summary metadata
        metadata = summary_msg.get("metadata", {})
        time_start = metadata.get("source_time_start")
        time_end = metadata.get("source_time_end")

        if not time_start or not time_end:
            return "要約に時間範囲情報がありません"

        # Parse and expand time range
        try:
            start_dt = datetime.fromisoformat(time_start.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(time_end.replace("Z", "+00:00"))

            if expand_before_minutes > 0:
                start_dt -= timedelta(minutes=expand_before_minutes)
            if expand_after_minutes > 0:
                end_dt += timedelta(minutes=expand_after_minutes)

        except Exception as exc:
            LOGGER.warning("Failed to parse time range: %s", exc)
            return "時間範囲の解析に失敗しました"

    else:
        # No UUID - get recent messages
        end_dt = datetime.now(dt_timezone.utc)
        start_dt = end_dt - timedelta(minutes=expand_before_minutes or DEFAULT_EXPAND_MINUTES)

    # Fetch messages in time range
    messages = _get_messages_in_range(adapter, start_dt, end_dt, max_messages)

    if not messages:
        return "指定された範囲にメッセージがありませんでした"

    # Format output
    output_parts = [
        f"【詳細ログ】{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}",
        f"({len(messages)}件のメッセージ)",
        "",
    ]

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        metadata = msg.get("metadata", {})
        tags = metadata.get("tags", [])
        with_list = metadata.get("with", [])

        # Format timestamp
        ts_display = ""
        if timestamp:
            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                ts_display = ts.strftime("%H:%M:%S")
            except Exception:
                ts_display = timestamp[:19]

        # Build context info
        context_info = []
        if with_list:
            context_info.append(f"with:{','.join(with_list)}")
        if "wait" in tags:
            context_info.append("待機")
        if "internal" in tags and "wait" not in tags:
            context_info.append("内部")

        context_str = f" ({', '.join(context_info)})" if context_info else ""

        output_parts.append(f"[{ts_display}] [{role}]{context_str}")
        output_parts.append(f"  {content}")
        output_parts.append("")

    # Add navigation hints
    output_parts.append("---")
    output_parts.append("より前の記録を見るには expand_before_minutes を指定してください")
    output_parts.append("より後の記録を見るには expand_after_minutes を指定してください")

    return "\n".join(output_parts)


def _find_summary_by_uuid(adapter: Any, uuid_prefix: str) -> Optional[Dict]:
    """Find a summary message by UUID prefix."""
    try:
        messages = adapter.recent_persona_messages(
            max_chars=100000,
            required_tags=["summary"],
        )

        for msg in reversed(messages):  # Most recent first
            metadata = msg.get("metadata", {})
            msg_uuid = metadata.get("summary_uuid", "")
            # Match full UUID or prefix
            if msg_uuid.startswith(uuid_prefix) or uuid_prefix.startswith(msg_uuid[:8]):
                return msg

        return None

    except Exception as exc:
        LOGGER.warning("Failed to find summary: %s", exc)
        return None


def _get_messages_in_range(
    adapter: Any,
    start_dt: datetime,
    end_dt: datetime,
    max_messages: int,
) -> List[Dict]:
    """Get messages within a time range."""
    try:
        # Get all recent messages (we'll filter by time)
        all_messages = adapter.recent_persona_messages(
            max_chars=500000,  # Large limit to get all
            required_tags=None,
        )

        # Filter by time range
        filtered = []
        for msg in all_messages:
            timestamp = msg.get("timestamp")
            if not timestamp:
                continue

            try:
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt_timezone.utc)

                if start_dt <= ts <= end_dt:
                    filtered.append(msg)

            except Exception:
                continue

        # Limit count
        return filtered[:max_messages]

    except Exception as exc:
        LOGGER.warning("Failed to get messages in range: %s", exc)
        return []


def schema() -> ToolSchema:
    return ToolSchema(
        name="detail_recall",
        description="Recall detailed messages from a specific time range. Use with a summary UUID to get original messages, or expand the range to see more context.",
        parameters={
            "type": "object",
            "properties": {
                "summary_uuid": {
                    "type": "string",
                    "description": "UUID from a previous summary (first 8 chars or full). If not provided, gets recent messages."
                },
                "expand_before_minutes": {
                    "type": "integer",
                    "description": "Extend time range N minutes before. Default: 0.",
                    "default": 0
                },
                "expand_after_minutes": {
                    "type": "integer",
                    "description": "Extend time range N minutes after. Default: 0.",
                    "default": 0
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Maximum messages to return. Default: 50.",
                    "default": 50
                }
            },
            "required": [],
        },
        result_type="string",
    )
