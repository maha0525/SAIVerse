import json
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Optional

import pytest

from persona.mixins import (
    PersonaEmotionMixin,
    PersonaHistoryMixin,
    PersonaMovementMixin,
)


class DummyHistoryManager:
    def __init__(self):
        self.building_histories = {}
        self.persona_messages = []
        self.building_messages = []

    def add_message(self, message, building_id, heard_by=None):
        self.building_messages.append((building_id, message, heard_by))

    def add_to_persona_only(self, message):
        self.persona_messages.append(message)

    def add_to_building_only(self, building_id, message, heard_by=None):
        self.building_messages.append((building_id, message, heard_by))

    def save_all(self):
        pass

    def get_recent_history(self, _limit):
        return []

    @property
    def building_names(self):
        return {}


class SimpleHistoryPersona(PersonaHistoryMixin):
    def __init__(self):
        self.persona_id = "persona"
        self.persona_name = "Persona"
        self.timezone = dt_timezone.utc
        self.timezone_name = "UTC"
        self.history_manager = DummyHistoryManager()
        self.entry_markers = {}
        self.pulse_cursors = {}
        self.conscious_log = []
        self.conscious_log_path = Path("/tmp/conscious_log.json")
        self.SessionLocal = lambda: None  # not used in these tests
        self.is_visitor = True
        self.auto_count = 0
        self.last_auto_prompt_times = {}
        self.emotion = {}
        self.occupants = {}
        self.messages = []


class MovementPersona(PersonaHistoryMixin, PersonaMovementMixin):
    def __init__(self, tmp_path: Path):
        self.persona_id = "p1"
        self.persona_name = "Mover"
        self.timezone = dt_timezone.utc
        self.timezone_name = "UTC"
        self.history_manager = DummyHistoryManager()
        self.history_manager.building_histories = {"room": [], "hall": []}
        self.entry_markers = {}
        self.pulse_cursors = {}
        self.conscious_log = []
        self.conscious_log_path = tmp_path / "conscious.json"
        self.SessionLocal = lambda: None
        self.is_visitor = True
        self.auto_count = 0
        self.last_auto_prompt_times = {}
        self.emotion = {}
        self.occupants = {"room": [], "hall": []}
        self.messages = []
        self.buildings = {
            "room": DummyBuilding("room"),
            "hall": DummyBuilding("hall"),
        }
        self.current_building_id = "room"
        self.move_callback = lambda persona_id, src, dest: (True, None)
        self.dispatch_callback = None
        self.explore_callback = None
        self.create_persona_callback = None

    def _generate(self, *args, **kwargs):
        return "auto", None, False

    def _save_session_metadata(self):
        pass


@dataclass
class DummyBuilding:
    building_id: str
    run_entry_llm: bool = False
    auto_prompt: Optional[str] = None
    auto_interval_sec: int = 0
    name: str = "Building"


class DummyEmotionModule:
    def __init__(self, response):
        self.response = response

    def evaluate(self, *args, **kwargs):
        return self.response


class EmotionPersona(PersonaHistoryMixin, PersonaEmotionMixin):
    def __init__(self):
        self.persona_id = "emo"
        self.persona_name = "Emo"
        self.timezone = dt_timezone.utc
        self.timezone_name = "UTC"
        self.history_manager = DummyHistoryManager()
        self.history_manager.building_histories = {"room": []}
        self.entry_markers = {}
        self.pulse_cursors = {}
        self.conscious_log = []
        self.conscious_log_path = Path("/tmp/conscious.json")
        self.SessionLocal = lambda: None
        self.is_visitor = True
        self.auto_count = 0
        self.last_auto_prompt_times = {}
        self.occupants = {"room": []}
        self.messages = []
        self.current_building_id = "room"
        self.emotion = {
            "stability": {"mean": 0.0, "variance": 1.0},
            "affect": {"mean": 0.0, "variance": 1.0},
            "resonance": {"mean": 0.0, "variance": 1.0},
            "attitude": {"mean": 0.0, "variance": 1.0},
        }
        self.emotion_module = DummyEmotionModule(
            [{"stability": {"mean": 5, "variance": -0.5}}]
        )


def test_timestamp_to_epoch_parses_iso_string():
    persona = SimpleHistoryPersona()
    epoch = persona._timestamp_to_epoch("2025-01-02T12:34:56+09:00")
    expected = int(datetime(2025, 1, 2, 3, 34, 56, tzinfo=dt_timezone.utc).timestamp())
    assert epoch == expected


def test_handle_movement_local_move_updates_current_building(tmp_path):
    persona = MovementPersona(tmp_path)
    moved = persona._handle_movement({"building": "hall"})
    assert moved is True
    assert persona.current_building_id == "hall"
    assert persona.entry_markers["hall"] == 0


def test_post_response_updates_records_emotion_summary():
    persona = EmotionPersona()
    prev = json.loads(json.dumps(persona.emotion))
    persona._post_response_updates(prev, "hello", None, "world")
    assert persona.history_manager.persona_messages, "summary should be logged"
    summary_entry = persona.history_manager.persona_messages[-1]
    assert summary_entry["role"] == "system"
