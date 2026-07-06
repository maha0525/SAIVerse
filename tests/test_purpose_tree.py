"""目的ノードの段階 (stage) と統一ビュー saiverse/purpose_tree.py のテスト。

life_concept_map.md §3.1 (種・段階・位置) / §10.1 (変えるもの/変えないもの) の
P1 実装分:

- derive_stage の後方互換既定規則 (既存 desire 行 → candidate / 既存 task 行 →
  adopted / 期限切れ desire → dormant / cancelled → aborted / completed)
- purpose_tree の読み出し (list_first_tier / resolve_ref / list_children) と
  書き込み (create_candidate / adopt / name_theme)
- 既存経路 (desire_engine / task) の挙動を変えないこと = 物理 stage 列は
  既存経路の書き込みでは NULL のまま
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base, PersonaTask
from saiverse import purpose_tree as PT
from saiverse.persona_task_manager import (
    DESIRE_STATE_EXPIRED,
    PersonaTaskManager,
    STAGE_ADOPTED,
    STAGE_CANDIDATE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    derive_stage,
)
from saiverse.track_manager import STATUS_RUNNING, TrackManager


class DeriveStageTests(unittest.TestCase):
    """後方互換の既定規則 (stage カラム NULL の既存行の読み方)。"""

    def test_live_desire_row_reads_candidate(self):
        self.assertEqual(derive_stage("pending", "note", "fresh"), STAGE_CANDIDATE)
        self.assertEqual(derive_stage("pending", "note", None), STAGE_CANDIDATE)

    def test_live_task_rows_read_adopted(self):
        self.assertEqual(derive_stage("pending", None, None), STAGE_ADOPTED)
        self.assertEqual(derive_stage("active", "track", None), STAGE_ADOPTED)
        self.assertEqual(derive_stage("paused", "track", None), STAGE_ADOPTED)

    def test_completed_reads_completed(self):
        self.assertEqual(derive_stage("completed", "note", None), "completed")
        self.assertEqual(derive_stage("completed", None, None), "completed")

    def test_expired_desire_reads_dormant(self):
        # desire_engine の論理アーカイブ (cancelled + expired) = 休眠 (§5)
        self.assertEqual(
            derive_stage("cancelled", "note", DESIRE_STATE_EXPIRED), "dormant"
        )

    def test_other_cancelled_reads_aborted(self):
        self.assertEqual(derive_stage("cancelled", None, None), "aborted")
        self.assertEqual(derive_stage("cancelled", "track", None), "aborted")
        # 期限切れマーカーなしで消された desire = 明示中止
        self.assertEqual(derive_stage("cancelled", "note", "fresh"), "aborted")


class _DbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.manager = SimpleNamespace(SessionLocal=self.SessionLocal)
        self.ptm = PersonaTaskManager(self.SessionLocal)
        self.tm = TrackManager(session_factory=self.SessionLocal)

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass


class StageBackwardCompatDbTests(_DbTestCase):
    """既存経路で書かれた行が既定規則で正しく読めること (DB 経由)。"""

    def test_existing_desire_path_reads_candidate_without_writing_stage(self):
        # 既存の desire_add 相当の経路 (parent_kind='note')
        task = self.ptm.create_task(
            persona_id="p1", title="星を見たい",
            parent_kind="note", note_id="note-1",
            auto_activate=False, desire_source="散歩の記憶",
        )
        self.assertEqual(task["stage"], STAGE_CANDIDATE)
        # 物理カラムは NULL のまま (= 既存経路の挙動不変)
        db = self.SessionLocal()
        try:
            row = db.query(PersonaTask).filter_by(id=task["id"]).first()
            self.assertIsNone(row.stage)
        finally:
            db.close()

    def test_existing_task_path_reads_adopted(self):
        task = self.ptm.create_task(
            persona_id="p1", title="資料を読む", auto_activate=False,
        )
        self.assertEqual(task["stage"], STAGE_ADOPTED)

    def test_expired_desire_reads_dormant(self):
        task = self.ptm.create_task(
            persona_id="p1", title="消える欲求",
            parent_kind="note", note_id="note-1", auto_activate=False,
        )
        # desire_engine.decay_desires の論理アーカイブと同じ書き込み
        self.ptm.update_task_status(
            task["id"], status=STATUS_CANCELLED, actor="test", persona_id="p1",
        )
        db = self.SessionLocal()
        try:
            row = db.query(PersonaTask).filter_by(id=task["id"]).first()
            row.desire_state = DESIRE_STATE_EXPIRED
            db.commit()
        finally:
            db.close()
        got = self.ptm.get_task(task["id"], persona_id="p1")
        self.assertEqual(got["stage"], "dormant")

    def test_completed_task_reads_completed(self):
        task = self.ptm.create_task(persona_id="p1", title="done", auto_activate=False)
        self.ptm.update_task_status(
            task["id"], status=STATUS_COMPLETED, actor="test", persona_id="p1",
        )
        got = self.ptm.get_task(task["id"], persona_id="p1")
        self.assertEqual(got["stage"], "completed")


class PurposeTreeTests(_DbTestCase):
    # ---- create_candidate ----

    def test_create_candidate_requires_source_ref(self):
        with self.assertRaises(ValueError):
            PT.create_candidate(self.manager, "p1", "接地なし", "")

    def test_create_candidate_is_outside_tree(self):
        node = PT.create_candidate(self.manager, "p1", "詩を書きたい", "message:abc")
        self.assertEqual(node["node_kind"], "task")
        self.assertEqual(node["stage"], STAGE_CANDIDATE)
        self.assertEqual(node["source_ref"], "message:abc")
        # 候補は第一階層に載らない (§3.1: 段階の区別)
        self.assertEqual(PT.list_first_tier(self.manager, "p1"), [])

    # ---- adopt ----

    def test_adopt_without_parent_stands_small_at_first_tier(self):
        node = PT.create_candidate(self.manager, "p1", "詩を書きたい", "message:abc")
        adopted = PT.adopt(self.manager, "p1", node["ref"])
        self.assertEqual(adopted["stage"], STAGE_ADOPTED)
        first = PT.list_first_tier(self.manager, "p1")
        self.assertEqual([n["ref"] for n in first], [adopted["ref"]])

    def test_adopt_legacy_note_bound_desire_detaches_note(self):
        # 旧 desire 実装 (parent_kind='note') → 採用で正規化 (§10.1)
        task = self.ptm.create_task(
            persona_id="p1", title="古い欲求",
            parent_kind="note", note_id="note-1",
            auto_activate=False,
        )
        adopted = PT.adopt(self.manager, "p1", task["task_ref"])
        self.assertEqual(adopted["stage"], STAGE_ADOPTED)
        self.assertIsNone(adopted["parent_kind"])
        self.assertIsNone(adopted["note_id"])

    def test_adopt_under_track_parent(self):
        track_id = self.tm.create("p1", "autonomous", title="言葉集め")
        node = PT.create_candidate(self.manager, "p1", "語彙帳を作る", "mark-quote")
        adopted = PT.adopt(self.manager, "p1", node["ref"], parent_ref="track:1")
        self.assertEqual(adopted["parent_kind"], "track")
        self.assertEqual(adopted["track_id"], track_id)
        # 木の親子として辿れる
        children = PT.list_children(self.manager, "p1", "track:1")
        self.assertEqual([c["id"] for c in children], [adopted["id"]])

    def test_adopt_rejects_non_candidate(self):
        task = self.ptm.create_task(persona_id="p1", title="普通のタスク", auto_activate=False)
        with self.assertRaises(PT.PurposeTreeError):
            PT.adopt(self.manager, "p1", task["task_ref"])

    def test_adopt_rejects_task_parent(self):
        parent = self.ptm.create_task(persona_id="p1", title="親タスク", auto_activate=False)
        node = PT.create_candidate(self.manager, "p1", "子候補", "src")
        with self.assertRaises(ValueError):
            PT.adopt(self.manager, "p1", node["ref"], parent_ref=parent["task_ref"])

    # ---- name_theme ----

    def test_name_theme_bundles_members_with_provenance(self):
        t1 = self.ptm.create_task(persona_id="p1", title="詩集の下書き", auto_activate=False)
        self.ptm.update_task_status(
            t1["id"], status=STATUS_COMPLETED, actor="test", persona_id="p1",
        )
        self.tm.create("p1", "autonomous", title="言葉集め")
        theme = PT.name_theme(
            self.manager, "p1", "言葉あそび", [t1["task_ref"], "track:1"],
        )
        self.assertEqual(theme["stage"], STAGE_ADOPTED)
        self.assertEqual(theme["promoted_from"], [t1["task_ref"], "track:1"])
        # テーマは第一階層に立つ
        refs = [n["ref"] for n in PT.list_first_tier(self.manager, "p1")]
        self.assertIn(theme["ref"], refs)

    def test_name_theme_rejects_unknown_member(self):
        with self.assertRaises(PT.NodeNotFoundError):
            PT.name_theme(self.manager, "p1", "幽霊テーマ", ["task:999"])

    def test_name_theme_rejects_empty_members(self):
        with self.assertRaises(ValueError):
            PT.name_theme(self.manager, "p1", "空テーマ", [])

    # ---- list_first_tier ----

    def test_first_tier_lists_live_tracks_and_parentless_adopted(self):
        live_track = self.tm.create("p1", "user_conversation", title="まはーとの会話",
                                    is_persistent=True)
        done_track = self.tm.create(
            "p1", "autonomous", title="終わった企て", initial_status=STATUS_RUNNING,
        )
        self.tm.complete(done_track)
        # track 内小目標 (親あり) は第一階層に載らない
        self.ptm.create_task(
            persona_id="p1", title="小目標", parent_kind="track",
            track_id=live_track, auto_activate=False,
        )
        # 既存の未所属タスク = 親なし採用ノードとして載る (既定規則)
        loose = self.ptm.create_task(persona_id="p1", title="未所属", auto_activate=False)

        first = PT.list_first_tier(self.manager, "p1")
        kinds = {(n["node_kind"], n["id"]) for n in first}
        self.assertIn(("track", live_track), kinds)
        self.assertNotIn(("track", done_track), kinds)
        self.assertIn(("task", loose["id"]), kinds)
        # 永続 Track = 営み (practice) の写像
        track_node = next(n for n in first if n["id"] == live_track)
        self.assertEqual(track_node["nature"], "practice")

    # ---- resolve_ref / list_children ----

    def test_resolve_ref_track_task_and_uuid(self):
        track_id = self.tm.create("p1", "autonomous", title="t")
        task = self.ptm.create_task(persona_id="p1", title="x", auto_activate=False)
        self.assertEqual(PT.resolve_ref(self.manager, "p1", "track:1")["id"], track_id)
        self.assertEqual(PT.resolve_ref(self.manager, "p1", track_id)["id"], track_id)
        self.assertEqual(
            PT.resolve_ref(self.manager, "p1", task["task_ref"])["id"], task["id"]
        )
        self.assertEqual(PT.resolve_ref(self.manager, "p1", task["id"])["id"], task["id"])

    def test_resolve_ref_rejects_other_kinds_and_unknown(self):
        with self.assertRaises(PT.NodeNotFoundError):
            PT.resolve_ref(self.manager, "p1", "item:5")
        with self.assertRaises(PT.NodeNotFoundError):
            PT.resolve_ref(self.manager, "p1", "track:999")
        with self.assertRaises(PT.NodeNotFoundError):
            PT.resolve_ref(self.manager, "p1", "")

    def test_list_children_of_task_returns_steps(self):
        task = self.ptm.create_task(
            persona_id="p1", title="親",
            steps=[{"title": "step1"}, {"title": "step2", "status": "completed"}],
            auto_activate=False,
        )
        children = PT.list_children(self.manager, "p1", task["task_ref"])
        self.assertEqual([c["node_kind"] for c in children], ["step", "step"])
        self.assertEqual([c["title"] for c in children], ["step1", "step2"])
        self.assertEqual(children[1]["stage"], "completed")


if __name__ == "__main__":
    unittest.main()
