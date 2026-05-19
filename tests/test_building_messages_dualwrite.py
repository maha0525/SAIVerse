"""Phase 1 dual-write の検証テスト。

各書き込み経路 (add_message / add_to_building_only / update_building_message /
add_building_event) が、 JSON 側の `building_histories` dict と、
DB 側の `building_messages` テーブル両方に書き込まれることを確認する。

詳細: docs/intent/building_memory_unified.md (Phase 1)
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, BuildingMessage
from persona.history_manager import HistoryManager
from database.building_messages import insert_building_message


class _PathMockMixin:
    """Path I/O を全モックして file system に触らないようにする setUp 補助。"""

    def _start_path_mocks(self) -> None:
        self._patchers = [
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="[]"),
            patch("pathlib.Path.write_text"),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.glob", return_value=[]),
        ]
        for p in self._patchers:
            p.start()
        stat_patcher = patch("pathlib.Path.stat")
        m_stat = stat_patcher.start()
        m_stat.return_value.st_size = 0
        self._patchers.append(stat_patcher)
        self.addCleanup(patch.stopall)


class TestBuildingMessageDualWrite(_PathMockMixin, unittest.TestCase):
    def setUp(self) -> None:
        # In-memory SQLite engine + sessionmaker
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)

        self._start_path_mocks()

        self.building_id = "user_room"
        self.history_manager = HistoryManager(
            persona_id="test_persona",
            persona_log_path=Path("/mock/persona/log.json"),
            building_memory_paths={
                self.building_id: Path("/mock/buildings/user_room/log.json"),
            },
            initial_persona_history=[],
            initial_building_histories={self.building_id: []},
            db_session_factory=self.SessionLocal,
        )

    def _fetch_all(self) -> list:
        db = self.SessionLocal()
        try:
            return db.query(BuildingMessage).order_by(BuildingMessage.seq).all()
        finally:
            db.close()

    def test_add_message_mirrors_to_db(self) -> None:
        msg = {"role": "user", "content": "Hello"}
        self.history_manager.add_message(msg, self.building_id, heard_by=["test_persona"])

        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.building_id, self.building_id)
        self.assertEqual(row.role, "user")
        self.assertEqual(row.content, "Hello")
        self.assertEqual(row.seq, 1)
        self.assertEqual(row.message_id, f"{self.building_id}:1")
        self.assertEqual(json.loads(row.heard_by), ["test_persona"])
        self.assertIsNone(row.event_type)

    def test_add_to_building_only_mirrors_to_db(self) -> None:
        msg = {"role": "assistant", "content": "placeholder", "persona_id": "test_persona"}
        self.history_manager.add_to_building_only(
            self.building_id, msg, heard_by=["test_persona"]
        )
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "assistant")
        self.assertEqual(rows[0].content, "placeholder")
        self.assertEqual(rows[0].persona_id, "test_persona")

    def test_update_building_message_mirrors_content_and_metadata(self) -> None:
        msg = {"role": "assistant", "content": "", "persona_id": "test_persona"}
        saved = self.history_manager.add_to_building_only(
            self.building_id, msg, heard_by=["test_persona"]
        )
        msg_id = saved["message_id"]

        self.history_manager.update_building_message(
            self.building_id,
            msg_id,
            content="finalised text",
            metadata={"llm_usage": {"input_tokens": 10}},
        )
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "finalised text")
        meta = json.loads(rows[0].metadata_json)
        self.assertEqual(meta.get("llm_usage"), {"input_tokens": 10})

    def test_event_metadata_serialized_to_structured_columns(self) -> None:
        msg = {
            "role": "host",
            "content": "<div>noted</div>",
            "metadata": {
                "event": {
                    "type": "occupancy",
                    "action": "enter",
                    "entity_id": "test_persona",
                    "from_building_id": "lobby",
                    "to_building_id": self.building_id,
                },
                "tags": ["world"],
            },
        }
        self.history_manager.add_to_building_only(self.building_id, msg, heard_by=[])
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.event_type, "occupancy")
        ev = json.loads(row.event_data)
        self.assertEqual(ev["action"], "enter")
        self.assertEqual(ev["entity_id"], "test_persona")
        # event 以外の metadata は metadata_json に分離されている
        residual = json.loads(row.metadata_json)
        self.assertEqual(residual.get("tags"), ["world"])
        self.assertNotIn("event", residual)

    def test_db_failure_does_not_raise(self) -> None:
        """DB 書き込み失敗時も JSON 側は成立し、 例外は外に出ない。"""
        # invalid factory: returns object without commit method
        broken_factory = lambda: object()
        hm = HistoryManager(
            persona_id="t2",
            persona_log_path=Path("/mock/persona/log.json"),
            building_memory_paths={
                self.building_id: Path("/mock/buildings/user_room/log.json"),
            },
            initial_persona_history=[],
            initial_building_histories={self.building_id: []},
            db_session_factory=broken_factory,
        )
        # 例外を投げず、 JSON 側にだけ書き込まれる
        result = hm.add_message({"role": "user", "content": "x"}, self.building_id, heard_by=[])
        self.assertEqual(result["content"], "x")
        # DB には 1 件も入っていない (in-memory engine と別なので当然)
        rows = self._fetch_all()
        self.assertEqual(len(rows), 0)


class TestAddBuildingEventDualWrite(unittest.TestCase):
    """manager/history.py の add_building_event 経路の dual-write 検証。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)

    def test_insert_via_shared_helper_event_columns(self) -> None:
        """insert_building_message 経由で event_type が DB に書かれることを確認。"""
        enriched = {
            "role": "host",
            "content": "moved",
            "timestamp": "2026-05-20T10:00:00",
            "seq": 5,
            "message_id": "room:5",
            "heard_by": ["a"],
            "ingested_by": [],
            "metadata": {
                "event": {
                    "type": "occupancy",
                    "action": "leave",
                    "entity_id": "1",
                    "from_building_id": "room",
                    "to_building_id": "next",
                }
            },
        }
        insert_building_message(self.SessionLocal, "room", enriched)
        db = self.SessionLocal()
        try:
            rows = db.query(BuildingMessage).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].event_type, "occupancy")
            ev = json.loads(rows[0].event_data)
            self.assertEqual(ev["action"], "leave")
            self.assertEqual(rows[0].seq, 5)
            self.assertEqual(rows[0].message_id, "room:5")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
