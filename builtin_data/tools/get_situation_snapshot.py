"""Get current situation snapshot for the active persona with optional change detection."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# Fields to track for change detection
TRACKED_FIELDS = ["building_id", "building_occupants", "user_presence"]


def _format_elapsed(td) -> str:
    """Format timedelta as human-readable string."""
    total_seconds = int(td.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}秒"
    elif total_seconds < 3600:
        minutes = total_seconds // 60
        return f"{minutes}分"
    elif total_seconds < 86400:
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        if minutes:
            return f"{hours}時間{minutes}分"
        return f"{hours}時間"
    else:
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        if hours:
            return f"{days}日{hours}時間"
        return f"{days}日"


def _format_timezone_offset(dt) -> str:
    """Format timezone offset as +HH:MM."""
    offset = dt.utcoffset()
    if offset is None:
        return "+00:00"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    total_seconds = abs(total_seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def get_situation_snapshot(
    building_id: Optional[str] = None,
    detect_changes: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """Get current situation snapshot for the active persona.

    Includes:
    - Current local time
    - Timezone
    - Pulse type (user/schedule/auto)
    - Elapsed time since last AI message
    - Current building
    - Personas in the building
    - User online status

    Args:
        building_id: Building ID. Defaults to current building.
        detect_changes: If True, compare with working memory and detect changes.
                       Also updates working memory and records changes to SAIMemory.

    Returns:
        Tuple of (snapshot_text, change_info)
        - snapshot_text: Human-readable snapshot
        - change_info: {
            "has_change": bool,
            "changes": list of changed field names,
            "change_summary": human-readable summary,
            "snapshot_data": structured snapshot data
          }
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

    # Use current building if not specified
    if not building_id:
        building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        raise RuntimeError("Building ID not specified and persona has no current building")

    # Get timezone
    timezone = getattr(persona, "timezone", dt_timezone.utc)
    timezone_name = getattr(persona, "timezone_name", "UTC")

    # Current time
    now_utc = datetime.now(dt_timezone.utc)
    now_local = now_utc.astimezone(timezone)
    current_datetime_str = now_local.strftime("%Y年%m月%d日 %H:%M:%S")
    timezone_display = f"{timezone_name} ({_format_timezone_offset(now_local)})"

    # Pulse type
    pulse_type = getattr(persona, "_current_pulse_type", None)
    pulse_type_map = {
        "user": "ユーザー応答",
        "schedule": "スケジュール実行",
        "auto": "自律稼働",
    }
    pulse_type_display = pulse_type_map.get(pulse_type, "不明")

    # Last AI message time from history
    last_ai_message_label = _get_last_ai_message_elapsed(persona, building_id, now_utc)

    # Building info
    buildings = getattr(persona, "buildings", {})
    building_obj = buildings.get(building_id)
    building_name = building_obj.name if building_obj else building_id

    # Occupants (sorted for consistent comparison)
    occupants = manager.occupants.get(building_id, [])
    id_to_name_map = getattr(persona, "id_to_name_map", {})
    occupant_ids = sorted([oid for oid in occupants if oid != persona_id])
    occupant_names = [id_to_name_map.get(oid, oid) for oid in occupant_ids]
    occupants_display = ", ".join(occupant_names) if occupant_names else "(自分のみ)"

    # User online status (3-state: online, away, offline)
    presence_status = getattr(manager, "user_presence_status", "offline")
    user_state_map = {"online": "オンライン", "away": "退席中", "offline": "オフライン"}
    user_state = user_state_map.get(presence_status, "オフライン")

    # Build snapshot text
    lines = [
        f"- 現地時刻: {current_datetime_str}",
        f"- タイムゾーン: {timezone_display}",
        f"- パルス種別: {pulse_type_display}",
        f"- 最後の発言からの経過: {last_ai_message_label}",
        f"- 現在のBuilding: {building_name}",
        f"- Building内の他のペルソナ: {occupants_display}",
        f"- ユーザーオンライン状態: {user_state}",
    ]
    snapshot_text = "\n".join(lines)

    # Build structured snapshot data (for change detection)
    snapshot_data = {
        "building_id": building_id,
        "building_name": building_name,
        "building_occupants": occupant_ids,  # IDs for reliable comparison
        "building_occupant_names": occupant_names,  # Names for display
        "user_presence": presence_status,
        "pulse_type": pulse_type,
        "current_time": current_datetime_str,
        "timezone": timezone_display,
    }

    # Change detection
    change_info: Dict[str, Any] = {
        "has_change": False,
        "changes": [],
        "change_summary": "",
        "snapshot_data": snapshot_data,
    }

    if detect_changes:
        change_info = _detect_and_record_changes(persona, snapshot_data, id_to_name_map)
        change_info["snapshot_data"] = snapshot_data

    return snapshot_text, change_info


