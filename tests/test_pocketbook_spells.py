"""手帳のスペル 2 本のテスト (pocketbook_open / pocketbook_write)。

正典: docs/intent/autonomous_behavior_v3.md §13.2.1 (本人が開く／書く口)。

- 開く: 空 / 目次と最近のページ / activity 指定 / limit 超過のめくり /
  本文を切らない / 約束の欄 / 表が無いときの空扱い / 読み取り失敗は空に丸めない
- 書く: want で新アクティビティ / did で既存へ追加 / promise がタスク帳へ /
  期限のローカル解釈 / 期限も相手も無い約束の拒否 / kind 不正の拒否

実 SAIMemory (temp DB) + temp 中央 DB + 合成ペルソナ。LLM 呼び出しは無い
(どちらのスペルも読み書きだけで、モデルを呼ばない)。
"""
from __future__ import annotations

import gc
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builtin_data.tools.pocketbook_open import pocketbook_open
from builtin_data.tools.pocketbook_write import pocketbook_write
from tools.context import persona_context


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class _PocketbookSpellTestBase(unittest.TestCase):
    """一時 SAIVERSE_HOME + 合成ペルソナ + 手帳/タスク帳の器を持つ共通 setup。"""

    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from database.models import AI, Base
        from saiverse_memory import SAIMemoryAdapter

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.persona_path = root / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        self._env_backup = {
            key: os.environ.get(key)
            for key in ("SAIVERSE_HOME", "SAIMEMORY_MEMORY")
        }
        os.environ["SAIVERSE_HOME"] = str(root)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._restore_env)
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        db_path = str(root / "central.db")
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        db = self.SessionLocal()
        try:
            db.add(AI(AIID="tester", HOME_CITYID=1, AINAME="エア"))
            db.commit()
        finally:
            db.close()
        self.addCleanup(self.engine.dispose)

        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_path, resource_id="tester"
        )
        self.addCleanup(self._close_adapter)
        self.persona = SimpleNamespace(persona_id="tester", sai_memory=self.adapter)
        self.manager = SimpleNamespace(
            SessionLocal=self.SessionLocal, personas={"tester": self.persona}
        )

    def _restore_env(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _close_adapter(self):
        try:
            self.adapter.close()
        except Exception:
            pass

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            # Windows では sqlite のハンドル解放が rmtree に間に合わないことがある。
            pass

    # --- fixture helpers -------------------------------------------------

    def _add_activity(self, name, origin="sluice", born_at=None):
        from sai_memory.memory.pocketbook import add_activity

        with self.adapter._db_lock:
            return add_activity(self.adapter.conn, name, origin, born_at=born_at)

    def _add_memo(self, activity_id, date, kind, text):
        from sai_memory.memory.pocketbook import add_memo

        with self.adapter._db_lock:
            return add_memo(self.adapter.conn, activity_id, date, kind, text)

    def _add_task(self, content, **kwargs):
        from saiverse.task_book import add_entry

        kwargs.setdefault("origin", "user")
        return add_entry(self.manager, "tester", content, **kwargs)

    def _open_tasks(self):
        from saiverse.task_book import list_open

        return list_open(self.manager, "tester")

    def _activities(self, include_closed=False):
        from sai_memory.memory.pocketbook import list_activities

        with self.adapter._db_lock:
            return list_activities(self.adapter.conn, include_closed=include_closed)

    def _memo_rows(self):
        return self.adapter.conn.execute(
            "SELECT activity_id, date, kind, text FROM memos ORDER BY id ASC"
        ).fetchall()

    # --- spell drivers ---------------------------------------------------

    def _open(self, **kwargs):
        with persona_context(
            "tester", self.persona_path, manager=self.manager
        ):
            return pocketbook_open(**kwargs)

    def _write(self, **kwargs):
        with persona_context(
            "tester", self.persona_path, manager=self.manager
        ):
            return pocketbook_write(**kwargs)


class PocketbookOpenSpellTest(_PocketbookSpellTestBase):
    def test_empty_book_says_nothing_written_yet(self):
        text = self._open()
        self.assertIn("手帳にはまだ何も書いていません", text)
        self.assertIn("開いている約束はありません", text)

    def test_index_and_recent_page(self):
        novel = self._add_activity("小説を書く", born_at=1_700_000_000)
        drawing = self._add_activity("絵の練習", born_at=1_700_000_001)
        self._add_memo(novel.id, "2026-08-20", "want", "星を拾う話を書きたい")
        self._add_memo(novel.id, "2026-08-22", "did", "第一稿を書いた")
        self._add_memo(drawing.id, "2026-08-21", "did", "クロッキーを30分")

        text = self._open()
        # 目次: 件数と最後に書いた日。
        self.assertIn("- 小説を書く（メモ 2 件、最後に書いた日 2026-08-22）", text)
        self.assertIn("- 絵の練習（メモ 1 件、最後に書いた日 2026-08-21）", text)
        # 最近のページ: 全アクティビティ横断で新しい順。
        page = text.split("■ 最近のページ")[1]
        lines = [ln for ln in page.splitlines() if ln.startswith("- ")]
        self.assertEqual(lines[0], "- 2026-08-22 [やった] 小説を書く: 第一稿を書いた")
        self.assertEqual(lines[1], "- 2026-08-21 [やった] 絵の練習: クロッキーを30分")
        self.assertEqual(lines[2], "- 2026-08-20 [やりたい] 小説を書く: 星を拾う話を書きたい")

    def test_activity_with_no_memo_is_listed_as_not_written_yet(self):
        self._add_activity("まだ何もしていない活動")
        text = self._open()
        self.assertIn("- まだ何もしていない活動（メモ 0 件、まだ書いていません）", text)

    def test_activity_scoped_open_hides_the_promise_column(self):
        novel = self._add_activity("小説を書く")
        drawing = self._add_activity("絵の練習")
        self._add_memo(novel.id, "2026-08-22", "did", "第一稿を書いた")
        self._add_memo(drawing.id, "2026-08-22", "did", "クロッキーを30分")
        self._add_task("感想を返す", counterpart="user")

        text = self._open(activity="小説を書く")
        self.assertIn("【手帳】小説を書く", text)
        self.assertIn("- 2026-08-22 [やった] 第一稿を書いた", text)
        self.assertNotIn("クロッキーを30分", text)
        self.assertNotIn("■ 約束の欄", text)
        self.assertNotIn("感想を返す", text)

    def test_unknown_activity_name_lists_the_open_ones(self):
        self._add_activity("小説を書く")
        text = self._open(activity="料理")
        self.assertIn("手帳に「料理」のページはありません", text)
        self.assertIn("小説を書く", text)

    def test_over_limit_shows_how_to_turn_the_page_and_before_works(self):
        novel = self._add_activity("小説を書く")
        for day in range(18, 23):  # 2026-08-18 〜 2026-08-22 の 5 件
            self._add_memo(novel.id, f"2026-08-{day}", "did", f"{day} 日の分")

        text = self._open(limit=2)
        self.assertIn("- 2026-08-22 [やった] 小説を書く: 22 日の分", text)
        self.assertIn("- 2026-08-21 [やった] 小説を書く: 21 日の分", text)
        self.assertNotIn("20 日の分", text)
        self.assertIn(
            "さらに 3 件、2026-08-21 より前にあります。"
            "続きは before='2026-08-21' で開けます。",
            text,
        )

        # めくった先には、ちょうど残りが出る (飛ばしも重複もない)。
        nxt = self._open(limit=2, before="2026-08-21")
        self.assertIn("20 日の分", nxt)
        self.assertIn("19 日の分", nxt)
        self.assertNotIn("21 日の分", nxt)
        self.assertIn(
            "さらに 1 件、2026-08-19 より前にあります。"
            "続きは before='2026-08-19' で開けます。",
            nxt,
        )

    def test_page_does_not_split_a_single_date(self):
        """同じ日付のメモはページの切れ目で分かれない (めくる鍵が日付なので)。"""
        novel = self._add_activity("小説を書く")
        for i in range(3):
            self._add_memo(novel.id, "2026-08-22", "did", f"同じ日の {i}")
        self._add_memo(novel.id, "2026-08-21", "did", "前の日の分")

        text = self._open(limit=2)
        for i in range(3):
            self.assertIn(f"同じ日の {i}", text)
        self.assertNotIn("前の日の分", text)
        self.assertIn(
            "さらに 1 件、2026-08-22 より前にあります。"
            "続きは before='2026-08-22' で開けます。",
            text,
        )

    def test_long_memo_text_is_not_truncated(self):
        novel = self._add_activity("小説を書く")
        long_text = "星を拾う話の続きを書いた。" * 40
        self._add_memo(novel.id, "2026-08-22", "did", long_text)
        text = self._open()
        self.assertIn(long_text, text)
        self.assertNotIn("…", text)

    def test_promise_column_lists_open_entries(self):
        due_epoch = int(datetime(2026, 8, 26, 23, 59, 59).timestamp())
        self._add_task("水曜までに挿絵を渡す", counterpart="user", due_at=due_epoch)
        self._add_task("ずっと一緒にいる", counterpart="user")
        text = self._open()
        self.assertIn("- 水曜までに挿絵を渡す (期限 2026-08-26、ユーザー)", text)
        self.assertIn("- ずっと一緒にいる (期限なし、ユーザー)", text)

    def test_closed_promise_is_not_listed(self):
        from saiverse.task_book import complete_entry

        keep = self._add_task("残る約束", counterpart="user")
        done = self._add_task("やり終えた約束", counterpart="user")
        complete_entry(self.manager, "tester", done["task_id"], outcome="返した")
        text = self._open()
        self.assertIn(keep["content"], text)
        self.assertNotIn("やり終えた約束", text)

    def test_missing_tables_are_treated_as_an_empty_book(self):
        with self.adapter._db_lock:
            self.adapter.conn.execute("DROP TABLE IF EXISTS memos")
            self.adapter.conn.execute("DROP TABLE IF EXISTS activities")
            self.adapter.conn.commit()
        text = self._open()
        self.assertIn("手帳にはまだ何も書いていません", text)

    def test_read_failure_is_not_flattened_to_empty(self):
        """表が無い以外の読み取り失敗は失敗のまま — 空の手帳に丸めない。"""
        with patch(
            "sai_memory.memory.pocketbook.list_activities",
            side_effect=RuntimeError("db exploded"),
        ):
            with self.assertRaises(RuntimeError):
                self._open()

    def test_bad_before_argument_is_refused(self):
        text = self._open(before="8/22")
        self.assertIn("before は 'YYYY-MM-DD' の日付で指定してください", text)

    def test_bad_limit_argument_is_refused(self):
        text = self._open(limit=0)
        self.assertIn("limit は 1 以上の整数で指定してください", text)


class PocketbookWriteSpellTest(_PocketbookSpellTestBase):
    def _today(self):
        from saiverse import clock

        return clock.now().date().isoformat()

    def test_want_creates_a_new_activity_and_a_memo(self):
        text = self._write(kind="want", text="星を拾う話を書きたい", activity="小説を書く")
        today = self._today()
        self.assertIn(f"手帳に書きました: {today} [やりたい] 小説を書く: 星を拾う話を書きたい", text)

        activities = self._activities()
        self.assertEqual([a.name for a in activities], ["小説を書く"])
        # 本人が立てたアクティビティは、読み口 UI で「ペルソナが書いた」に映る出自。
        self.assertEqual(activities[0].origin, "sluice")
        rows = self._memo_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], today)
        self.assertEqual(rows[0][2], "want")
        self.assertEqual(rows[0][3], "星を拾う話を書きたい")

    def test_did_appends_to_an_existing_activity(self):
        existing = self._add_activity("絵の練習", origin="user")
        self._write(kind="did", text="クロッキーを30分", activity="絵の練習")
        # 新しいアクティビティは立たない (get-or-create が既存へ収束)。
        self.assertEqual([a.id for a in self._activities()], [existing.id])
        rows = self._memo_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], existing.id)
        self.assertEqual(rows[0][2], "did")

    def test_memo_text_is_not_truncated(self):
        long_text = "星を拾う話の続きを書いた。" * 40
        self._write(kind="did", text=long_text, activity="小説を書く")
        self.assertEqual(self._memo_rows()[0][3], long_text)

    def test_memo_without_activity_is_refused_with_a_hint(self):
        self._add_activity("小説を書く")
        text = self._write(kind="want", text="続きを書きたい")
        self.assertIn("activity で書いてください", text)
        self.assertIn("小説を書く", text)
        self.assertEqual(self._memo_rows(), [])

    def test_unknown_kind_is_refused(self):
        text = self._write(kind="todo", text="何か")
        self.assertIn("kind は want", text)
        self.assertEqual(self._memo_rows(), [])
        self.assertEqual(self._open_tasks(), [])

    def test_empty_text_is_refused(self):
        text = self._write(kind="did", text="   ", activity="小説を書く")
        self.assertIn("書く内容 (text) が空です", text)
        self.assertEqual(self._memo_rows(), [])

    def test_promise_goes_to_the_task_book_with_the_default_counterpart(self):
        text = self._write(kind="promise", text="感想を返す")
        self.assertIn("手帳の約束の欄に書きました: 感想を返す（期限なし、ユーザー）", text)
        tasks = self._open_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["content"], "感想を返す")
        self.assertEqual(tasks[0]["counterpart"], "user")
        self.assertEqual(tasks[0]["origin"], "persona")
        self.assertIsNone(tasks[0]["due_at"])
        # メモ欄には入らない (器の振り分けは種類で決まる)。
        self.assertEqual(self._memo_rows(), [])

    def test_promise_due_is_read_as_a_local_date(self):
        self._write(kind="promise", text="挿絵を渡す", due="2026-08-26")
        tasks = self._open_tasks()
        expected = int(datetime(2026, 8, 26, 23, 59, 59).timestamp())
        self.assertEqual(tasks[0]["due_at"], expected)

    def test_promise_with_neither_due_nor_counterpart_is_refused(self):
        text = self._write(kind="promise", text="いつか小説を書きたい", counterpart="")
        self.assertIn("約束ではなく「やりたいこと」です", text)
        self.assertIn("kind='want'", text)
        self.assertEqual(self._open_tasks(), [])

    def test_unparsable_due_keeps_the_promise_without_a_deadline(self):
        text = self._write(kind="promise", text="挿絵を渡す", due="来週")
        tasks = self._open_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertIsNone(tasks[0]["due_at"])
        self.assertIn("期限なしで書きました", text)

    def test_written_memo_is_readable_by_opening_the_book(self):
        """書いたものが、同じ本人の「開く」で読み返せる (一冊の手帳)。"""
        self._write(kind="want", text="星を拾う話を書きたい", activity="小説を書く")
        self._write(kind="promise", text="感想を返す")
        text = self._open()
        self.assertIn("星を拾う話を書きたい", text)
        self.assertIn("- 感想を返す (期限なし、ユーザー)", text)


if __name__ == "__main__":
    unittest.main()
