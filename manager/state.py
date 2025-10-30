from dataclasses import dataclass, field
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

    personas: Dict[str, Any] = field(default_factory=dict)
    visiting_personas: Dict[str, Any] = field(default_factory=dict)
    avatar_map: Dict[str, str] = field(default_factory=dict)
    persona_map: Dict[str, str] = field(default_factory=dict)
    occupants: Dict[str, List[str]] = field(default_factory=dict)
    id_to_name_map: Dict[str, str] = field(default_factory=dict)

    user_id: int = 1
    user_display_name: str = "ユーザー"
    user_is_online: bool = False
    user_current_building_id: Optional[str] = None
    user_current_city_id: Optional[int] = None

    timezone_name: str = "UTC"
    timezone_info: Optional[Any] = None

    default_avatar: str = ""
    host_avatar: str = ""
    start_in_online_mode: bool = False
    ui_port: int = 0
    api_port: int = 0
