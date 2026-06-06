from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from persona.mixins import PersonaHistoryMixin


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

    def get_building_history(self, building_id):
        return self.building_histories.get(building_id, [])

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
        self.SessionLocal = lambda: None
        self.is_visitor = True
        self.occupants = {}
        self.messages = []


def test_timestamp_to_epoch_parses_iso_string():
    persona = SimpleHistoryPersona()
    epoch = persona._timestamp_to_epoch("2025-01-02T12:34:56+09:00")
    expected = int(datetime(2025, 1, 2, 3, 34, 56, tzinfo=dt_timezone.utc).timestamp())
    assert epoch == expected
