"""出来事テーブル (episodes) と saiverse/episodes.py のテスト (life_concept_map.md §8.1)。

検証項目:
- open/close の CRUD と episode:N 参照 (short_id はペルソナ内連番)
- 時刻刻印が saiverse.clock 経由であること (仮想クロックの時刻がそのまま刻まれる)
- list_today の重なり判定 (開きっぱなし・日跨ぎ・前日に閉じた出来事)
- kind 検証 / close の冪等性 / origin_ref なし (= 自発) が合法であること

NOTE: 既存の tests/test_episode_context.py は arasuji (Chronicle) の「エピソード
文脈」で別物。本ファイルは出来事エンベロープ (database/models.py Episode) を扱う。
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base
from saiverse import clock
from saiverse import episodes as E


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class EpisodesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.manager = SimpleNamespace(SessionLocal=sessionmaker(bind=self.engine))

    def tearDown(self):
        clock.disable_virtual()
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    # ---- open / short_id ----

    def test_open_assigns_sequential_short_ids_per_persona(self):
        e1 = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        e2 = E.open_episode(self.manager, "p1", E.KIND_SLOT)
        e3 = E.open_episode(self.manager, "p2", E.KIND_PRESENCE)
        self.assertEqual(e1["short_id"], 1)
        self.assertEqual(e2["short_id"], 2)
        self.assertEqual(e3["short_id"], 1)  # per-persona
        self.assertEqual(e2["episode_ref"], "episode:2")

    def test_open_records_fields(self):
        ep = E.open_episode(
            self.manager, "p1", E.KIND_WORK_SESSION,
            building_id="atelier",
            participants=["p1", "user_1"],
            origin_ref="task:3",
            occurrence_id="occ-abc",
            meta={"note": "x"},
        )
        self.assertEqual(ep["status"], E.STATUS_OPEN)
        self.assertEqual(ep["building_id"], "atelier")
        self.assertEqual(ep["participants"], ["p1", "user_1"])
        self.assertEqual(ep["origin_ref"], "task:3")
        self.assertEqual(ep["occurrence_id"], "occ-abc")
        self.assertEqual(ep["meta"], {"note": "x"})
        self.assertIsNone(ep["ended_at"])

    def test_open_without_origin_is_legal(self):
        # 無計画の出来事は出自なしが合法 (§8.1 — 予定に偽のコマを起こさない)
        ep = E.open_episode(self.manager, "p1", E.KIND_STROLL)
        self.assertIsNone(ep["origin_ref"])

    def test_open_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            E.open_episode(self.manager, "p1", "party")

    # ---- 仮想クロック経由の時刻刻印 ----

    def test_timestamps_follow_virtual_clock(self):
        start = datetime(2026, 7, 6, 9, 0, 0)
        clock.enable_virtual(start)
        ep = E.open_episode(self.manager, "p1", E.KIND_SLOT)
        self.assertEqual(ep["started_at"], _epoch(start))

        later = datetime(2026, 7, 6, 10, 30, 0)
        clock.advance_to(later)
        closed = E.close_episode(
            self.manager, "p1", ep["episode_ref"], digest_ref="chronicle:xyz"
        )
        self.assertEqual(closed["ended_at"], _epoch(later))
        self.assertEqual(closed["status"], E.STATUS_CLOSED)
        self.assertEqual(closed["digest_ref"], "chronicle:xyz")

    # ---- close ----

    def test_close_is_idempotent(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        first = E.close_episode(self.manager, "p1", ep["episode_ref"])
        second = E.close_episode(self.manager, "p1", ep["episode_ref"])
        self.assertEqual(first["ended_at"], second["ended_at"])
        self.assertEqual(second["status"], E.STATUS_CLOSED)

    def test_close_unknown_ref_raises(self):
        with self.assertRaises(E.EpisodeNotFoundError):
            E.close_episode(self.manager, "p1", "episode:999")

    # ---- get_by_ref ----

    def test_get_by_ref_short_and_uuid(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_OTHER)
        by_short = E.get_by_ref(self.manager, "p1", ep["episode_ref"])
        by_uuid = E.get_by_ref(self.manager, "p1", ep["episode_id"])
        self.assertEqual(by_short["episode_id"], ep["episode_id"])
        self.assertEqual(by_uuid["episode_id"], ep["episode_id"])

    def test_get_by_ref_wrong_persona_raises(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_OTHER)
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p2", ep["episode_ref"])

    def test_get_by_ref_invalid_format_raises(self):
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p1", "not-a-ref")

    # ---- list_today ----

    def test_list_today_overlap_semantics(self):
        day_start = datetime(2026, 7, 6, 0, 0, 0)
        day_end = datetime(2026, 7, 7, 0, 0, 0)

        # (a) 前日に開いて前日に閉じた → 載らない
        clock.enable_virtual(datetime(2026, 7, 5, 10, 0, 0))
        ep_a = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        clock.advance_to(datetime(2026, 7, 5, 11, 0, 0))
        E.close_episode(self.manager, "p1", ep_a["episode_ref"])

        # (b) 前日に開いてまだ開いている (日跨ぎ) → 載る
        clock.advance_to(datetime(2026, 7, 5, 23, 0, 0))
        ep_b = E.open_episode(self.manager, "p1", E.KIND_PRESENCE)

        # (c) 今日開いて今日閉じた → 載る
        clock.advance_to(datetime(2026, 7, 6, 9, 0, 0))
        ep_c = E.open_episode(self.manager, "p1", E.KIND_SLOT)
        clock.advance_to(datetime(2026, 7, 6, 9, 30, 0))
        E.close_episode(self.manager, "p1", ep_c["episode_ref"])

        # (d) 他ペルソナの今日の出来事 → 載らない
        E.open_episode(self.manager, "p2", E.KIND_SLOT)

        # (e) 翌日に開いた → 載らない
        clock.advance_to(datetime(2026, 7, 7, 8, 0, 0))
        E.open_episode(self.manager, "p1", E.KIND_STROLL)

        today = E.list_today(self.manager, "p1", _epoch(day_start), _epoch(day_end))
        refs = [ep["episode_ref"] for ep in today]
        self.assertEqual(refs, [ep_b["episode_ref"], ep_c["episode_ref"]])
        # STARTED_AT 昇順
        self.assertLess(today[0]["started_at"], today[1]["started_at"])


if __name__ == "__main__":
    unittest.main()
