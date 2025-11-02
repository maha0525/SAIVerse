import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from datetime import datetime, timezone as dt_timezone, tzinfo, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from buildings import Building
from saiverse_memory import SAIMemoryAdapter
from llm_clients import get_llm_client
from model_configs import model_supports_images
from action_handler import ActionHandler
from history_manager import HistoryManager
from emotion_module import EmotionControlModule
from database.models import AI as AIModel
from persona.bootstrap import (
    initialise_memory_adapter,
    load_action_priority,
    load_session_data,
)
from persona.constants import (
    RECALL_SNIPPET_PULSE_MAX_CHARS,
)
from persona.history import initialise_pulse_state
from persona.tasks import TaskStorage
from persona.mixins import (
    PersonaGenerationMixin,
    PersonaHistoryMixin,
    PersonaMovementMixin,
    PersonaEmotionMixin,
    PersonaPulseMixin,
)

load_dotenv()


class PersonaCore(
    PersonaGenerationMixin,
    PersonaHistoryMixin,
    PersonaMovementMixin,
    PersonaEmotionMixin,
    PersonaPulseMixin,
):
    def __init__(
        self,
        city_name: str,
        persona_id: str,
        persona_name: str,
        persona_system_instruction: str,
        avatar_image: Optional[str],
        buildings: List[Building],
        common_prompt_path: Path,
        session_factory: Callable,
        is_visitor: bool = False,
        home_city_id: Optional[str] = None, # ★ 故郷のCity ID
        interaction_mode: str = "auto", # ★ 現在の対話モード
        is_dispatched: bool = False, # ★ このペルソナが他のCityに派遣中かどうかのフラグ
        emotion_prompt_path: Path = Path("system_prompts/emotion_parameter.txt"),
        action_priority_path: Path = Path("action_priority.json"),
        building_histories: Optional[Dict[str, List[Dict[str, str]]]] = None,
        occupants: Optional[Dict[str, List[str]]] = None,
        id_to_name_map: Optional[Dict[str, str]] = None,
        move_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        dispatch_callback: Optional[Callable[[str, str, str], Tuple[bool, Optional[str]]]] = None,
        explore_callback: Optional[Callable[[str, str], None]] = None, # New callback
        create_persona_callback: Optional[Callable[[str, str], Tuple[bool, str]]] = None,
        start_building_id: str = "air_room",
        model: str = "gpt-4o",
        context_length: int = 120000,
        user_room_id: str = "user_room",
        provider: str = "ollama",
        timezone_info: Optional[tzinfo] = dt_timezone.utc,
        timezone_name: str = "UTC",
    ):
        self.city_name = city_name
        self.is_visitor = is_visitor
        self.is_dispatched = is_dispatched
        self.interaction_mode = interaction_mode
        self.home_city_id = home_city_id # ★ 故郷の情報を記憶
        self.SessionLocal = session_factory
        self.buildings: Dict[str, Building] = {b.building_id: b for b in buildings}
        self.user_room_id = user_room_id
        self.common_prompt = common_prompt_path.read_text(encoding="utf-8")
        self.emotion_prompt = emotion_prompt_path.read_text(encoding="utf-8")
        self.persona_id = persona_id
        self.persona_name = persona_name
        self.persona_system_instruction = persona_system_instruction
        self.avatar_image = avatar_image
        self.saiverse_home = Path.home() / ".saiverse"
        self.persona_log_path = (
            self.saiverse_home / "personas" / self.persona_id / "log.json"
        )
        self.conscious_log_path = (
            self.saiverse_home / "personas" / self.persona_id / "conscious_log.json"
        )
        self.task_storage = TaskStorage(self.persona_id, base_dir=self.saiverse_home)
        self.building_memory_paths: Dict[str, Path] = {
            b_id: self.saiverse_home / "buildings" / b_id / "log.json"
            for b_id in self.buildings
        }
        self.action_priority = load_action_priority(action_priority_path)
        self.action_handler = ActionHandler(self.action_priority)

        self.occupants = occupants if occupants is not None else {}
        self.id_to_name_map = id_to_name_map if id_to_name_map is not None else {}

        # Initialize stateful attributes with defaults before loading session
        self.current_building_id = start_building_id
        self.auto_count = 0
        self.last_auto_prompt_times: Dict[str, float] = {b_id: time.time() for b_id in self.buildings}
        self.emotion = {"stability": {"mean": 0, "variance": 1}, "affect": {"mean": 0, "variance": 1}, "resonance": {"mean": 0, "variance": 1}, "attitude": {"mean": 0, "variance": 1}}
        self.pulse_cursors: Dict[str, int] = {}
        self.entry_markers: Dict[str, int] = {}
        self._raw_pulse_cursor_data: Dict[str, Any] = {}
        self._raw_pulse_cursor_format: str = "count"

        # Load session data, which may overwrite the defaults
        load_session_data(self)

        # Initialise SAIMemory bridge for long-term recall/summary
        self.sai_memory: Optional[SAIMemoryAdapter] = initialise_memory_adapter(self)

        # Initialize managers that depend on loaded data
        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=self.messages,
            initial_building_histories=building_histories,
            memory_adapter=self.sai_memory,
        )

        # Configure pulse tracking based on loaded histories
        initialise_pulse_state(self)

        # Initialize remaining attributes
        self.move_callback = move_callback
        self.dispatch_callback = dispatch_callback
        self.explore_callback = explore_callback
        self.create_persona_callback = create_persona_callback
        self.model = model
        self.context_length = context_length
        self.model_supports_images = model_supports_images(model)
        self.llm_client = get_llm_client(model, provider, self.context_length)
        self.emotion_module = EmotionControlModule()
        tz_label = (timezone_name or "UTC").strip() or "UTC"
        if isinstance(timezone_info, str):
            candidate = timezone_info.strip() or tz_label
            try:
                tz_obj = ZoneInfo(candidate)
                tz_label = candidate
            except Exception:
                logging.warning("PersonaCore received invalid timezone '%s'. Falling back to UTC.", candidate)
                tz_obj = dt_timezone.utc
                tz_label = "UTC"
        elif timezone_info is None:
            tz_obj = dt_timezone.utc
        else:
            tz_obj = timezone_info
        self.timezone = tz_obj
        self.timezone_name = tz_label
        self._last_conscious_prompt_time_utc: Optional[datetime] = None
        self.pending_attachment_metadata: List[Dict[str, Any]] = []
