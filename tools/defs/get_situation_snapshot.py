"""Get current situation snapshot for the active persona."""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone
from typing import List, Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.defs import ToolSchema

LOGGER = logging.getLogger(__name__)


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


def get_situation_snapshot(building_id: Optional[str] = None) -> str:
    """Get current situation snapshot for the active persona.

    Includes:
    - Current local time
    - Timezone
    - Pulse type (user/schedule/auto)
    - Elapsed time since last AI message
    - Current building
    - Personas in the building
    - User online status

    Returns formatted snapshot text.
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

    # Occupants
    occupants = manager.occupants.get(building_id, [])
    id_to_name_map = getattr(persona, "id_to_name_map", {})
    occupant_names = []
    for oid in occupants:
        if oid == persona_id:
            continue  # Skip self
        name = id_to_name_map.get(oid, oid)
        occupant_names.append(name)
    occupants_display = ", ".join(occupant_names) if occupant_names else "(自分のみ)"

    # User online status (3-state: online, away, offline)
    presence_status = getattr(manager, "user_presence_status", "offline")
    user_state_map = {"online": "オンライン", "away": "退席中", "offline": "オフライン"}
    user_state = user_state_map.get(presence_status, "オフライン")

    # Build snapshot
    lines = [
        f"- 現地時刻: {current_datetime_str}",
        f"- タイムゾーン: {timezone_display}",
        f"- パルス種別: {pulse_type_display}",
        f"- 最後の発言からの経過: {last_ai_message_label}",
        f"- 現在のBuilding: {building_name}",
        f"- Building内の他のペルソナ: {occupants_display}",
        f"- ユーザーオンライン状態: {user_state}",
    ]

    return "\n".join(lines)


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
        description="Get current situation snapshot including time, location, and who is present.",
        parameters={
            "type": "object",
            "properties": {
                "building_id": {
                    "type": "string",
                    "description": "Building ID. Defaults to current building."
                }
            },
            "required": [],
        },
        result_type="string",
    )
