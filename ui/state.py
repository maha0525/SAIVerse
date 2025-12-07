from __future__ import annotations

import threading
from typing import Dict, List, Optional, Set, Tuple, Union

from saiverse_manager import SAIVerseManager

# Centralised UI state so that handlers can share data without relying on main.py globals.
manager: Optional[SAIVerseManager] = None
building_choices: List[str] = []
building_name_to_id: Dict[str, str] = {}
autonomous_building_choices: List[str] = []
autonomous_building_map: Dict[str, str] = {}
model_choices: List[Union[str, Tuple[str, str]]] = []  # Can be ["model"] or [("display_name", "model_id")] - Gradio format
chat_history_limit: int = 120
version: str = ""

# --- Unread message tracking ---
# building_id -> message count when last viewed
_last_seen_counts: Dict[str, int] = {}
# building_id set that has unread messages
_unread_buildings: Set[str] = set()
# Lock for thread-safe access
_unread_lock = threading.Lock()


def bind_manager(instance: SAIVerseManager) -> None:
    """Register the manager instance and seed building caches."""
    global manager
    manager = instance
    refresh_building_caches()


def set_model_choices(choices: List[str]) -> None:
    global model_choices
    model_choices = choices


def set_chat_history_limit(limit: int) -> None:
    global chat_history_limit
    chat_history_limit = max(0, limit)


def set_version(value: str) -> None:
    global version
    version = value


def refresh_building_caches() -> None:
    """Recompute building-related lookup tables from the current manager."""
    if not manager:
        return
    global building_choices, building_name_to_id, autonomous_building_choices, autonomous_building_map
    building_choices = [b.name for b in manager.buildings]
    building_name_to_id = {b.name: b.building_id for b in manager.buildings}
    autonomous_building_choices = [
        b.name for b in manager.buildings if b.building_id != manager.user_room_id
    ]
    autonomous_building_map = {
        b.name: b.building_id for b in manager.buildings if b.building_id != manager.user_room_id
    }


# --- Unread message management functions ---

def get_current_message_count(building_id: str) -> int:
    """Get current message count for a building (only visible messages)."""
    if not manager:
        return 0
    history = manager.building_histories.get(building_id, [])
    # Only count messages that are displayed in UI (user and assistant roles)
    # host role messages (system notifications like "X moved to Y") are not shown to user
    visible_count = sum(1 for msg in history if msg.get("role") in ("user", "assistant"))
    return visible_count


def mark_building_as_read(building_id: str) -> None:
    """Mark a building as read (user entered or is viewing it)."""
    import logging
    LOGGER = logging.getLogger(__name__)
    with _unread_lock:
        current_count = get_current_message_count(building_id)
        was_unread = building_id in _unread_buildings
        _last_seen_counts[building_id] = current_count
        _unread_buildings.discard(building_id)
        LOGGER.info(
            "[unread] mark_building_as_read: %s, count=%d, was_unread=%s, unread_now=%s",
            building_id, current_count, was_unread, _unread_buildings
        )


def check_for_new_messages() -> Set[str]:
    """
    Check all buildings for new messages.
    Returns set of building_ids that have new unread messages since last check.
    """
    import logging
    LOGGER = logging.getLogger(__name__)

    if not manager:
        return set()

    newly_unread: Set[str] = set()
    # Get current building (user is viewing this one, so don't mark as unread)
    current_building_id = manager.user_current_building_id

    with _unread_lock:
        for building in manager.buildings:
            bid = building.building_id
            current_count = get_current_message_count(bid)
            last_seen = _last_seen_counts.get(bid, 0)

            if current_count > last_seen:
                LOGGER.warning(
                    "[unread-debug] Building %s (%s): current=%d > last_seen=%d, current_building=%s",
                    bid, building.name, current_count, last_seen, current_building_id
                )
                # Skip the building user is currently in
                if bid == current_building_id:
                    # Update last_seen so it won't be marked unread when user leaves
                    _last_seen_counts[bid] = current_count
                    continue

                if bid not in _unread_buildings:
                    newly_unread.add(bid)
                _unread_buildings.add(bid)

    return newly_unread


def get_unread_buildings() -> Set[str]:
    """Get the set of building_ids with unread messages."""
    with _unread_lock:
        return _unread_buildings.copy()


def initialize_message_counts() -> None:
    """Initialize last seen counts for all buildings (call on startup)."""
    if not manager:
        return
    with _unread_lock:
        for building in manager.buildings:
            bid = building.building_id
            _last_seen_counts[bid] = get_current_message_count(bid)
        _unread_buildings.clear()
