"""Get new messages from building history and add to persona history."""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def get_building_messages(building_id: Optional[str] = None) -> str:
    """Get new messages from building history that this persona hasn't seen yet.

    - Filters by heard_by (messages the persona could hear)
    - Filters by ingested_by (messages not yet processed)
    - Converts other personas' messages to user role with speaker prefix
    - Adds messages to persona history
    - Marks messages as ingested

    Returns a summary of perceived messages.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    # Get persona from manager
    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        raise RuntimeError("Building ID not specified and persona has no current building")

    # Get building history
    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        raise RuntimeError("Persona has no history_manager")

    hist = history_manager.building_histories.get(building_id, [])
    if not hist:
        return "新規メッセージはありません"

    # Get pulse cursors for tracking what we've seen
    pulse_cursors = getattr(persona, "pulse_cursors", {})
    entry_markers = getattr(persona, "entry_markers", {})
    last_cursor = pulse_cursors.get(building_id, 0)
    entry_limit = entry_markers.get(building_id, last_cursor)

    # Get id_to_name_map for speaker name resolution
    id_to_name_map = getattr(persona, "id_to_name_map", {})

    # Find new messages
    new_msgs: List[Dict[str, Any]] = []
    max_seen_seq = last_cursor

    for msg in hist:
        try:
            seq = int(msg.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0

        if seq <= last_cursor:
            max_seen_seq = max(max_seen_seq, seq)
            continue
        max_seen_seq = max(max_seen_seq, seq)

        if seq <= entry_limit:
            continue

        # Filter by heard_by
        heard_by = msg.get("heard_by") or []
        if persona_id not in heard_by:
            continue

        new_msgs.append(msg)

    # Update pulse cursor
    pulse_cursors[building_id] = max_seen_seq

    if not new_msgs:
        return "新規メッセージはありません"

    # Process new messages
    perceived_count = 0
    speaker_counts: Dict[str, int] = {}

    for m in new_msgs:
        try:
            role = m.get("role")
            pid = m.get("persona_id")
            content = m.get("content", "")

            # Skip if already ingested
            ingested = m.get("ingested_by") or []
            if isinstance(ingested, list) and persona_id in ingested:
                continue

            # Skip empty and system-like summary notes
            if not content or ("note-box" in content and role == "assistant"):
                continue

            # Convert other personas' messages to user role
            if role == "assistant" and pid and pid != persona_id:
                from saiverse.content_tags import strip_for_other_persona
                speaker = id_to_name_map.get(pid, pid)
                stripped = strip_for_other_persona(content)
                if stripped is None:
                    _mark_ingested(m, persona_id)
                    perceived_count += 1
                    speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1
                    continue
                entry = {
                    "role": "user",
                    "content": f"{speaker}: {stripped}"
                }
                # Copy metadata if present, add "with" field
                metadata = m.get("metadata")
                if isinstance(metadata, dict):
                    entry["metadata"] = copy.deepcopy(metadata)
                else:
                    entry["metadata"] = {}
                # Add conversation partner to "with" field
                entry["metadata"]["with"] = [pid]
                # Copy timestamp
                ts_value = m.get("timestamp")
                if isinstance(ts_value, str):
                    entry["timestamp"] = ts_value

                history_manager.add_to_persona_only(entry)
                _mark_ingested(m, persona_id)
                perceived_count += 1
                speaker_counts[speaker] = speaker_counts.get(speaker, 0) + 1

            # Ingest user messages directly
            elif role == "user" and (pid is None or pid != persona_id):
                entry = {
                    "role": "user",
                    "content": content
                }
                # Copy metadata if present, add "with" field
                metadata = m.get("metadata")
                if isinstance(metadata, dict):
                    entry["metadata"] = copy.deepcopy(metadata)
                else:
                    entry["metadata"] = {}
                # Mark as conversation with user
                entry["metadata"]["with"] = ["user"]
                ts_value = m.get("timestamp")
                if isinstance(ts_value, str):
                    entry["timestamp"] = ts_value

                history_manager.add_to_persona_only(entry)
                _mark_ingested(m, persona_id)
                perceived_count += 1
                speaker_counts["ユーザー"] = speaker_counts.get("ユーザー", 0) + 1

        except Exception as exc:
            LOGGER.debug("Failed to process message: %s", exc)
            continue

    if perceived_count == 0:
        return "新規メッセージはありません"

    # Build summary
    details = ", ".join(f"{name}: {count}件" for name, count in speaker_counts.items())
    return f"{perceived_count}件の新規メッセージを認識しました（{details}）"


def _mark_ingested(msg: Dict[str, Any], persona_id: str) -> None:
    """Mark a message as ingested by this persona."""
    try:
        bucket = msg.setdefault("ingested_by", [])
        if isinstance(bucket, list) and persona_id not in bucket:
            bucket.append(persona_id)
    except Exception:
        LOGGER.warning("Failed to mark message as ingested by %s", persona_id, exc_info=True)


def auto_ingest_building_messages(persona: Any, manager: Any) -> int:
    """Automatically ingest new building messages at pulse start.

    Mirrors get_building_messages() but takes persona/manager directly
    instead of using tool context vars. Called automatically before each pulse.

    Returns the number of messages ingested.
    """
    persona_id = getattr(persona, "persona_id", None)
    building_id = getattr(persona, "current_building_id", None)
    if not persona_id or not building_id:
        return 0

    history_manager = getattr(persona, "history_manager", None)
    if not history_manager:
        return 0

    hist = history_manager.building_histories.get(building_id, [])
    if not hist:
        return 0

    pulse_cursors = getattr(persona, "pulse_cursors", None)
    if pulse_cursors is None:
        persona.pulse_cursors = {}
        pulse_cursors = persona.pulse_cursors

    entry_markers = getattr(persona, "entry_markers", {})
    last_cursor = pulse_cursors.get(building_id, 0)
    entry_limit = entry_markers.get(building_id, last_cursor)

    id_to_name_map = getattr(manager, "id_to_name_map", {})

    LOGGER.info(
        "[auto_ingest] %s building=%s hist_len=%d last_cursor=%d entry_limit=%d",
        persona_id, building_id, len(hist), last_cursor, entry_limit,
    )

    new_msgs: List[Dict[str, Any]] = []
    max_seen_seq = last_cursor

    for msg in hist:
        try:
            seq = int(msg.get("seq", 0))
        except (TypeError, ValueError):
            seq = 0

        max_seen_seq = max(max_seen_seq, seq)

        if seq <= last_cursor or seq <= entry_limit:
            continue

        heard_by = msg.get("heard_by") or []
        if persona_id not in heard_by:
            LOGGER.debug(
                "[auto_ingest] %s skipping seq=%d (not in heard_by=%s)", persona_id, seq, heard_by
            )
            continue

        new_msgs.append(msg)

    pulse_cursors[building_id] = max_seen_seq

    if not new_msgs:
        return 0

    ingested_count = 0
    dirty = False

    for m in new_msgs:
        try:
            role = m.get("role")
            pid = m.get("persona_id")
            content = m.get("content", "")

            ingested = m.get("ingested_by") or []
            if isinstance(ingested, list) and persona_id in ingested:
                continue

            if not content or ("note-box" in content and role == "assistant"):
                continue

            if role == "assistant" and pid and pid != persona_id:
                from saiverse.content_tags import strip_for_other_persona
                speaker = id_to_name_map.get(pid, pid)
                stripped = strip_for_other_persona(content)
                if stripped is None:
                    _mark_ingested(m, persona_id)
                    dirty = True
                    continue
                entry: Dict[str, Any] = {
                    "role": "user",
                    "content": f"{speaker}: {stripped}",
                }
                metadata = m.get("metadata")
                entry["metadata"] = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
                entry["metadata"]["with"] = [pid]
                ts_value = m.get("timestamp")
                if isinstance(ts_value, str):
                    entry["timestamp"] = ts_value

                history_manager.add_to_persona_only(entry)
                _mark_ingested(m, persona_id)
                ingested_count += 1
                dirty = True

            elif role == "user" and (pid is None or pid != persona_id):
                entry = {"role": "user", "content": content}
                metadata = m.get("metadata")
                entry["metadata"] = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
                entry["metadata"]["with"] = ["user"]
                ts_value = m.get("timestamp")
                if isinstance(ts_value, str):
                    entry["timestamp"] = ts_value

                history_manager.add_to_persona_only(entry)
                _mark_ingested(m, persona_id)
                ingested_count += 1
                dirty = True

        except Exception:
            LOGGER.debug("auto_ingest: failed to process message", exc_info=True)
            continue

    if dirty:
        try:
            save_fn = getattr(manager, "_save_building_histories", None)
            if save_fn:
                save_fn([building_id])
        except Exception:
            LOGGER.warning("auto_ingest: failed to save building histories", exc_info=True)

    if ingested_count:
        LOGGER.info(
            "[auto_ingest] %s ingested %d new message(s) from building %s",
            persona_id, ingested_count, building_id,
        )

    return ingested_count


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_building_messages",
        description="Get new messages from building history that this persona hasn't seen yet. Adds them to persona history.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID to get messages from. Defaults to current building."
                }
            },
            "required": [],
        },
        result_type="string",
    )
