"""Record wait action with consolidation of consecutive waits."""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


def record_wait(reason: Optional[str] = None) -> str:
    """Record a wait action.

    If the previous message was also a wait, consolidate them into a single
    message with wait_started (first wait) and wait_latest (current wait).

    Args:
        reason: Optional reason for waiting

    Returns:
        Confirmation message
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
        return "(待機を選択 - メモリ未接続)"

    now = datetime.now(dt_timezone.utc)
    now_iso = now.isoformat()

    # Check if the LAST message (of any type) was a wait
    try:
        # Get recent messages of all types (conversation + internal)
        recent_all = adapter.recent_persona_messages(
            max_chars=1000,
            required_tags=["conversation", "internal"]
        )
        last_msg = recent_all[-1] if recent_all else None

        # Check if last message has wait tag
        last_wait = None
        if last_msg:
            tags = last_msg.get("metadata", {}).get("tags", [])
            if "wait" in tags:
                last_wait = last_msg
    except Exception:
        last_wait = None

    if last_wait:
        # Consolidate with previous wait
        prev_metadata = last_wait.get("metadata", {})
        wait_started = prev_metadata.get("wait_started", last_wait.get("timestamp", now_iso))
        wait_count = prev_metadata.get("wait_count", 1) + 1

        # Format timestamps for content (human-readable)
        started_str = _format_timestamp(wait_started)
        latest_str = _format_timestamp(now_iso)

        # Build consolidated message with timestamps in content
        content = f"(待機中: 開始 {started_str}, 最新 {latest_str}, {wait_count}回目)"
        if reason:
            content = f"(待機中: 開始 {started_str}, 最新 {latest_str}, {wait_count}回目 - {reason})"

        new_msg = {
            "role": "assistant",
            "content": content,
            "metadata": {
                "tags": ["internal", "wait"],
                "wait_started": wait_started,
                "wait_latest": now_iso,
                "wait_count": wait_count,
            },
        }

        # Remove old wait and add new one
        deleted = _remove_last_wait(adapter, last_wait)
        if not deleted:
            LOGGER.debug("Failed to remove old wait message")

        adapter.append_persona_message(new_msg)
        LOGGER.debug("[record_wait] Consolidated wait #%d", wait_count)
        return f"待機継続 ({wait_count}回目)"

    else:
        # New wait - include start timestamp
        started_str = _format_timestamp(now_iso)
        content = f"(待機開始: {started_str})"
        if reason:
            content = f"(待機開始: {started_str} - {reason})"

        new_msg = {
            "role": "assistant",
            "content": content,
            "metadata": {
                "tags": ["internal", "wait"],
                "wait_started": now_iso,
                "wait_latest": now_iso,
                "wait_count": 1,
            },
        }

        adapter.append_persona_message(new_msg)
        LOGGER.debug("[record_wait] New wait recorded")
        return "待機を選択"


def _format_timestamp(iso_str: str) -> str:
    """Format ISO timestamp to human-readable HH:MM:SS format."""
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # Convert to local time for display
        local_ts = ts.astimezone()
        return local_ts.strftime("%H:%M:%S")
    except Exception:
        return iso_str[:19]  # Fallback to raw timestamp


def _remove_last_wait(adapter, msg: dict) -> bool:
    """Remove the last wait message from SAIMemory.

    Args:
        adapter: SAIMemoryAdapter instance
        msg: Message dict containing 'id' field

    Returns:
        True if deletion succeeded, False otherwise
    """
    message_id = msg.get("id")
    if not message_id:
        LOGGER.debug("No message ID found in wait message, cannot delete")
        return False

    try:
        result = adapter.delete_message(message_id)
        if result:
            LOGGER.debug("[record_wait] Deleted old wait message: %s", message_id)
        return result
    except Exception as exc:
        LOGGER.warning("Failed to delete wait message %s: %s", message_id, exc)
        return False


def schema() -> ToolSchema:
    return ToolSchema(
        name="record_wait",
        description="Record a wait action. Consolidates consecutive waits into a single message.",
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Optional reason for waiting"
                }
            },
            "required": [],
        },
        result_type="string",
    )
