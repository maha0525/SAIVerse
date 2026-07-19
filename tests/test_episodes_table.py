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

    # ---- get_open_episode_by_origin (W2 D5) ----

    def test_get_open_episode_by_origin_returns_open_match(self):
        ep = E.open_episode(
            self.manager, "p1", E.KIND_SLOT, origin_ref="day_plan:p1:2026-07-20:0",
        )
        found = E.get_open_episode_by_origin(
            self.manager, "p1", "day_plan:p1:2026-07-20:0",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["episode_id"], ep["episode_id"])

    def test_get_open_episode_by_origin_ignores_closed_and_other(self):
        ep = E.open_episode(
            self.manager, "p1", E.KIND_SLOT, origin_ref="day_plan:p1:2026-07-20:1",
        )
        E.close_episode(self.manager, "p1", ep["episode_ref"])
        # 閉じた出来事は返らない
        self.assertIsNone(
            E.get_open_episode_by_origin(self.manager, "p1", "day_plan:p1:2026-07-20:1")
        )
        # 別 persona / 別 origin / 空 origin は None
        E.open_episode(
            self.manager, "p2", E.KIND_SLOT, origin_ref="day_plan:p2:2026-07-20:0",
        )
        self.assertIsNone(
            E.get_open_episode_by_origin(self.manager, "p1", "day_plan:p2:2026-07-20:0")
        )
        self.assertIsNone(E.get_open_episode_by_origin(self.manager, "p1", ""))

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

    # ---- set_digest_ref (W1 Chunk C / D9-5: 再訪の鍵の後段確定) ----

    def test_set_digest_ref_fills_null(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        E.close_episode(self.manager, "p1", ep["episode_ref"])
        updated = E.set_digest_ref(
            self.manager, "p1", ep["episode_ref"], "message:abc",
        )
        self.assertEqual(updated["digest_ref"], "message:abc")
        self.assertEqual(
            E.get_by_ref(self.manager, "p1", ep["episode_ref"])["digest_ref"],
            "message:abc",
        )

    def test_set_digest_ref_same_value_is_noop(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        E.close_episode(self.manager, "p1", ep["episode_ref"])
        E.set_digest_ref(self.manager, "p1", ep["episode_ref"], "message:abc")
        updated = E.set_digest_ref(
            self.manager, "p1", ep["episode_ref"], "message:abc",
        )
        self.assertEqual(updated["digest_ref"], "message:abc")

    def test_set_digest_ref_does_not_overwrite_different_value(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        E.close_episode(
            self.manager, "p1", ep["episode_ref"], digest_ref="message:first",
        )
        with self.assertLogs("saiverse.episodes", level="WARNING") as logs:
            updated = E.set_digest_ref(
                self.manager, "p1", ep["episode_ref"], "message:second",
            )
        self.assertEqual(updated["digest_ref"], "message:first")  # 上書きしない
        self.assertTrue(any("not overwriting" in m for m in logs.output))

    def test_set_digest_ref_unknown_episode_raises(self):
        with self.assertRaises(E.EpisodeNotFoundError):
            E.set_digest_ref(self.manager, "p1", "episode:99", "message:abc")

    def test_set_digest_ref_requires_value(self):
        ep = E.open_episode(self.manager, "p1", E.KIND_WORK_SESSION)
        with self.assertRaises(ValueError):
            E.set_digest_ref(self.manager, "p1", ep["episode_ref"], "")

    # ---- session モード (W2 Chunk A / D3: 予約 tx / 精算 tx への同梱) ----

    def test_open_session_mode_not_visible_before_commit(self):
        db = self.manager.SessionLocal()
        try:
            ep = E.open_episode(self.manager, "p1", E.KIND_SLOT, session=db)
            # 呼び出し元 commit 前は別 Session から見えない
            with self.assertRaises(E.EpisodeNotFoundError):
                E.get_by_ref(self.manager, "p1", ep["episode_ref"])
            db.commit()
        finally:
            db.close()
        # commit 後は見える
        fetched = E.get_by_ref(self.manager, "p1", ep["episode_ref"])
        self.assertEqual(fetched["episode_id"], ep["episode_id"])
        self.assertEqual(fetched["status"], E.STATUS_OPEN)
        self.assertEqual(ep["short_id"], 1)

    def test_open_session_mode_rollback_leaves_nothing(self):
        db = self.manager.SessionLocal()
        try:
            ep = E.open_episode(self.manager, "p1", E.KIND_SLOT, session=db)
            db.rollback()
        finally:
            db.close()
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p1", ep["episode_ref"])

    def test_close_session_mode_not_visible_before_commit(self):
        # まず通常経路で open (commit 済み)
        ep = E.open_episode(self.manager, "p1", E.KIND_SLOT)
        db = self.manager.SessionLocal()
        try:
            E.close_episode(self.manager, "p1", ep["episode_ref"], session=db)
            # commit 前: 別 Session からはまだ open
            self.assertEqual(
                E.get_by_ref(self.manager, "p1", ep["episode_ref"])["status"],
                E.STATUS_OPEN,
            )
            db.commit()
        finally:
            db.close()
        # commit 後: closed
        self.assertEqual(
            E.get_by_ref(self.manager, "p1", ep["episode_ref"])["status"],
            E.STATUS_CLOSED,
        )

    def test_session_mode_does_not_mutate_open_cache_until_invalidated(self):
        # キャッシュ整合の契約: session モードはキャッシュを触らず、呼び出し元が
        # commit 後に invalidate_open_cache() を呼ぶまで stale hit が残る。
        # まずキャッシュに None を焼く (open が無い状態を読む)
        self.assertIsNone(E.get_open_episode(self.manager, "p1"))

        db = self.manager.SessionLocal()
        try:
            ep = E.open_episode(self.manager, "p1", E.KIND_SLOT, session=db)
            db.commit()
        finally:
            db.close()
        # 未 invalidate: stale な None が cache hit で返り続ける (DB フォールバック
        # しないことの確認)
        self.assertIsNone(E.get_open_episode(self.manager, "p1"))
        # invalidate 後: DB から現在の open を読み直す
        E.invalidate_open_cache(self.manager, "p1")
        restored = E.get_open_episode(self.manager, "p1")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["episode_id"], ep["episode_id"])

    def test_session_none_path_updates_cache_as_before(self):
        # session=None の従来経路はキャッシュを更新する (無傷であること)
        opened = E.open_episode(self.manager, "p1", E.KIND_CONVERSATION)
        # open 直後、キャッシュ経由で読める (DB を再度引かずとも最後の open)
        cached = E.get_open_episode(self.manager, "p1")
        self.assertEqual(cached["episode_id"], opened["episode_id"])
        # close (session=None) はキャッシュを落とす → 次回 DB フォールバックで None
        E.close_episode(self.manager, "p1", opened["episode_ref"])
        self.assertIsNone(E.get_open_episode(self.manager, "p1"))


if __name__ == "__main__":
    unittest.main()
