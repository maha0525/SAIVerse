"""fetch_game_session_log (ゲームセッションログビュー) のテスト。

複数 Building の building_messages を挿入順で merge し、heard_by / since_ts /
カーソル (message_id) で絞り込むビューの検証。実 sqlite を使う。
"""
import json
import os
import tempfile
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.building_messages import fetch_game_session_log
from database.models import Base, BuildingMessage


class GameSessionLogTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self._seq = {}

    def tearDown(self):
        self.engine.dispose()
        os.unlink(self.db_path)

    def _add(self, building_id, content, *, heard_by=("1",), ts="2026-06-11T10:00:00",
             role="user"):
        seq = self._seq.get(building_id, 0) + 1
        self._seq[building_id] = seq
        db = self.SessionLocal()
        try:
            db.add(BuildingMessage(
                building_id=building_id,
                seq=seq,
                role=role,
                content=content,
                timestamp=ts,
                heard_by=json.dumps(list(heard_by), ensure_ascii=False),
                ingested_by="[]",
                message_id=f"{building_id}:{seq}",
            ))
            db.commit()
        finally:
            db.close()
        return f"{building_id}:{seq}"

    def _fetch(self, **kwargs):
        kwargs.setdefault("viewer_id", "1")
        return fetch_game_session_log(
            self.SessionLocal, ["b1", "b2"], **kwargs,
        )

    def test_cross_building_merge_in_insertion_order(self):
        self._add("b1", "m1")
        self._add("b2", "m2")
        self._add("b1", "m3")
        msgs, has_more = self._fetch()
        self.assertEqual([m["content"] for m in msgs], ["m1", "m2", "m3"])
        self.assertEqual([m["building_id"] for m in msgs], ["b1", "b2", "b1"])
        self.assertFalse(has_more)

    def test_heard_by_filter(self):
        self._add("b1", "for_user", heard_by=("1", "air"))
        self._add("b1", "not_for_user", heard_by=("air",))
        msgs, _ = self._fetch()
        self.assertEqual([m["content"] for m in msgs], ["for_user"])

    def test_heard_by_no_partial_id_match(self):
        # viewer "1" が "12" に部分一致しないこと
        self._add("b1", "for_12_only", heard_by=("12",))
        msgs, _ = self._fetch()
        self.assertEqual(msgs, [])

    def test_since_ts_excludes_pre_session(self):
        self._add("b1", "old", ts="2026-06-11T09:00:00")
        self._add("b1", "new", ts="2026-06-11T11:00:00")
        msgs, _ = self._fetch(since_ts="2026-06-11T10:00:00")
        self.assertEqual([m["content"] for m in msgs], ["new"])

    def test_building_scope_excludes_lobby(self):
        self._add("b1", "inside")
        self._add("lobby", "outside")
        msgs, _ = self._fetch()
        self.assertEqual([m["content"] for m in msgs], ["inside"])

    def test_after_cursor_polling(self):
        self._add("b1", "m1")
        anchor = self._add("b2", "m2")
        self._add("b1", "m3")
        self._add("b2", "m4")
        msgs, _ = self._fetch(after_message_id=anchor)
        self.assertEqual([m["content"] for m in msgs], ["m3", "m4"])

    def test_before_cursor_scrollback_with_has_more(self):
        ids = [self._add("b1", f"m{i}") for i in range(5)]
        msgs, has_more = self._fetch(before_message_id=ids[4], limit=2)
        self.assertEqual([m["content"] for m in msgs], ["m2", "m3"])
        self.assertTrue(has_more)
        msgs, has_more = self._fetch(before_message_id=ids[2], limit=5)
        self.assertEqual([m["content"] for m in msgs], ["m0", "m1"])
        self.assertFalse(has_more)

    def test_tail_limit_with_has_more(self):
        for i in range(5):
            self._add("b1", f"m{i}")
        msgs, has_more = self._fetch(limit=3)
        self.assertEqual([m["content"] for m in msgs], ["m2", "m3", "m4"])
        self.assertTrue(has_more)

    def test_unknown_cursor_returns_empty(self):
        self._add("b1", "m1")
        msgs, has_more = self._fetch(after_message_id="nope:99")
        self.assertEqual(msgs, [])
        self.assertFalse(has_more)

    def test_empty_building_list(self):
        msgs, has_more = fetch_game_session_log(self.SessionLocal, [], viewer_id="1")
        self.assertEqual(msgs, [])
        self.assertFalse(has_more)


if __name__ == "__main__":
    unittest.main()
