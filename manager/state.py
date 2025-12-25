from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from buildings import Building


@dataclass
class CoreState:
    """Shared mutable state for runtime/admin services."""

    session_factory: Session
    city_id: int
    city_name: str
    model: str
    provider: str
    context_length: int
    saiverse_home: Path
    user_room_id: str

    buildings: List[Building] = field(default_factory=list)
    building_map: Dict[str, Building] = field(default_factory=dict)
    building_memory_paths: Dict[str, Path] = field(default_factory=dict)
    building_histories: Dict[str, List[Dict[str, str]]] = field(
        default_factory=dict
    )
    capacities: Dict[str, int] = field(default_factory=dict)
    items: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    item_locations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    items_by_building: Dict[str, List[str]] = field(default_factory=dict)
    items_by_persona: Dict[str, List[str]] = field(default_factory=dict)
    world_items: List[str] = field(default_factory=list)
    persona_pending_events: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    personas: Dict[str, Any] = field(default_factory=dict)
    visiting_personas: Dict[str, Any] = field(default_factory=dict)
    avatar_map: Dict[str, str] = field(default_factory=dict)
    persona_map: Dict[str, str] = field(default_factory=dict)
    occupants: Dict[str, List[str]] = field(default_factory=dict)
    id_to_name_map: Dict[str, str] = field(default_factory=dict)

    user_id: int = 1
    user_display_name: str = "ユーザー"
    user_presence_status: str = "offline"  # "online", "away", "offline"
    user_last_activity_time: Optional[datetime] = None
    user_current_building_id: Optional[str] = None
    user_current_city_id: Optional[int] = None
    user_avatar_data: str = ""

    timezone_name: str = "UTC"
    timezone_info: Optional[Any] = None

    default_avatar: str = ""
    host_avatar: str = ""
    start_in_online_mode: bool = False
    ui_port: int = 0
    api_port: int = 0
    autonomous_conversation_running: bool = False
    global_auto_enabled: bool = True  # Global ON/OFF for ConversationManager
