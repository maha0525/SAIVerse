"""手帳 / タスク帳の読み口 API テスト (api/routes/people/pocketbook.py)。

対象 (route 関数を直叩き、既存 api テストの流儀に従う):
- get_pocketbook: アクティビティとメモの形・日付降順・include_closed の切替・
  表が無いときの空配列・未知ペルソナの 404
- get_task_book: open な一件の形・閉じた行の除外・表が無いときの空配列・
  未知ペルソナの 404

読み取り専用の面なので、書き込みは fixture 側 (add_activity / add_memo /
add_entry) だけが行う。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.people.pocketbook import get_pocketbook, get_task_book
from database.models import AI, Base, City, User


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_db(persona_name="エア"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME=persona_name))
        db.commit()
    finally:
        db.close()
    return engine, Session


class PocketbookApiTest(unittest.TestCase):
    def setUp(self):
        from saiverse_memory import SAIMemoryAdapter

        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.engine, self.Session = _make_db()
        self.addCleanup(self.engine.dispose)

        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_path, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)
        persona = SimpleNamespace(sai_memory=self.adapter)
        self.manager = SimpleNamespace(
            SessionLocal=self.Session, personas={"tester": persona}
        )

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows では sqlite のハンドル解放が rmtree に間に合わないことがある。
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    # --- fixture helpers -------------------------------------------------

    def _add_activity(self, name, origin="sluice", born_at=None):
        from sai_memory.memory.pocketbook import add_activity

        with self.adapter._db_lock:
            return add_activity(
                self.adapter.conn, name, origin, born_at=born_at
            )

    def _add_memo(self, activity_id, date, kind, text, **kwargs):
        from sai_memory.memory.pocketbook import add_memo

        with self.adapter._db_lock:
            return add_memo(
                self.adapter.conn, activity_id, date, kind, text, **kwargs
            )

    def _close_activity(self, activity_id):
        from sai_memory.memory.pocketbook import close_activity

        with self.adapter._db_lock:
            return close_activity(self.adapter.conn, activity_id, closed_at=1_700_000_000)

    def _drop_pocketbook_tables(self):
        with self.adapter._db_lock:
            self.adapter.conn.execute("DROP TABLE IF EXISTS memos")
            self.adapter.conn.execute("DROP TABLE IF EXISTS activities")
            self.adapter.conn.commit()

    # --- 手帳 ------------------------------------------------------------

    def test_pocketbook_empty(self):
        resp = get_pocketbook("tester", manager=self.manager)
        self.assertEqual(resp.activities, [])
        self.assertFalse(resp.include_closed)

    def test_pocketbook_returns_activities_with_memos_newest_first(self):
        act = self._add_activity("小説を書く", origin="sluice", born_at=1_700_000_000)
        self._add_memo(act.id, "2026-08-20", "want", "星を拾う話を書きたい")
        self._add_memo(act.id, "2026-08-22", "did", "第一稿を書いた")
        self._add_memo(act.id, "2026-08-21", "did", "設定を練った")

        resp = get_pocketbook("tester", manager=self.manager)
        self.assertEqual(len(resp.activities), 1)
        a = resp.activities[0]
        self.assertEqual(a.id, act.id)
        self.assertEqual(a.name, "小説を書く")
        self.assertEqual(a.status, "open")
        self.assertEqual(a.origin, "sluice")
        self.assertEqual(a.born_at, 1_700_000_000)
        self.assertIsNone(a.closed_at)
        self.assertEqual(a.last_memo_date, "2026-08-22")
        # 日付降順 (新しいメモが上)
        self.assertEqual([m.date for m in a.memos], ["2026-08-22", "2026-08-21", "2026-08-20"])
        self.assertEqual([m.kind for m in a.memos], ["did", "did", "want"])
        # 本文は切り詰めない (ペルソナ本人の言葉)
        self.assertEqual(a.memos[2].text, "星を拾う話を書きたい")

    def test_pocketbook_memo_carries_span_ids(self):
        act = self._add_activity("絵の練習", origin="user")
        self._add_memo(
            act.id, "2026-08-22", "did", "手を描いた",
            span_start_id="msg-1", span_end_id="msg-9",
        )
        resp = get_pocketbook("tester", manager=self.manager)
        memo = resp.activities[0].memos[0]
        self.assertEqual(memo.span_start_id, "msg-1")
        self.assertEqual(memo.span_end_id, "msg-9")

    def test_pocketbook_excludes_closed_by_default(self):
        open_act = self._add_activity("開いている活動", born_at=1_700_000_000)
        closed_act = self._add_activity("閉じた活動", born_at=1_700_000_001)
        self.assertTrue(self._close_activity(closed_act.id))

        resp = get_pocketbook("tester", manager=self.manager)
        self.assertEqual([a.id for a in resp.activities], [open_act.id])

    def test_pocketbook_include_closed(self):
        open_act = self._add_activity("開いている活動", born_at=1_700_000_000)
        closed_act = self._add_activity("閉じた活動", born_at=1_700_000_001)
        self._close_activity(closed_act.id)

        resp = get_pocketbook("tester", include_closed=True, manager=self.manager)
        self.assertTrue(resp.include_closed)
        self.assertEqual([a.id for a in resp.activities], [open_act.id, closed_act.id])
        closed_row = resp.activities[1]
        self.assertEqual(closed_row.status, "closed")
        self.assertEqual(closed_row.closed_at, 1_700_000_000)

    def test_pocketbook_missing_tables_returns_empty(self):
        self._drop_pocketbook_tables()
        resp = get_pocketbook("tester", manager=self.manager)
        self.assertEqual(resp.activities, [])

    def test_pocketbook_unknown_persona_404(self):
        with self.assertRaises(HTTPException) as ctx:
            get_pocketbook("no-such-persona", manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_pocketbook_read_failure_is_not_flattened_to_empty(self):
        """表が無い以外の読み取り失敗は 500 — 空に丸めない。"""
        with patch(
            "sai_memory.memory.pocketbook.list_activities",
            side_effect=RuntimeError("db exploded"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                get_pocketbook("tester", manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 500)

    # --- タスク帳 --------------------------------------------------------

    def _add_task(self, content, **kwargs):
        from saiverse.task_book import add_entry

        kwargs.setdefault("origin", "sluice")
        return add_entry(self.manager, "tester", content, **kwargs)

    def test_task_book_empty(self):
        resp = get_task_book("tester", manager=self.manager)
        self.assertEqual(resp.tasks, [])

    def test_task_book_returns_open_entries(self):
        first = self._add_task("感想を返す", counterpart="user")
        second = self._add_task("資料を仕上げる", due_at=1_700_000_500)

        resp = get_task_book("tester", manager=self.manager)
        # 並びは (CREATED_AT, TASK_ID) 昇順。同一秒に作ると uuid が同着を割るので、
        # ここでは並びではなく集合と中身を固定する。
        self.assertEqual(
            {t.task_id for t in resp.tasks}, {first["task_id"], second["task_id"]}
        )
        by_id = {t.task_id: t for t in resp.tasks}
        a = by_id[first["task_id"]]
        self.assertEqual(a.content, "感想を返す")
        self.assertEqual(a.counterpart, "user")
        self.assertIsNone(a.due_at)      # 期限なしは正当な行
        self.assertEqual(a.status, "open")
        self.assertEqual(a.origin, "sluice")
        self.assertEqual(a.persona_id, "tester")
        b = by_id[second["task_id"]]
        self.assertEqual(b.due_at, 1_700_000_500)
        self.assertIsNone(b.counterpart)

    def test_task_book_excludes_closed(self):
        from saiverse.task_book import complete_entry, withdraw_entry

        keep = self._add_task("残る約束", counterpart="user")
        done = self._add_task("やり終えた約束", counterpart="user")
        gone = self._add_task("取り下げた約束", counterpart="user")
        complete_entry(self.manager, "tester", done["task_id"], outcome="返した")
        withdraw_entry(self.manager, "tester", gone["task_id"])

        resp = get_task_book("tester", manager=self.manager)
        self.assertEqual([t.task_id for t in resp.tasks], [keep["task_id"]])

    def test_task_book_carries_meta(self):
        entry = self._add_task(
            "メタ付きの約束", counterpart="user", meta={"source": "sluice"}
        )
        resp = get_task_book("tester", manager=self.manager)
        self.assertEqual(resp.tasks[0].task_id, entry["task_id"])
        self.assertEqual(resp.tasks[0].meta, {"source": "sluice"})

    def test_task_book_missing_table_returns_empty(self):
        from database.models import TaskBookEntry

        TaskBookEntry.__table__.drop(self.engine)
        resp = get_task_book("tester", manager=self.manager)
        self.assertEqual(resp.tasks, [])

    def test_task_book_unknown_persona_404(self):
        with self.assertRaises(HTTPException) as ctx:
            get_task_book("no-such-persona", manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_task_book_read_failure_is_not_flattened_to_empty(self):
        with patch(
            "saiverse.task_book.list_open", side_effect=RuntimeError("db exploded")
        ):
            with self.assertRaises(HTTPException) as ctx:
                get_task_book("tester", manager=self.manager)
        self.assertEqual(ctx.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
