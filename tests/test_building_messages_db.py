"""Phase 2+3 DB-single-source の検証テスト。

各書き込み経路 (add_message / add_to_building_only / update_building_message /
add_building_event) が building_messages テーブルに正しく書かれることを確認。
JSON 側の書き込みは廃止された。

詳細: docs/intent/building_memory_unified.md
"""
from __future__ import annotations

import gc
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, BuildingMessage
from database.building_messages import insert_building_message, fetch_building_messages, fetch_max_seq
from persona.history_manager import HistoryManager


class _PathMockMixin:
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


class TestBuildingMessageDB(_PathMockMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(gc.collect)
        self._start_path_mocks()

        self.building_id = "user_room"
        self.history_manager = HistoryManager(
            persona_id="test_persona",
            persona_log_path=Path("/mock/persona/log.json"),
            building_memory_paths={
                self.building_id: Path("/mock/buildings/user_room/log.json"),
            },
            initial_persona_history=[],
            db_session_factory=self.SessionLocal,
        )

    def _fetch_all(self) -> list:
        db = self.SessionLocal()
        try:
            return db.query(BuildingMessage).order_by(BuildingMessage.seq).all()
        finally:
            db.close()

    def test_add_message_writes_to_db(self) -> None:
        msg = {"role": "user", "content": "Hello"}
        self.history_manager.add_message(msg, self.building_id, heard_by=["test_persona"])
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "user")
        self.assertEqual(rows[0].content, "Hello")
        self.assertEqual(rows[0].seq, 1)
        self.assertEqual(rows[0].message_id, f"{self.building_id}:1")

    def test_add_to_building_only_writes_to_db(self) -> None:
        msg = {"role": "assistant", "content": "placeholder", "persona_id": "test_persona"}
        self.history_manager.add_to_building_only(
            self.building_id, msg, heard_by=["test_persona"]
        )
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "assistant")
        self.assertEqual(rows[0].persona_id, "test_persona")

    def test_db_assigns_independent_seq(self) -> None:
        """JSON 側 seq を渡しても DB は max(seq)+1 で独立採番する。"""
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id=self.building_id, seq=10, role="user",
                content="old", timestamp="2026-05-20T10:00:00",
                heard_by='[]', ingested_by='[]',
            ))
            db.commit()
        finally:
            db.close()
        self.history_manager.add_to_building_only(
            self.building_id,
            {"role": "user", "content": "new", "seq": 1, "message_id": "user_room:1"},
            heard_by=[],
        )
        rows = self._fetch_all()
        self.assertEqual(len(rows), 2)
        new_row = next(r for r in rows if r.content == "new")
        self.assertEqual(new_row.seq, 11)
        self.assertEqual(new_row.legacy_seq, 1)
        self.assertEqual(new_row.legacy_message_id, "user_room:1")

    def test_update_building_message_updates_db(self) -> None:
        msg = {"role": "assistant", "content": "", "persona_id": "test_persona"}
        saved = self.history_manager.add_to_building_only(
            self.building_id, msg, heard_by=["test_persona"]
        )
        msg_id = saved["message_id"]
        self.history_manager.update_building_message(
            self.building_id, msg_id,
            content="finalised",
            metadata={"llm_usage": {"input_tokens": 10}},
        )
        rows = self._fetch_all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].content, "finalised")
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
        self.assertEqual(rows[0].event_type, "occupancy")
        ev = json.loads(rows[0].event_data)
        self.assertEqual(ev["action"], "enter")
        residual = json.loads(rows[0].metadata_json)
        self.assertEqual(residual.get("tags"), ["world"])
        self.assertNotIn("event", residual)

    def test_fetch_building_messages_returns_dicts(self) -> None:
        self.history_manager.add_to_building_only(
            self.building_id, {"role": "user", "content": "hi"}, heard_by=[]
        )
        result = fetch_building_messages(self.SessionLocal, self.building_id)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], dict)
        self.assertEqual(result[0]["content"], "hi")

    def test_fetch_max_seq(self) -> None:
        self.assertEqual(fetch_max_seq(self.SessionLocal, self.building_id), 0)
        self.history_manager.add_to_building_only(
            self.building_id, {"role": "user", "content": "a"}, heard_by=[]
        )
        self.history_manager.add_to_building_only(
            self.building_id, {"role": "user", "content": "b"}, heard_by=[]
        )
        self.assertEqual(fetch_max_seq(self.SessionLocal, self.building_id), 2)


