"""クリップ (clip) ストア sai_memory/clips.py のテスト (concept_consolidation.md)。

検証項目:
- init の冪等性 / add・get の基本 CRUD (点クリップ・範囲クリップ)
- 旧 marks テーブルからの一回きり移行 (mark_id→clip_id / harvested_to→pasted_to)
- created_at が saiverse.clock 経由 (仮想クロックの epoch がそのまま刻まれる)
- list_clips のフィルタ (unpasted / since / message_id)
- clip_pasted の来歴刻印 (クリップは消えない — 歴史として残る §5.1)
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

from sai_memory import clips as P
from saiverse import clock


class ClipsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 実運用ではペルソナの memory.db に相乗りする (module docstring 参照)
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        P.init_clips_tables(self.conn)

    def tearDown(self):
        clock.disable_virtual()
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_init_is_idempotent(self):
        P.init_clips_tables(self.conn)  # 2 回目も例外なし
        P.add_clip(self.conn, message_id="m1", quote="ことば")
        P.init_clips_tables(self.conn)  # 既存データを壊さない
        self.assertEqual(len(P.list_clips(self.conn)), 1)

    def test_add_and_get_point_clip(self):
        clip = P.add_clip(
            self.conn,
            message_id="m1",
            quote="この言い回しは後で掘れ",
            purpose_ref="task:2",
            origin_episode_ref="episode:5",
        )
        got = P.get_clip(self.conn, clip.clip_id)
        self.assertEqual(got, clip)
        self.assertEqual(got.purpose_ref, "task:2")
        self.assertEqual(got.origin_episode_ref, "episode:5")
        self.assertIsNone(got.pasted_to)
        self.assertFalse(got.is_range)

    def test_add_and_get_range_clip(self):
        # 範囲クリップ (SCENE 由来参照・Chronicle source_ids の形)。quote は任意
        clip = P.add_clip(
            self.conn, message_id="m1", message_id_end="m9",
        )
        got = P.get_clip(self.conn, clip.clip_id)
        self.assertTrue(got.is_range)
        self.assertEqual(got.message_id, "m1")
        self.assertEqual(got.message_id_end, "m9")
        self.assertIsNone(got.quote)

    def test_range_clip_with_label_quote(self):
        clip = P.add_clip(
            self.conn, message_id="m1", message_id_end="m3", quote="初対面の夜",
        )
        self.assertEqual(clip.quote, "初対面の夜")

    def test_add_with_immediate_paste(self):
        # SCENE のように「撮った瞬間に貼る」用途 (pasted_to を最初から刻む)
        clip = P.add_clip(
            self.conn, message_id="m1", message_id_end="m5", pasted_to="core:3",
        )
        self.assertEqual(clip.pasted_to, "core:3")
        self.assertEqual(P.list_clips(self.conn, unpasted_only=True), [])

    def test_bare_clip_without_purpose_is_legal(self):
        # 素の予約 (型なし・目的接続なし) が合法 (§13-2 「素＝型なし予約」)
        clip = P.add_clip(self.conn, message_id="m1", quote="なんとなく気になる")
        self.assertIsNone(clip.purpose_ref)

    def test_point_clip_requires_message_and_quote(self):
        with self.assertRaises(ValueError):
            P.add_clip(self.conn, message_id="", quote="x")
        with self.assertRaises(ValueError):
            P.add_clip(self.conn, message_id="m1", quote="")
        with self.assertRaises(ValueError):
            P.add_clip(self.conn, message_id="m1")  # 点クリップは quote 必須

    def test_created_at_follows_virtual_clock(self):
        t = datetime(2026, 7, 6, 14, 0, 0)
        clock.enable_virtual(t)
        clip = P.add_clip(self.conn, message_id="m1", quote="仮想時刻の刻印")
        self.assertEqual(clip.created_at, int(t.timestamp()))

    def test_list_filters(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        early = P.add_clip(self.conn, message_id="m1", quote="早い")
        clock.advance_to(datetime(2026, 7, 6, 12, 0, 0))
        cutoff = int(clock.now().timestamp())
        late = P.add_clip(self.conn, message_id="m2", quote="遅い")

        # since フィルタ
        since_late = P.list_clips(self.conn, since=cutoff)
        self.assertEqual([p.clip_id for p in since_late], [late.clip_id])

        # message_id フィルタ
        on_m1 = P.list_clips(self.conn, message_id="m1")
        self.assertEqual([p.clip_id for p in on_m1], [early.clip_id])

        # unpasted フィルタ (貼ると土壌プールから消える)
        P.clip_pasted(self.conn, early.clip_id, "task:7")
        unpasted = P.list_clips(self.conn, unpasted_only=True)
        self.assertEqual([p.clip_id for p in unpasted], [late.clip_id])

        # 全件は created_at 昇順のまま
        all_clips = P.list_clips(self.conn)
        self.assertEqual(
            [p.clip_id for p in all_clips], [early.clip_id, late.clip_id]
        )

    def test_clip_pasted_records_provenance_and_keeps_row(self):
        clip = P.add_clip(self.conn, message_id="m1", quote="収穫対象")
        updated = P.clip_pasted(self.conn, clip.clip_id, "task:9")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.pasted_to, "task:9")
        # 行は消えない (歴史として残る)
        self.assertEqual(len(P.list_clips(self.conn)), 1)

    def test_clip_pasted_unknown_id_returns_none(self):
        self.assertIsNone(P.clip_pasted(self.conn, "no-such-clip", "task:1"))

    def test_clip_pasted_requires_target(self):
        clip = P.add_clip(self.conn, message_id="m1", quote="x")
        with self.assertRaises(ValueError):
            P.clip_pasted(self.conn, clip.clip_id, "")

    # ----- short_id (clip:N、P2b) -----

    def test_add_clip_assigns_sequential_short_ids(self):
        first = P.add_clip(self.conn, message_id="m1", quote="一枚目")
        second = P.add_clip(self.conn, message_id="m2", quote="二枚目")
        self.assertEqual(first.short_id, 1)
        self.assertEqual(second.short_id, 2)
        self.assertEqual(first.ref, "clip:1")

    def test_get_clip_by_short_id(self):
        clip = P.add_clip(self.conn, message_id="m1", quote="探す")
        got = P.get_clip_by_short_id(self.conn, clip.short_id)
        self.assertEqual(got, clip)
        self.assertIsNone(P.get_clip_by_short_id(self.conn, 999))


class LegacyClipsShortIdMigrationTests(unittest.TestCase):
    """short_id 列を持たない既存 clips テーブルへの追加系 migration + backfill。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        # P2a 当時の DDL (short_id 列なし) を再現
        self.conn.execute(
            """
            CREATE TABLE clips (
                clip_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                quote TEXT,
                message_id_end TEXT,
                purpose_ref TEXT,
                created_at INTEGER NOT NULL,
                pasted_to TEXT,
                origin_episode_ref TEXT
            )
            """
        )
        # 挿入順と created_at 順をわざと食い違わせる (採番基準の検証)
        self.conn.execute(
            "INSERT INTO clips VALUES ('new-clip', 'm2', '新しい', NULL, NULL, 200, NULL, NULL)"
        )
        self.conn.execute(
            "INSERT INTO clips VALUES ('old-clip', 'm1', '古い', NULL, NULL, 100, NULL, NULL)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_alter_and_backfill_by_created_at_order(self):
        P.init_clips_tables(self.conn)
        old = P.get_clip(self.conn, "old-clip")
        new = P.get_clip(self.conn, "new-clip")
        # created_at 昇順 (old=100 が new=200 より先) で 1, 2。
        # 挿入順採番なら逆になるはずなので、基準が created_at であることを検証
        self.assertEqual(old.short_id, 1)
        self.assertEqual(new.short_id, 2)
        self.assertEqual(P.get_clip_by_short_id(self.conn, 1).clip_id, "old-clip")

    def test_backfill_runs_once_new_clips_continue_numbering(self):
        P.init_clips_tables(self.conn)
        added = P.add_clip(self.conn, message_id="m3", quote="続き")
        self.assertEqual(added.short_id, 3)
        P.init_clips_tables(self.conn)  # 再 init で再採番しない
        self.assertEqual(P.get_clip(self.conn, "old-clip").short_id, 1)


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

    def test_migrated_marks_get_short_ids(self):
        # marks 移行行にも backfill で clip:N が振られる (created_at 昇順)
        P.init_clips_tables(self.conn)
        clips = P.list_clips(self.conn)
        self.assertEqual([p.short_id for p in clips], [1, 2])

    def test_migrates_rows_and_drops_marks(self):
        P.init_clips_tables(self.conn)
        clips = P.list_clips(self.conn)
        self.assertEqual([p.clip_id for p in clips], ["id1", "id2"])
        # 列の写像: mark_id→clip_id / harvested_to→pasted_to / 移行行は点クリップ
        first, second = clips
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
        P.init_clips_tables(self.conn)
        P.add_clip(self.conn, message_id="m3", quote="新規")
        P.init_clips_tables(self.conn)  # 再 init でも重複移行しない
        self.assertEqual(len(P.list_clips(self.conn)), 3)


class LegacyPhotosTableMigrationTest(unittest.TestCase):
    """旧 ``photos`` テーブル (2026-07-10〜07-15) → ``clips`` への改名移行。

    データを動かさない ALTER RENAME なので、行がそのまま残ることを確かめる。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        gc.collect()
        self._tmpdir.cleanup()

    def _make_legacy_photos_table(self):
        """改名前 (photos 世代) の DB を作る。"""
        self.conn.execute(
            """
            CREATE TABLE photos (
                photo_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                quote TEXT,
                message_id_end TEXT,
                purpose_ref TEXT,
                created_at INTEGER NOT NULL,
                pasted_to TEXT,
                origin_episode_ref TEXT,
                short_id INTEGER
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX idx_photos_pasted ON photos(pasted_to)"
        )
        self.conn.executemany(
            "INSERT INTO photos (photo_id, message_id, quote, created_at, pasted_to, short_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("p-1", "msg-1", "引用1", 1000, "m:5", 1),
                ("p-2", "msg-2", "引用2", 2000, None, 2),
            ],
        )
        self.conn.commit()

    def test_photos_table_is_renamed_and_rows_survive(self):
        self._make_legacy_photos_table()
        P.init_clips_tables(self.conn)

        clips = P.list_clips(self.conn)
        self.assertEqual([c.clip_id for c in clips], ["p-1", "p-2"])
        self.assertEqual(clips[0].quote, "引用1")
        self.assertEqual(clips[0].short_id, 1)
        # 旧テーブルは残らない (旧 path を残さない)
        tables = {
            r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("clips", tables)
        self.assertNotIn("photos", tables)

    def test_legacy_pasted_to_is_normalized_to_canonical(self):
        # 旧表記 (m:5) のまま残るとページに貼ったクリップが見えなくなる
        self._make_legacy_photos_table()
        P.init_clips_tables(self.conn)

        self.assertEqual(P.list_clips(self.conn)[0].pasted_to, "memopedia:5")
        self.assertEqual(len(P.list_clips_pasted_to(self.conn, "memopedia:5")), 1)

    def test_rename_is_idempotent(self):
        self._make_legacy_photos_table()
        P.init_clips_tables(self.conn)
        P.init_clips_tables(self.conn)  # 2 回目は no-op
        self.assertEqual(len(P.list_clips(self.conn)), 2)
        self.assertEqual(P.list_clips(self.conn)[0].pasted_to, "memopedia:5")


if __name__ == "__main__":
    unittest.main()