def _detect_and_record_changes(
    persona: Any,
    current_snapshot: Dict[str, Any],
    id_to_name_map: Dict[str, str],
) -> Dict[str, Any]:
    """Compare current snapshot with working memory and detect changes.
    
    Updates working memory and records changes to SAIMemory if detected.
    """
    sai_mem = getattr(persona, "sai_memory", None)
    if not sai_mem or not sai_mem.is_ready():
        return {"has_change": False, "changes": [], "change_summary": ""}

    # Load previous snapshot from working memory
    working_mem = sai_mem.load_working_memory()
    prev_snapshot = working_mem.get("situation_snapshot", {})

    # Detect changes in tracked fields
    changes: List[str] = []
    change_descriptions: List[str] = []

    for field in TRACKED_FIELDS:
        prev_value = prev_snapshot.get(field)
        curr_value = current_snapshot.get(field)

        if prev_value != curr_value:
            changes.append(field)
            desc = _describe_change(field, prev_value, curr_value, prev_snapshot, current_snapshot, id_to_name_map)
            if desc:
                change_descriptions.append(desc)

    has_change = len(changes) > 0
    change_summary = "、".join(change_descriptions) if change_descriptions else ""

    # Update working memory
    working_mem["situation_snapshot"] = current_snapshot
    sai_mem.save_working_memory(working_mem)

    # Record changes to SAIMemory if any
    if has_change and change_summary:
        _record_change_to_memory(persona, change_summary)

    return {
        "has_change": has_change,
        "changes": changes,
        "change_summary": change_summary,
    }


def _describe_change(
    field: str,
    prev_value: Any,
    curr_value: Any,
    prev_snapshot: Dict[str, Any],
    current_snapshot: Dict[str, Any],
    id_to_name_map: Dict[str, str],
) -> str:
    """Generate human-readable description of a change."""
    if field == "building_id":
        prev_name = prev_snapshot.get("building_name") or prev_value or "(なし)"
        curr_name = current_snapshot.get("building_name", curr_value)
        return f"Building移動: {prev_name} → {curr_name}"

    elif field == "building_occupants":
        prev_set = set(prev_value) if prev_value else set()
        curr_set = set(curr_value) if curr_value else set()

        arrived = curr_set - prev_set
        left = prev_set - curr_set

        parts = []
        for oid in arrived:
            name = id_to_name_map.get(oid, oid)
            parts.append(f"{name}が入室")
        for oid in left:
            name = id_to_name_map.get(oid, oid)
            parts.append(f"{name}が退室")

        return "、".join(parts) if parts else ""

    elif field == "user_presence":
        state_map = {"online": "オンライン", "away": "退席中", "offline": "オフライン"}
        prev_disp = state_map.get(prev_value, prev_value or "不明")
        curr_disp = state_map.get(curr_value, curr_value or "不明")
        return f"ユーザー: {prev_disp} → {curr_disp}"

    return ""


def _record_change_to_memory(persona: Any, change_summary: str) -> None:
    """Record situation change to SAIMemory."""
    sai_mem = getattr(persona, "sai_memory", None)
    if not sai_mem or not sai_mem.is_ready():
        return

    message = {
        "role": "user",
        "content": f"<system>【状況変化】{change_summary}</system>",
        "metadata": {
            "tags": ["internal", "situation_change"],
        },
    }
    try:
        sai_mem.append_persona_message(message)
        LOGGER.info("[situation_snapshot] Recorded change: %s", change_summary)
    except Exception as exc:
        LOGGER.warning("Failed to record situation change: %s", exc)


def _get_last_ai_message_elapsed(persona, building_id: str, now_utc: datetime) -> str:
    """Get elapsed time since last AI (assistant) message in history."""
    try:
        history_manager = getattr(persona, "history_manager", None)
        if not history_manager:
            return "不明"
        
        # Get recent history and find last assistant message
        # Use get_recent_history with a reasonable char limit
        messages = history_manager.get_recent_history(max_chars=50000)
        last_ai_time = None
        
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                # Try created_at first (from SAIMemory, UNIX timestamp)
                created_at = msg.get("created_at")
                if created_at:
                    try:
                        last_ai_time = datetime.fromtimestamp(created_at, tz=dt_timezone.utc)
                        break
                    except (TypeError, ValueError):
                        pass
                
                # Fallback to timestamp field (ISO format string, top-level)
                timestamp = msg.get("timestamp")
                if timestamp:
                    try:
                        last_ai_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                        if last_ai_time.tzinfo is None:
                            last_ai_time = last_ai_time.replace(tzinfo=dt_timezone.utc)
                        break
                    except (TypeError, ValueError):
                        pass
        
        if last_ai_time is None:
            return "履歴なし"
        
        elapsed = now_utc - last_ai_time
        return _format_elapsed(elapsed)
    except Exception as e:
        LOGGER.warning("Failed to get last AI message time: %s", e)
        return "不明"


def schema() -> ToolSchema:
    return ToolSchema(
        name="get_situation_snapshot",
        description="Get current situation snapshot including time, location, and who is present. Optionally detect and record changes.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                },
                "detect_changes": {
                    "type": "boolean",
                    "description": "If true, compare with previous snapshot, detect changes, update working memory, and record changes to SAIMemory.",
                    "default": False
                }
            },
            "required": [],
        },
        result_type="tuple",
    )