class TestInsertHelper(unittest.TestCase):
    """insert_building_message 単独テスト (HistoryManager 経由しない)。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(gc.collect)

    def test_insert_returns_dict_with_new_seq(self) -> None:
        msg = {
            "role": "host",
            "content": "moved",
            "timestamp": "2026-05-20T10:00:00",
            "heard_by": ["a"],
            "ingested_by": [],
            "metadata": {
                "event": {"type": "occupancy", "action": "leave"},
            },
        }
        result = insert_building_message(self.SessionLocal, "room", msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["seq"], 1)
        self.assertEqual(result["message_id"], "room:1")
        self.assertEqual(result["role"], "host")
        self.assertEqual(result["metadata"]["event"]["action"], "leave")
        self.assertIs(result["_was_inserted"], True)

    def test_duplicate_client_message_id_reports_existing_row(self) -> None:
        msg = {
            "role": "user",
            "content": "hello",
            "client_message_id": "command-1",
        }
        first = insert_building_message(self.SessionLocal, "room", msg)
        second = insert_building_message(self.SessionLocal, "room", msg)

        self.assertIs(first["_was_inserted"], True)
        self.assertIs(second["_was_inserted"], False)
        self.assertEqual(second["message_id"], first["message_id"])

    def test_insert_with_none_session_factory_is_noop(self) -> None:
        msg = {"role": "user", "content": "x"}
        result = insert_building_message(None, "room", msg)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()


class TestWithdrawBuildingMessage(_PathMockMixin, unittest.TestCase):
    """発言の取り下げ — 世界の記録から行を消す唯一の経路。

    取り消せるかどうかは好みでは決まらず、**ペルソナがもう読んだか**で決まる
    (2026-08-25 まはー裁定)。読まれた後に消すと、ペルソナは「聞いた覚えが
    あるのに記録が無い」状態になり、無言で消えるより悪い。
    設計: docs/issues/user_utterance_path_failure_inventory.md
    """

    BID = "room"

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(gc.collect)
        self._start_path_mocks()

    def _insert(self, *, role="user", content="こんばんは", ingested_by=None):
        saved = insert_building_message(
            self.SessionLocal, self.BID,
            {"role": role, "content": content, "heard_by": ["p1"], "ingested_by": []},
        )
        message_id = str(saved["message_id"])
        if ingested_by:
            db = self.SessionLocal()
            try:
                row = db.query(BuildingMessage).filter_by(
                    building_id=self.BID, message_id=message_id,
                ).first()
                row.ingested_by = json.dumps(ingested_by)
                db.commit()
            finally:
                db.close()
        return message_id

    def _withdraw(self, message_id):
        from database.building_messages import withdraw_building_message_in_db
        return withdraw_building_message_in_db(
            self.SessionLocal, self.BID, message_id,
        )

    def test_an_unread_message_is_withdrawn_and_its_text_comes_back(self):
        """まだ誰も読んでいない発言は取り下げ、本文を手元へ返す。"""
        mid = self._insert(content="やっぱりやめる")

        withdrawn, reason, content = self._withdraw(mid)

        self.assertTrue(withdrawn)
        self.assertEqual(reason, "withdrawn")
        self.assertEqual(content, "やっぱりやめる")
        self.assertEqual(fetch_building_messages(self.SessionLocal, self.BID), [])

    def test_a_message_someone_has_read_is_refused(self):
        """一人でも記憶へ転記していたら消さない。行はそのまま残る。"""
        mid = self._insert(ingested_by=["p1"])

        withdrawn, reason, content = self._withdraw(mid)

        self.assertFalse(withdrawn)
        self.assertEqual(reason, "already_heard")
        self.assertIsNone(content)
        self.assertEqual(len(fetch_building_messages(self.SessionLocal, self.BID)), 1)

    def test_only_user_messages_can_be_withdrawn(self):
        """取り消せるのは自分の発言だけ。ペルソナの発言は対象外。"""
        mid = self._insert(role="assistant")

        withdrawn, reason, _content = self._withdraw(mid)

        self.assertFalse(withdrawn)
        self.assertEqual(reason, "wrong_role")
        self.assertEqual(len(fetch_building_messages(self.SessionLocal, self.BID)), 1)

    def test_a_missing_message_reports_not_found(self):
        withdrawn, reason, _content = self._withdraw("room:9999")

        self.assertFalse(withdrawn)
        self.assertEqual(reason, "not_found")

    def test_unreadable_ingested_by_refuses_rather_than_deletes(self):
        """判断できないときは消さない方へ倒す (取り消しは元に戻せない)。"""
        mid = self._insert()
        db = self.SessionLocal()
        try:
            row = db.query(BuildingMessage).filter_by(
                building_id=self.BID, message_id=mid,
            ).first()
            row.ingested_by = "{壊れた JSON"
            db.commit()
        finally:
            db.close()

        withdrawn, reason, _content = self._withdraw(mid)

        self.assertFalse(withdrawn)
        self.assertEqual(reason, "already_heard")
        self.assertEqual(len(fetch_building_messages(self.SessionLocal, self.BID)), 1)

    def test_a_readable_but_non_list_ingested_by_also_refuses(self):
        """JSON として読めても配列でない値は、「誰も読んでいない」とは数えない。

        上の「壊れた JSON」は守られていたのに、``{}`` や ``null`` のように
        **読めてしまう**値はそのまま素通りして削除まで進んでいた。分かれ目は
        読めるかどうかではなく、「誰が読んだのかを言えるか」。
        """
        for broken in ("{}", "null", "123", '"p1"'):
            with self.subTest(ingested_by=broken):
                mid = self._insert()
                db = self.SessionLocal()
                try:
                    row = db.query(BuildingMessage).filter_by(
                        building_id=self.BID, message_id=mid,
                    ).first()
                    row.ingested_by = broken
                    db.commit()
                finally:
                    db.close()

                withdrawn, reason, _content = self._withdraw(mid)

                self.assertFalse(withdrawn)
                self.assertEqual(reason, "already_heard")

                db = self.SessionLocal()
                try:
                    still_there = db.query(BuildingMessage).filter_by(
                        building_id=self.BID, message_id=mid,
                    ).first()
                finally:
                    db.close()
                self.assertIsNotNone(still_there)
