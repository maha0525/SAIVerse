"""purpose スペル群 (P2c-1) の結合テスト。

concept_consolidation.md「P2: 統一スペル動詞」の地図別動詞:

- purpose_decompose: ステップ分解 (旧 task_decompose 後継)
- purpose_step: ステップ状態更新 (旧 task_update_step 後継)
- purpose_close: 完了・中止・休眠の stage 遷移 (旧 task_done 後継 + 拡張)
- モード権限 (旧 task 系と同一のゲート)

``purpose_seed`` / ``purpose_adopt`` は 2026-08-21 に退役した (欲求プールと
目的の木が機構ごと消えたため — autonomous_behavior_v3.md §8/§9-5)。

スペルは module レベルで SessionLocal に束縛した manager singleton を持つため、
テストは load 後の module 上で singleton を temp DB 版に差し替える
(tests/test_task_tools.py と同じ流儀)。
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import ActionTrack, Base
from saiverse.persona_task_manager import (
    PARENT_TRACK,
    STAGE_DORMANT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
)
from tool_loader import load_builtin_tool
from tools.context import persona_context

_mod_decompose = load_builtin_tool("purpose_decompose")
_mod_step = load_builtin_tool("purpose_step")
_mod_close = load_builtin_tool("purpose_close")


class PurposeToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".saiverse"
        self.persona_id = "tester"
        self.persona_dir = self.root / "personas" / self.persona_id
        self.persona_dir.mkdir(parents=True)

        db_path = self.root / "saiverse_test.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.ptm = PersonaTaskManager(self.SessionLocal)

        # load 済みスペル module の manager singleton を temp DB 版へ差し替え
        _mod_decompose._task_manager = self.ptm
        _mod_step._task_manager = self.ptm
        _mod_close._task_manager = self.ptm

        # タスクの親になる Track 行を 1 本。TrackManager は 2026-08-22 (束 6c) に
        # 退役したので、旧データ相当の ActionTrack 行を ORM で直接置く。
        # (PersonaTaskManager の parent_kind='track' 経路はまだ生きている)
        self.track_id = str(uuid.uuid4())
        db = self.SessionLocal()
        db.add(ActionTrack(
            track_id=self.track_id, persona_id=self.persona_id, short_id=1,
            track_type="autonomous", status="running",
        ))
        db.commit()
        db.close()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # purpose_decompose / purpose_step
    # ------------------------------------------------------------------

    def test_decompose_writes_steps(self):
        task = self.ptm.create_task(
            persona_id=self.persona_id, title="分解対象",
            parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
        )
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_decompose.purpose_decompose(
                node_ref=task["task_ref"],
                steps=[{"title": "下調べ"}, {"title": "実作業"}, {"title": "仕上げ"}],
            )
        self.assertIn(task["task_ref"], out)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        self.assertEqual(len(refreshed["steps"]), 3)
        self.assertEqual(refreshed["active_step_id"], refreshed["steps"][0]["id"])

    def test_decompose_rejects_empty_steps(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_decompose.purpose_decompose(node_ref="task:1", steps=[])
        self.assertIn("Error", out)

    def test_step_updates_status_and_auto_advances(self):
        task = self.ptm.create_task(
            persona_id=self.persona_id, title="分解済み",
            parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
            steps=[{"title": "下調べ"}, {"title": "実作業"}],
        )
        with persona_context(self.persona_id, self.persona_dir):
            msg, snippet, _ = _mod_step.purpose_step(
                node_ref=task["task_ref"], step_position=1, status="completed",
            )
        self.assertIn("Updated step 1", msg)
        self.assertIsNotNone(snippet.history_snippet)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        self.assertEqual(refreshed["steps"][0]["status"], "completed")
        # auto_advance: active_step が次 (step2) に進む
        self.assertEqual(refreshed["active_step_id"], refreshed["steps"][1]["id"])

    # ------------------------------------------------------------------
    # purpose_close — 完了・中止・休眠
    # ------------------------------------------------------------------

    def _make_adopted_task(self, title="閉じる対象"):
        return self.ptm.create_task(
            persona_id=self.persona_id, title=title,
            parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
        )

    def test_close_completed(self):
        task = self._make_adopted_task()
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_close.purpose_close(node_ref=task["task_ref"])
        self.assertIn("完了", out)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        self.assertEqual(refreshed["status"], STATUS_COMPLETED)

    def test_close_cancelled(self):
        task = self._make_adopted_task()
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_close.purpose_close(
                node_ref=task["task_ref"], outcome="cancelled", reason="優先度が下がった",
            )
        self.assertIn("中止", out)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        self.assertEqual(refreshed["status"], STATUS_CANCELLED)

    def test_close_dormant_marks_stage(self):
        task = self._make_adopted_task()
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_close.purpose_close(node_ref=task["task_ref"], outcome="dormant")
        self.assertIn("休眠", out)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        # 休眠 = 論理アーカイブ (cancelled) + stage=dormant の明示 (§5)
        self.assertEqual(refreshed["status"], STATUS_CANCELLED)
        self.assertEqual(refreshed["stage"], STAGE_DORMANT)

    def test_close_rejects_unknown_outcome(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_close.purpose_close(node_ref="task:1", outcome="postponed")
        self.assertIn("Error", out)

    def test_close_unknown_ref_reports_error(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_close.purpose_close(node_ref="task:999")
        self.assertIn("Error", out)


class PurposeSpellPermissionTests(unittest.TestCase):
    """purpose 動詞は旧 task/desire 系と同じモード権限 (mode_spell_permissions.md §5)。"""

    PURPOSE_SPELLS = (
        "purpose_decompose", "purpose_step", "purpose_close",
    )

    def test_purpose_allowed_in_autonomous_and_conversation(self):
        from sea.mode_spell_permissions import check_spell_permission
        from sea.pulse_context import Aspect

        for sp in self.PURPOSE_SPELLS:
            self.assertIsNone(check_spell_permission(sp, Aspect.AUTONOMOUS))
            self.assertIsNone(check_spell_permission(sp, Aspect.CONVERSATION))

    def test_purpose_blocked_in_meta_and_worker(self):
        from sea.mode_spell_permissions import check_spell_permission
        from sea.pulse_context import Aspect

        for sp in self.PURPOSE_SPELLS:
            self.assertIsNotNone(check_spell_permission(sp, Aspect.META))
            self.assertIsNotNone(check_spell_permission(sp, Aspect.WORKER))

    def test_old_names_no_longer_gated(self):
        # P2c-4a: 旧 task_*/desire_add スペル自体が撤去されたので、ゲートからも
        # 落ちている (aspect 制限なし = 実体が無い名前を誤って通しても実害は無い)。
        from sea.mode_spell_permissions import check_spell_permission
        from sea.pulse_context import Aspect

        for sp in ("task_add", "task_done", "desire_add"):
            self.assertIsNone(check_spell_permission(sp, Aspect.META))


class PurposeSpellSchemaTests(unittest.TestCase):
    """残る 3 本が spell=True で露出していること。"""

    def test_all_registered_as_spells(self):
        for mod, name in (
            (_mod_decompose, "purpose_decompose"),
            (_mod_step, "purpose_step"),
            (_mod_close, "purpose_close"),
        ):
            sch = mod.schema()
            self.assertEqual(sch.name, name)
            self.assertTrue(sch.spell)
            self.assertTrue(sch.spell_display_name)


if __name__ == "__main__":
    unittest.main()
