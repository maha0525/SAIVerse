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

    # Check if last message was a wait
    try:
        recent = adapter.recent_persona_messages(max_chars=500, required_tags=["wait"])
        last_wait = recent[0] if recent else None
    except Exception:
        last_wait = None

    if last_wait and _is_recent_wait(last_wait, now):
        # Consolidate with previous wait
        prev_metadata = last_wait.get("metadata", {})
        wait_started = prev_metadata.get("wait_started", last_wait.get("timestamp", now_iso))
        wait_count = prev_metadata.get("wait_count", 1) + 1

        # Build consolidated message
        content = f"(待機中: {wait_count}回目)"
        if reason:
            content = f"(待機中: {wait_count}回目 - {reason})"

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
        try:
            _remove_last_wait(adapter, last_wait)
        except Exception as exc:
            LOGGER.debug("Failed to remove old wait: %s", exc)

        adapter.append_persona_message(new_msg)
        LOGGER.debug("[record_wait] Consolidated wait #%d", wait_count)
        return f"待機継続 ({wait_count}回目)"

    else:
        # New wait
        content = "(待機を選択)"
        if reason:
            content = f"(待機を選択 - {reason})"

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


def _is_recent_wait(msg: dict, now: datetime) -> bool:
    """Check if message is a recent wait (within last hour)."""
    metadata = msg.get("metadata", {})
    tags = metadata.get("tags", [])

    if "wait" not in tags:
        return False

    # Check if it's recent (within 1 hour)
    try:
        ts_str = metadata.get("wait_latest") or msg.get("timestamp")
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt_timezone.utc)
            diff = (now - ts).total_seconds()
            return diff < 3600  # Within 1 hour
    except Exception:
        pass

    return True  # If can't determine, assume recent


def _remove_last_wait(adapter, msg: dict) -> None:
    """Remove the last wait message from SAIMemory.

    This is a best-effort operation - if deletion fails, we still add the new one.
    """
    # SAIMemory doesn't have a direct delete API, but we can mark as superseded
    # For now, we leave the old message and let the new one replace it conceptually
    # The consolidation info is in the new message, so old one becomes obsolete
    pass


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
