import base64
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Iterator

from city_manager import CityManager
from persona_core import PersonaCore
from model_configs import get_model_provider, get_context_length


DEFAULT_MODEL = "gpt-4o"


class SAIVerseManager:
    """Manage multiple personas and building occupancy."""

    def __init__(
        self,
        persona_list_path: Path = Path("ai_sessions/personas.json"),
        model: str = DEFAULT_MODEL,
    ):
        self.city = CityManager()
        self.saiverse_home = Path.home() / ".saiverse"
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)

        self.buildings = self.city.buildings
        self.building_map = self.city.building_map
        self.capacities = self.city.capacities
        self.building_memory_paths = self.city.building_memory_paths
        self.building_histories = self.city.building_histories
        default_avatar_path = Path("assets/icons/blank.png")
        if default_avatar_path.exists():
            mime = "image/png"
            data_b = default_avatar_path.read_bytes()
            b64 = base64.b64encode(data_b).decode("ascii")
            self.default_avatar = f"data:{mime};base64,{b64}"
        else:
            self.default_avatar = ""
        data = json.loads(Path(persona_list_path).read_text(encoding="utf-8"))
        self.personas: Dict[str, PersonaCore] = {}
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
            core = PersonaCore(
                city=self.city,
                common_prompt_path=Path("system_prompts/common.txt"),
                persona_base=base,
                action_priority_path=Path("action_priority.json"),
                start_building_id=start_id,
                model=self.model,
                context_length=self.context_length,
                provider=self.provider,
            )
            self.personas[pid] = core
            self.city.add_persona(pid, core.current_building_id)
        self.persona_map = {p["persona_name"]: p["persona_id"] for p in data}


    def handle_user_input(self, message: str) -> List[str]:
        replies: List[str] = []
        for pid in list(self.city.occupants.get("user_room", [])):
            replies.extend(self.personas[pid].handle_user_input(message))
        self.city.save_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def handle_user_input_stream(self, message: str) -> Iterator[str]:
        for pid in list(self.city.occupants.get("user_room", [])):
            for token in self.personas[pid].handle_user_input_stream(message):
                yield token
        self.city.save_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()

    def summon_persona(self, persona_id: str) -> List[str]:
        if persona_id not in self.personas:
            return []
        replies = self.personas[persona_id].summon_to_user_room()
        self.city.save_histories()
        for persona in self.personas.values():
            persona._save_session_metadata()
        return replies

    def set_model(self, model: str) -> None:
        """Update LLM model for all routers."""
        self.model = model
        self.context_length = get_context_length(model)
        self.provider = get_model_provider(model)
        for persona in self.personas.values():
            persona.set_model(model, self.context_length, self.provider)

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
        for persona in self.personas.values():
            replies.extend(persona.run_scheduled_prompt())
        if replies:
            self.city.save_histories()
            for persona in self.personas.values():
                persona._save_session_metadata()
        return replies
