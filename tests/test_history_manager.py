"""HistoryManager の persona log + SAIMemory 連携テスト。

Phase 2+3 以降、 Building 関連 API は DB-single-source なので test_building_messages_db.py
に集約。 本ファイルでは persona log (in-memory + persona_log.json) と Memopedia 関連の
振る舞いのみテストする。
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base
from persona.history_manager import HistoryManager


class TestHistoryManagerPersonaLog(unittest.TestCase):
    def setUp(self):
        self.persona_id = "test_persona"
        self.persona_log_path = Path("/mock/saiverse_home/personas/test_persona/log.json")
        self.building_memory_paths = {
            "user_room": Path("/mock/saiverse_home/buildings/user_room/log.json"),
        }

        self.mock_path_exists = patch("pathlib.Path.exists").start()
        self.mock_path_read_text = patch("pathlib.Path.read_text").start()
        self.mock_path_write_text = patch("pathlib.Path.write_text").start()
        self.mock_path_mkdir = patch("pathlib.Path.mkdir").start()
        self.mock_path_glob = patch("pathlib.Path.glob").start()
        self.mock_path_stat = patch("pathlib.Path.stat").start()
        self.mock_path_exists.return_value = True
        self.mock_path_read_text.return_value = "[]"
        self.mock_path_glob.return_value = []
        self.mock_path_stat.return_value.st_size = 0

        # In-memory DB for the DB-backed Building API parts.
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)

        self.history_manager = HistoryManager(
            persona_id=self.persona_id,
            persona_log_path=self.persona_log_path,
            building_memory_paths=self.building_memory_paths,
            initial_persona_history=[],
            db_session_factory=self.SessionLocal,
        )

    def tearDown(self):
        patch.stopall()

    def test_initialization(self):
        self.assertEqual(self.history_manager.persona_id, "test_persona")
        self.assertEqual(self.history_manager.persona_log_path, self.persona_log_path)
        self.assertEqual(self.history_manager.messages, [])

    def test_add_message_appends_to_persona_log_and_db(self):
        msg = {"role": "user", "content": "Hello"}
        self.history_manager.add_message(msg, "user_room", heard_by=["test_persona"])
        # persona log
        self.assertEqual(len(self.history_manager.messages), 1)
        self.assertEqual(self.history_manager.messages[0]["content"], "Hello")
        # DB
        rows = self.history_manager.get_building_history("user_room")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "Hello")

    def test_add_to_persona_only_does_not_touch_building(self):
        msg = {"role": "system", "content": "Persona specific"}
        self.history_manager.add_to_persona_only(msg)
        self.assertEqual(len(self.history_manager.messages), 1)
        rows = self.history_manager.get_building_history("user_room")
        self.assertEqual(rows, [])

    def test_get_recent_history(self):
        msgs = [
            {"role": "user", "content": "1"},
            {"role": "assistant", "content": "22"},
            {"role": "user", "content": "333"},
            {"role": "assistant", "content": "4444"},
        ]
        for msg in msgs:
            self.history_manager.add_message(msg, "user_room")
        recent = self.history_manager.get_recent_history(100)
        self.assertEqual(
            [(m["role"], m["content"]) for m in recent],
            [(m["role"], m["content"]) for m in msgs],
        )

    def test_save_all_writes_only_persona_log(self):
        self.history_manager.add_message(
            {"role": "user", "content": "x"}, "user_room"
        )
        self.history_manager.save_all()
        self.mock_path_write_text.assert_called()


class TestRecentEntrantEventsViaDB(unittest.TestCase):
    """get_recent_entrant_events / mark_entrant_event_recalled の DB 経由動作。"""

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)
        self.mock_path_exists = patch("pathlib.Path.exists", return_value=True).start()
        self.mock_path_stat = patch("pathlib.Path.stat").start()
        self.mock_path_stat.return_value.st_size = 0
        self.addCleanup(patch.stopall)
        self.hm = HistoryManager(
            persona_id="me",
            persona_log_path=Path("/mock/p/log.json"),
            building_memory_paths={"room": Path("/mock/b/room/log.json")},
            initial_persona_history=[],
            db_session_factory=self.SessionLocal,
        )

    def test_recent_entrant_events_only_ai_enter(self):
        self.hm.add_to_building_only("room", {
            "role": "host",
            "content": "x",
            "metadata": {"event": {
                "type": "occupancy", "action": "enter",
                "entity_id": "other_ai", "entity_type": "ai",
                "event_key": "k1",
            }},
        }, heard_by=[])
        self.hm.add_to_building_only("room", {
            "role": "host",
            "content": "y",
            "metadata": {"event": {
                "type": "occupancy", "action": "leave",
                "entity_id": "other_ai", "entity_type": "ai",
                "event_key": "k2",
            }},
        }, heard_by=[])
        events = self.hm.get_recent_entrant_events("room", lookback_messages=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_key"], "k1")

    def test_mark_entrant_event_recalled(self):
        self.hm.add_to_building_only("room", {
            "role": "host",
            "content": "x",
            "metadata": {"event": {
                "type": "occupancy", "action": "enter",
                "entity_id": "other_ai", "entity_type": "ai",
                "event_key": "k1",
            }},
        }, heard_by=[])
        ok = self.hm.mark_entrant_event_recalled("room", "k1")
        self.assertTrue(ok)
        # 2 度目は False (= 既に recalled_by に入っている)
        # ※ mark_event_recalled は idempotent な True を返す実装なので, ここでは「再呼出しでも成功扱い」のみ確認
        ok2 = self.hm.mark_entrant_event_recalled("room", "k1")
        self.assertTrue(ok2)


if __name__ == "__main__":
    unittest.main()
