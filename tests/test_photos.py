"""写真 (photo) ストア sai_memory/photos.py のテスト (concept_consolidation.md)。

検証項目:
- init の冪等性 / add・get の基本 CRUD (点写真・範囲写真)
- 旧 marks テーブルからの一回きり移行 (mark_id→photo_id / harvested_to→pasted_to)
- created_at が saiverse.clock 経由 (仮想クロックの epoch がそのまま刻まれる)
- list_photos のフィルタ (unpasted / since / message_id)
- photo_pasted の来歴刻印 (写真は消えない — 歴史として残る §5.1)
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

from sai_memory import photos as P
from saiverse import clock


class PhotosTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 実運用ではペルソナの memory.db に相乗りする (module docstring 参照)
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        P.init_photos_tables(self.conn)

    def tearDown(self):
        clock.disable_virtual()
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_init_is_idempotent(self):
        P.init_photos_tables(self.conn)  # 2 回目も例外なし
        P.add_photo(self.conn, message_id="m1", quote="ことば")
        P.init_photos_tables(self.conn)  # 既存データを壊さない
        self.assertEqual(len(P.list_photos(self.conn)), 1)

    def test_add_and_get_point_photo(self):
        photo = P.add_photo(
            self.conn,
            message_id="m1",
            quote="この言い回しは後で掘れ",
            purpose_ref="task:2",
            origin_episode_ref="episode:5",
        )
        got = P.get_photo(self.conn, photo.photo_id)
        self.assertEqual(got, photo)
        self.assertEqual(got.purpose_ref, "task:2")
        self.assertEqual(got.origin_episode_ref, "episode:5")
        self.assertIsNone(got.pasted_to)
        self.assertFalse(got.is_range)

    def test_add_and_get_range_photo(self):
        # 範囲写真 (SCENE 由来参照・Chronicle source_ids の形)。quote は任意
        photo = P.add_photo(
            self.conn, message_id="m1", message_id_end="m9",
        )
        got = P.get_photo(self.conn, photo.photo_id)
        self.assertTrue(got.is_range)
        self.assertEqual(got.message_id, "m1")
        self.assertEqual(got.message_id_end, "m9")
        self.assertIsNone(got.quote)

    def test_range_photo_with_label_quote(self):
        photo = P.add_photo(
            self.conn, message_id="m1", message_id_end="m3", quote="初対面の夜",
        )
        self.assertEqual(photo.quote, "初対面の夜")

    def test_add_with_immediate_paste(self):
        # SCENE のように「撮った瞬間に貼る」用途 (pasted_to を最初から刻む)
        photo = P.add_photo(
            self.conn, message_id="m1", message_id_end="m5", pasted_to="c:3",
        )
        self.assertEqual(photo.pasted_to, "c:3")
        self.assertEqual(P.list_photos(self.conn, unpasted_only=True), [])

    def test_bare_photo_without_purpose_is_legal(self):
        # 素の予約 (型なし・目的接続なし) が合法 (§13-2 「素＝型なし予約」)
        photo = P.add_photo(self.conn, message_id="m1", quote="なんとなく気になる")
        self.assertIsNone(photo.purpose_ref)

    def test_point_photo_requires_message_and_quote(self):
        with self.assertRaises(ValueError):
            P.add_photo(self.conn, message_id="", quote="x")
        with self.assertRaises(ValueError):
            P.add_photo(self.conn, message_id="m1", quote="")
        with self.assertRaises(ValueError):
            P.add_photo(self.conn, message_id="m1")  # 点写真は quote 必須

    def test_created_at_follows_virtual_clock(self):
        t = datetime(2026, 7, 6, 14, 0, 0)
        clock.enable_virtual(t)
        photo = P.add_photo(self.conn, message_id="m1", quote="仮想時刻の刻印")
        self.assertEqual(photo.created_at, int(t.timestamp()))

    def test_list_filters(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        early = P.add_photo(self.conn, message_id="m1", quote="早い")
        clock.advance_to(datetime(2026, 7, 6, 12, 0, 0))
        cutoff = int(clock.now().timestamp())
        late = P.add_photo(self.conn, message_id="m2", quote="遅い")

        # since フィルタ
        since_late = P.list_photos(self.conn, since=cutoff)
        self.assertEqual([p.photo_id for p in since_late], [late.photo_id])

        # message_id フィルタ
        on_m1 = P.list_photos(self.conn, message_id="m1")
        self.assertEqual([p.photo_id for p in on_m1], [early.photo_id])

        # unpasted フィルタ (貼ると土壌プールから消える)
        P.photo_pasted(self.conn, early.photo_id, "task:7")
        unpasted = P.list_photos(self.conn, unpasted_only=True)
        self.assertEqual([p.photo_id for p in unpasted], [late.photo_id])

        # 全件は created_at 昇順のまま
        all_photos = P.list_photos(self.conn)
        self.assertEqual(
            [p.photo_id for p in all_photos], [early.photo_id, late.photo_id]
        )

    def test_photo_pasted_records_provenance_and_keeps_row(self):
        photo = P.add_photo(self.conn, message_id="m1", quote="収穫対象")
        updated = P.photo_pasted(self.conn, photo.photo_id, "task:9")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.pasted_to, "task:9")
        # 行は消えない (歴史として残る)
        self.assertEqual(len(P.list_photos(self.conn)), 1)

    def test_photo_pasted_unknown_id_returns_none(self):
        self.assertIsNone(P.photo_pasted(self.conn, "no-such-photo", "task:1"))

    def test_photo_pasted_requires_target(self):
        photo = P.add_photo(self.conn, message_id="m1", quote="x")
        with self.assertRaises(ValueError):
            P.photo_pasted(self.conn, photo.photo_id, "")


class LegacyMarksMigrationTests(unittest.TestCase):
    """旧 marks テーブルが存在する DB での一回きり移行。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        # 旧スキーマ (sai_memory/marks.py 当時の DDL) を再現
        self.conn.execute(
            """
            CREATE TABLE marks (
                mark_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                quote TEXT NOT NULL,
                purpose_ref TEXT,
                created_at INTEGER NOT NULL,
                harvested_to TEXT,
                origin_episode_ref TEXT
            )
            """
        )
        self.conn.execute(
            "INSERT INTO marks VALUES ('id1', 'm1', '早い', 'task:1', 100, NULL, NULL)"
        )
        self.conn.execute(
            "INSERT INTO marks VALUES ('id2', 'm2', '遅い', NULL, 200, 'task:3', 'episode:1')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_migrates_rows_and_drops_marks(self):
        P.init_photos_tables(self.conn)
        photos = P.list_photos(self.conn)
        self.assertEqual([p.photo_id for p in photos], ["id1", "id2"])
        # 列の写像: mark_id→photo_id / harvested_to→pasted_to / 移行行は点写真
        first, second = photos
        self.assertEqual(first.quote, "早い")
        self.assertIsNone(first.message_id_end)
        self.assertIsNone(first.pasted_to)
        self.assertEqual(second.pasted_to, "task:3")
        self.assertEqual(second.origin_episode_ref, "episode:1")
        # 旧テーブルは落ちている (旧 path を残さない)
        cur = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='marks'"
        )
        self.assertIsNone(cur.fetchone())

    def test_migration_runs_only_once(self):
        P.init_photos_tables(self.conn)
        P.add_photo(self.conn, message_id="m3", quote="新規")
        P.init_photos_tables(self.conn)  # 再 init でも重複移行しない
        self.assertEqual(len(P.list_photos(self.conn)), 3)


if __name__ == "__main__":
    unittest.main()
