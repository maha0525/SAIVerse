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
    - Elapsed time since last pulse
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

    # Elapsed since last pulse
    last_pulse_time = getattr(persona, "_last_conscious_prompt_time_utc", None)
    if last_pulse_time is not None:
        elapsed = now_utc - last_pulse_time
        elapsed_label = _format_elapsed(elapsed)
    else:
        elapsed_label = "初回実行"

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

    # User online status
    user_online = getattr(manager, "user_online", False)
    user_state = "オンライン" if user_online else "オフライン"

    # Build snapshot
    lines = [
        f"- 現地時刻: {current_datetime_str}",
        f"- タイムゾーン: {timezone_display}",
        f"- 前回のパルスからの経過: {elapsed_label}",
        f"- 現在のBuilding: {building_name}",
        f"- Building内の他のペルソナ: {occupants_display}",
        f"- ユーザーオンライン状態: {user_state}",
    ]

    return "\n".join(lines)


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
