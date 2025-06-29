import base64
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterator

from buildings import Building
from buildings.user_room import load as load_user_room
from buildings.deep_think_room import load as load_deep_think_room
from buildings.air_room import load as load_air_room
from buildings.eris_room import load as load_eris_room
from buildings.const_test_room import load as load_const_test_room
from router import Router


DEFAULT_MODEL = "gpt-4o"


class SAIVerseManager:
    """Manage multiple personas and building occupancy."""

    def __init__(
        self,
        persona_list_path: Path = Path("ai_sessions/personas.json"),
        model: str = DEFAULT_MODEL,
    ):
        self.buildings: List[Building] = [
            load_user_room(),
            load_deep_think_room(),
            load_air_room(),
            load_eris_room(),
            load_const_test_room(),
        ]
        self.building_map: Dict[str, Building] = {b.building_id: b for b in self.buildings}
        self.capacities: Dict[str, int] = {b.building_id: b.capacity for b in self.buildings}
        self.saiverse_home = Path.home() / ".saiverse"
        self.building_memory_paths: Dict[str, Path] = {
            b.building_id: self.saiverse_home / "buildings" / b.building_id / "log.json"
            for b in self.buildings
        }
        self.model = model
        self.building_histories: Dict[str, List[Dict[str, str]]] = {}
        default_avatar_path = Path("assets/icons/blank.png")
        if default_avatar_path.exists():
            mime = "image/png"
            data_b = default_avatar_path.read_bytes()
            b64 = base64.b64encode(data_b).decode("ascii")
            self.default_avatar = f"data:{mime};base64,{b64}"
        else:
            self.default_avatar = ""
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
        data = json.loads(Path(persona_list_path).read_text(encoding="utf-8"))
        self.routers: Dict[str, Router] = {}
        self.occupants: Dict[str, List[str]] = {b.building_id: [] for b in self.buildings}
        self.avatar_map: Dict[str, str] = {}
        for p in data:
            pid = p["persona_id"]
            base = Path("ai_sessions") / pid
            start_id = "air_room"
            try:
                base_data = json.loads((base / "base.json").read_text(encoding="utf-8"))
                start_id = base_data.get("start_building_id", start_id)
                avatar = base_data.get("avatar_image")
                if avatar:
                    try:
                        avatar_path = Path(avatar)
                        mime = "image/png"
                        if avatar_path.suffix.lower() in {".jpg", ".jpeg"}:
                            mime = "image/jpeg"
                        elif avatar_path.suffix.lower() == ".gif":
                            mime = "image/gif"
                        data_b = avatar_path.read_bytes()
                        b64 = base64.b64encode(data_b).decode("ascii")
                        self.avatar_map[pid] = f"data:{mime};base64,{b64}"
                    except Exception:
                        self.avatar_map[pid] = avatar
            except Exception:
                pass
            router = Router(
                buildings=self.buildings,
                common_prompt_path=Path("system_prompts/common.txt"),
                persona_base=base,
                action_priority_path=Path("action_priority.json"),
                building_histories=self.building_histories,
                move_callback=self._move_persona,
                start_building_id=start_id,
                model=self.model,
            )
            self.routers[pid] = router
            self.occupants[router.current_building_id].append(pid)
        self.persona_map = {p["persona_name"]: p["persona_id"] for p in data}

    def _move_persona(self, persona_id: str, from_id: str, to_id: str) -> Tuple[bool, Optional[str]]:
        if len(self.occupants.get(to_id, [])) >= self.capacities.get(to_id, 1):
            return False, f"{self.building_map[to_id].name}は定員オーバーです"
        if persona_id in self.occupants.get(from_id, []):
            self.occupants[from_id].remove(persona_id)
        self.occupants.setdefault(to_id, []).append(persona_id)
        return True, None

    def _save_building_histories(self) -> None:
        for b_id, path in self.building_memory_paths.items():
            hist = self.building_histories.get(b_id, [])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")

    def handle_user_input(self, message: str) -> List[str]:
        replies: List[str] = []
        for pid in list(self.occupants.get("user_room", [])):
            replies.extend(self.routers[pid].handle_user_input(message))
        self._save_building_histories()
        for router in self.routers.values():
            router._save_session_metadata()
        return replies

    def handle_user_input_stream(self, message: str) -> Iterator[str]:
        for pid in list(self.occupants.get("user_room", [])):
            for token in self.routers[pid].handle_user_input_stream(message):
                yield token
        self._save_building_histories()
        for router in self.routers.values():
            router._save_session_metadata()

    def summon_persona(self, persona_id: str) -> List[str]:
        if persona_id not in self.routers:
            return []
        if (
            len(self.occupants.get("user_room", [])) >= self.capacities.get("user_room", 1)
            and persona_id not in self.occupants.get("user_room", [])
        ):
            msg = f"移動できませんでした。{self.building_map['user_room'].name}は定員オーバーです"
            self.building_histories["user_room"].append(
                {"role": "assistant", "content": f"<div class=\"note-box\">{msg}</div>"}
            )
            self._save_building_histories()
            return []
        replies = self.routers[persona_id].summon_to_user_room()
        self._save_building_histories()
        for router in self.routers.values():
            router._save_session_metadata()
        return replies

    def set_model(self, model: str) -> None:
        """Update LLM model for all routers."""
        self.model = model
        for router in self.routers.values():
            router.set_model(model)

    def get_building_history(self, building_id: str) -> List[Dict[str, str]]:
        history = self.building_histories.get(building_id, [])
        display: List[Dict[str, str]] = []
        for msg in history:
            if msg.get("role") == "assistant":
                pid = msg.get("persona_id")
                avatar = self.avatar_map.get(pid, self.default_avatar)
                try:
                    data = json.loads(msg.get("content", ""))
                    say = data.get("say", "")
                except json.JSONDecodeError:
                    say = msg.get("content", "")
                if avatar:
                    html = f"<img src='{avatar}' class='inline-avatar'>{say}"
                else:
                    html = f"{say}"
                display.append({"role": "assistant", "content": html})
            else:
                display.append(msg)
        return display

    def run_scheduled_prompts(self) -> List[str]:
        """Run scheduled prompts for all routers."""
        replies: List[str] = []
        for router in self.routers.values():
            replies.extend(router.run_scheduled_prompt())
        if replies:
            self._save_building_histories()
            for router in self.routers.values():
                router._save_session_metadata()
        return replies
