from __future__ import annotations

from typing import Dict, List, Optional

from saiverse_manager import SAIVerseManager

# Centralised UI state so that handlers can share data without relying on main.py globals.
manager: Optional[SAIVerseManager] = None
building_choices: List[str] = []
building_name_to_id: Dict[str, str] = {}
autonomous_building_choices: List[str] = []
autonomous_building_map: Dict[str, str] = {}
model_choices: List[str] = []
chat_history_limit: int = 120
version: str = ""


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
