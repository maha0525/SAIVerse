import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from buildings import Building
from buildings.user_room import load as load_user_room
from buildings.deep_think_room import load as load_deep_think_room
from buildings.air_room import load as load_air_room
from buildings.eris_room import load as load_eris_room
from buildings.const_test_room import load as load_const_test_room


class CityManager:
    """Manage buildings and their histories/occupants."""

    def __init__(self, building_loaders: Optional[List[Callable[[], Building]]] = None):
        if building_loaders is None:
            building_loaders = [
                load_user_room,
                load_deep_think_room,
                load_air_room,
                load_eris_room,
                load_const_test_room,
            ]
        self.buildings: List[Building] = [loader() for loader in building_loaders]
        self.building_map: Dict[str, Building] = {b.building_id: b for b in self.buildings}
        self.capacities: Dict[str, int] = {b.building_id: b.capacity for b in self.buildings}

        self.saiverse_home = Path.home() / ".saiverse"
        self.building_memory_paths: Dict[str, Path] = {
            b.building_id: self.saiverse_home / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }
        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        for b_id, path in self.building_memory_paths.items():
            if path.exists():
                try:
                    self.building_histories[b_id] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logging.warning("Failed to load building history %s", b_id)
                    self.building_histories[b_id] = []
            else:
                fallback = Path("buildings") / b_id / "memory.json"
                if fallback.exists():
                    try:
                        self.building_histories[b_id] = json.loads(fallback.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        self.building_histories[b_id] = []
                else:
                    self.building_histories[b_id] = []

        self.occupants: Dict[str, List[str]] = {b.building_id: [] for b in self.buildings}

    def add_persona(self, persona_id: str, building_id: str) -> None:
        self.occupants.setdefault(building_id, []).append(persona_id)

    def move_persona(self, persona_id: str, from_id: str, to_id: str) -> Tuple[bool, Optional[str]]:
        if len(self.occupants.get(to_id, [])) >= self.capacities.get(to_id, 1):
            return False, f"{self.building_map[to_id].name}は定員オーバーです"
        if persona_id in self.occupants.get(from_id, []):
            self.occupants[from_id].remove(persona_id)
        self.occupants.setdefault(to_id, []).append(persona_id)
        return True, None

    def save_histories(self) -> None:
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

