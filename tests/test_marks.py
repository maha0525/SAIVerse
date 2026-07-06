"""Mark (観測点) ストア sai_memory/marks.py のテスト (life_concept_map.md §9.1 層 1)。

検証項目:
- init の冪等性 / add・get の基本 CRUD
- created_at が saiverse.clock 経由 (仮想クロックの epoch がそのまま刻まれる)
- list_marks のフィルタ (unharvested / since / message_id)
- mark_harvested の来歴刻印 (mark は消えない — 歴史として残る §5.1)
"""
from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sai_memory import marks as M
from saiverse import clock


class MarksTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 実運用ではペルソナの memory.db に相乗りする (module docstring 参照)
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        M.init_marks_tables(self.conn)

    def tearDown(self):
        clock.disable_virtual()
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_init_is_idempotent(self):
        M.init_marks_tables(self.conn)  # 2 回目も例外なし
        M.add_mark(self.conn, message_id="m1", quote="ことば")
        M.init_marks_tables(self.conn)  # 既存データを壊さない
        self.assertEqual(len(M.list_marks(self.conn)), 1)

    def test_add_and_get(self):
        mark = M.add_mark(
            self.conn,
            message_id="m1",
            quote="この言い回しは後で掘れ",
            purpose_ref="task:2",
            origin_episode_ref="episode:5",
        )
        got = M.get_mark(self.conn, mark.mark_id)
        self.assertEqual(got, mark)
        self.assertEqual(got.purpose_ref, "task:2")
        self.assertEqual(got.origin_episode_ref, "episode:5")
        self.assertIsNone(got.harvested_to)

    def test_bare_mark_without_purpose_is_legal(self):
        # 素の予約 (型なし・目的接続なし) が合法 (§13-2 「素＝型なし予約」)
        mark = M.add_mark(self.conn, message_id="m1", quote="なんとなく気になる")
        self.assertIsNone(mark.purpose_ref)

    def test_add_requires_message_and_quote(self):
        with self.assertRaises(ValueError):
            M.add_mark(self.conn, message_id="", quote="x")
        with self.assertRaises(ValueError):
            M.add_mark(self.conn, message_id="m1", quote="")

    def test_created_at_follows_virtual_clock(self):
        t = datetime(2026, 7, 6, 14, 0, 0)
        clock.enable_virtual(t)
        mark = M.add_mark(self.conn, message_id="m1", quote="仮想時刻の刻印")
        self.assertEqual(mark.created_at, int(t.timestamp()))

    def test_list_filters(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        early = M.add_mark(self.conn, message_id="m1", quote="早い")
        clock.advance_to(datetime(2026, 7, 6, 12, 0, 0))
        cutoff = int(clock.now().timestamp())
        late = M.add_mark(self.conn, message_id="m2", quote="遅い")

        # since フィルタ
        since_late = M.list_marks(self.conn, since=cutoff)
        self.assertEqual([m.mark_id for m in since_late], [late.mark_id])

        # message_id フィルタ
        on_m1 = M.list_marks(self.conn, message_id="m1")
        self.assertEqual([m.mark_id for m in on_m1], [early.mark_id])

        # unharvested フィルタ (収穫すると消える)
        M.mark_harvested(self.conn, early.mark_id, "task:7")
        unharvested = M.list_marks(self.conn, unharvested_only=True)
        self.assertEqual([m.mark_id for m in unharvested], [late.mark_id])

        # 全件は created_at 昇順のまま
        all_marks = M.list_marks(self.conn)
        self.assertEqual(
            [m.mark_id for m in all_marks], [early.mark_id, late.mark_id]
        )

    def test_mark_harvested_records_provenance_and_keeps_row(self):
        mark = M.add_mark(self.conn, message_id="m1", quote="収穫対象")
        updated = M.mark_harvested(self.conn, mark.mark_id, "task:9")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.harvested_to, "task:9")
        # 行は消えない (歴史として残る)
        self.assertEqual(len(M.list_marks(self.conn)), 1)

    def test_mark_harvested_unknown_id_returns_none(self):
        self.assertIsNone(M.mark_harvested(self.conn, "no-such-mark", "task:1"))

    def test_mark_harvested_requires_target(self):
        mark = M.add_mark(self.conn, message_id="m1", quote="x")
        with self.assertRaises(ValueError):
            M.mark_harvested(self.conn, mark.mark_id, "")


if __name__ == "__main__":
    unittest.main()
