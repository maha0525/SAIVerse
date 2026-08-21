"""出来事テーブル (episodes) の**読み取り** API のテスト (saiverse/episodes.py)。

⚠ 書き込み API (``open_episode`` / ``close_episode`` / ``set_digest_ref`` と
session モードの一族) は束 6c (2026-08-22、autonomous_behavior_v3.md §7) で
退役した — 「エピソードという専用の記録行は持たない」の裁定で、旧エピソードが
持っていた情報は遷移の一行・台帳・Chronicle へ返された。それを検証していた
テスト (連番採番 / kind 検証 / 仮想クロック刻印 / close の冪等性 /
set_digest_ref の上書き禁止 / 予約 tx への相乗り / open キャッシュの無効化) は
検証対象の関数ごと消えたのでこのファイルから削除した。

``episodes`` テーブルと既存の行は**読み取り専用の残置**として残る (v3 §9-8 ①)
ので、その旧データを読む口の振る舞いはここで固定し続ける。そのため fixture の
行は ORM (database.models.Episode) で直接挿入する — 旧世代が書き残した行を
読む、という実際の状況をそのまま再現する形。

NOTE: 既存の tests/test_episode_context.py は arasuji (Chronicle) の「エピソード
文脈」で別物。本ファイルは出来事エンベロープ (database/models.py Episode) を扱う。
"""
from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, Episode
from saiverse import episodes as E


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class EpisodesTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        self._short_ids: Dict[str, int] = {}

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    # ---- 旧データの再現 (ORM 直挿し) ----

    def _row(
        self,
        persona_id: str,
        kind: str,
        *,
        started_at: int = 1_000,
        ended_at: Optional[int] = None,
        status: str = E.STATUS_OPEN,
        building_id: Optional[str] = None,
        participants: Optional[List[str]] = None,
        origin_ref: Optional[str] = None,
        occurrence_id: Optional[str] = None,
        digest_ref: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """旧世代が書き残した episodes 行を 1 件作る。

        SHORT_ID はペルソナ内連番 (episode:N 参照子の N)。書き手が退役した今も
        読み手はこの連番で行を選ぶので、fixture 側で同じ規則を再現する。
        """
        short_id = self._short_ids.get(persona_id, 0) + 1
        self._short_ids[persona_id] = short_id
        episode_id = str(uuid.uuid4())
        db = self.SessionLocal()
        try:
            db.add(Episode(
                EPISODE_ID=episode_id,
                PERSONA_ID=persona_id,
                SHORT_ID=short_id,
                KIND=kind,
                OCCURRENCE_ID=occurrence_id,
                STARTED_AT=started_at,
                ENDED_AT=ended_at,
                BUILDING_ID=building_id,
                PARTICIPANTS_JSON=(
                    json.dumps(participants, ensure_ascii=False)
                    if participants is not None else None
                ),
                ORIGIN_REF=origin_ref,
                STATUS=status,
                DIGEST_REF=digest_ref,
                META_JSON=(
                    json.dumps(meta, ensure_ascii=False) if meta is not None else None
                ),
            ))
            db.commit()
        finally:
            db.close()
        return {
            "episode_id": episode_id,
            "short_id": short_id,
            "episode_ref": f"episode:{short_id}",
        }

    def _closed_row(self, persona_id: str, kind: str, **kwargs) -> Dict[str, Any]:
        kwargs.setdefault("ended_at", kwargs.get("started_at", 1_000) + 60)
        return self._row(persona_id, kind, status=E.STATUS_CLOSED, **kwargs)

    # ---- _to_dict: 列 → dict の写し ----

    def test_row_fields_are_deserialized(self):
        row = self._row(
            "p1", E.KIND_WORK_SESSION,
            building_id="atelier",
            participants=["p1", "user_1"],
            origin_ref="task:3",
            occurrence_id="occ-abc",
            meta={"note": "x"},
        )
        ep = E.get_by_ref(self.manager, "p1", row["episode_ref"])
        self.assertEqual(ep["status"], E.STATUS_OPEN)
        self.assertEqual(ep["building_id"], "atelier")
        self.assertEqual(ep["participants"], ["p1", "user_1"])
        self.assertEqual(ep["origin_ref"], "task:3")
        self.assertEqual(ep["occurrence_id"], "occ-abc")
        self.assertEqual(ep["meta"], {"note": "x"})
        self.assertIsNone(ep["ended_at"])

    def test_broken_json_columns_degrade_instead_of_raising(self):
        """壊れた JSON 列は空へ縮退する (旧データを読むだけで落ちない)。"""
        row = self._row("p1", E.KIND_OTHER)
        db = self.SessionLocal()
        try:
            db.query(Episode).filter(
                Episode.EPISODE_ID == row["episode_id"]
            ).update({"PARTICIPANTS_JSON": "{壊れ", "META_JSON": "{壊れ"})
            db.commit()
        finally:
            db.close()
        with self.assertLogs("saiverse.episodes", level="WARNING"):
            ep = E.get_by_ref(self.manager, "p1", row["episode_ref"])
        self.assertEqual(ep["participants"], [])
        self.assertIsNone(ep["meta"])

    # ---- get_by_ref ----

    def test_get_by_ref_short_and_uuid(self):
        row = self._row("p1", E.KIND_OTHER)
        by_short = E.get_by_ref(self.manager, "p1", row["episode_ref"])
        by_uuid = E.get_by_ref(self.manager, "p1", row["episode_id"])
        self.assertEqual(by_short["episode_id"], row["episode_id"])
        self.assertEqual(by_uuid["episode_id"], row["episode_id"])

    def test_get_by_ref_wrong_persona_raises(self):
        row = self._row("p1", E.KIND_OTHER)
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p2", row["episode_ref"])

    def test_get_by_ref_invalid_format_raises(self):
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p1", "not-a-ref")

    def test_get_by_ref_unknown_short_id_raises(self):
        with self.assertRaises(E.EpisodeNotFoundError):
            E.get_by_ref(self.manager, "p1", "episode:999")

    # ---- get_open_episode / get_latest_closed_episode ----

    def test_get_open_episode_returns_last_opened(self):
        """open が複数あれば SHORT_ID 最大 (最後に開いた 1 件) を返す。"""
        self._row("p1", E.KIND_SLOT)
        last = self._row("p1", E.KIND_WORK_SESSION)
        found = E.get_open_episode(self.manager, "p1")
        self.assertIsNotNone(found)
        self.assertEqual(found["episode_id"], last["episode_id"])
        # kind 指定 (非キャッシュ経路) は絞り込みが効く
        by_kind = E.get_open_episode(self.manager, "p1", kind=E.KIND_SLOT)
        self.assertEqual(by_kind["kind"], E.KIND_SLOT)

    def test_get_open_episode_ignores_closed_rows(self):
        self._closed_row("p1", E.KIND_CONVERSATION)
        self.assertIsNone(E.get_open_episode(self.manager, "p1"))

    def test_get_open_non_conversation_excludes_conversation(self):
        """「別の活動中か」は会話を除いた集合から引く (開いた順に依らない)。"""
        work = self._row("p1", E.KIND_WORK_SESSION)
        self._row("p1", E.KIND_CONVERSATION)  # 会話が後から開いても隠れない
        found = E.get_open_non_conversation_episode(self.manager, "p1")
        self.assertIsNotNone(found)
        self.assertEqual(found["episode_id"], work["episode_id"])

    def test_get_latest_closed_episode(self):
        self._closed_row("p1", E.KIND_SLOT, started_at=1_000)
        newest = self._closed_row("p1", E.KIND_WORK_SESSION, started_at=2_000)
        self._row("p1", E.KIND_CONVERSATION)  # open は対象外
        found = E.get_latest_closed_episode(self.manager, "p1")
        self.assertEqual(found["episode_id"], newest["episode_id"])
        by_kind = E.get_latest_closed_episode(self.manager, "p1", kind=E.KIND_SLOT)
        self.assertEqual(by_kind["kind"], E.KIND_SLOT)

    # ---- get_open_episode_by_origin (コマ発火の逆引き) ----

    def test_get_open_episode_by_origin_returns_open_match(self):
        row = self._row(
            "p1", E.KIND_SLOT, origin_ref="day_plan:p1:2026-07-20:0",
        )
        found = E.get_open_episode_by_origin(
            self.manager, "p1", "day_plan:p1:2026-07-20:0",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["episode_id"], row["episode_id"])

    def test_get_open_episode_by_origin_ignores_closed_and_other(self):
        self._closed_row(
            "p1", E.KIND_SLOT, origin_ref="day_plan:p1:2026-07-20:1",
        )
        # 閉じた出来事は返らない
        self.assertIsNone(
            E.get_open_episode_by_origin(self.manager, "p1", "day_plan:p1:2026-07-20:1")
        )
        # 別 persona / 別 origin / 空 origin は None
        self._row("p2", E.KIND_SLOT, origin_ref="day_plan:p2:2026-07-20:0")
        self.assertIsNone(
            E.get_open_episode_by_origin(self.manager, "p1", "day_plan:p2:2026-07-20:0")
        )
        self.assertIsNone(E.get_open_episode_by_origin(self.manager, "p1", ""))

    # ---- list_today ----

    def test_list_today_overlap_semantics(self):
        day_start = _epoch(datetime(2026, 7, 6, 0, 0, 0))
        day_end = _epoch(datetime(2026, 7, 7, 0, 0, 0))

        # (a) 前日に開いて前日に閉じた → 載らない
        self._closed_row(
            "p1", E.KIND_CONVERSATION,
            started_at=_epoch(datetime(2026, 7, 5, 10, 0, 0)),
            ended_at=_epoch(datetime(2026, 7, 5, 11, 0, 0)),
        )
        # (b) 前日に開いてまだ開いている (日跨ぎ) → 載る
        ep_b = self._row(
            "p1", E.KIND_PRESENCE,
            started_at=_epoch(datetime(2026, 7, 5, 23, 0, 0)),
        )
        # (c) 今日開いて今日閉じた → 載る
        ep_c = self._closed_row(
            "p1", E.KIND_SLOT,
            started_at=_epoch(datetime(2026, 7, 6, 9, 0, 0)),
            ended_at=_epoch(datetime(2026, 7, 6, 9, 30, 0)),
        )
        # (d) 他ペルソナの今日の出来事 → 載らない
        self._row(
            "p2", E.KIND_SLOT, started_at=_epoch(datetime(2026, 7, 6, 9, 0, 0)),
        )
        # (e) 翌日に開いた → 載らない
        self._row(
            "p1", E.KIND_STROLL, started_at=_epoch(datetime(2026, 7, 7, 8, 0, 0)),
        )

        today = E.list_today(self.manager, "p1", day_start, day_end)
        refs = [ep["episode_ref"] for ep in today]
        self.assertEqual(refs, [ep_b["episode_ref"], ep_c["episode_ref"]])
        # STARTED_AT 昇順
        self.assertLess(today[0]["started_at"], today[1]["started_at"])


if __name__ == "__main__":
    unittest.main()
