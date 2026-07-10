"""purpose スペル群 (P2c-1) の結合テスト。

concept_consolidation.md「P2: 統一スペル動詞」の地図別動詞 + P2c-0 決定4
(候補を生む seed と木に接ぐ adopt は別動詞):

- purpose_seed: 候補を生む (旧 desire_add 後継。接地 source の維持を検証)
- purpose_adopt: 候補の採用 (接ぎ木) / 枝への小目標追加 (旧 task_add 後継)
- purpose_decompose: ステップ分解 (旧 task_decompose 後継)
- purpose_step: ステップ状態更新 (旧 task_update_step 後継)
- purpose_close: 完了・中止・休眠の stage 遷移 (旧 task_done 後継 + 拡張)
- モード権限 (旧 task/desire 系と同一のゲート)

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
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import (
    PARENT_NOTE,
    PARENT_TRACK,
    STAGE_ADOPTED,
    STAGE_CANDIDATE,
    STAGE_DORMANT,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
)
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool
from tools.context import persona_context

_mod_seed = load_builtin_tool("purpose_seed")
_mod_adopt = load_builtin_tool("purpose_adopt")
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
        self.tm = TrackManager(session_factory=self.SessionLocal)
        self.nm = NoteManager(session_factory=self.SessionLocal)

        # load 済みスペル module の manager singleton を temp DB 版へ差し替え
        _mod_seed._note_manager = self.nm
        _mod_seed._task_manager = self.ptm
        _mod_adopt._track_manager = self.tm
        _mod_adopt._manager.SessionLocal = self.SessionLocal
        _mod_decompose._task_manager = self.ptm
        _mod_step._task_manager = self.ptm
        _mod_close._task_manager = self.ptm

        # 自律 Track を1本 (track:1 が解決できるように)
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
    # purpose_seed — 候補を生む (接地の維持)
    # ------------------------------------------------------------------

    def test_seed_creates_note_bound_candidate_with_grounding(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_seed.purpose_seed(
                title="風景スケッチの練習をしたい",
                type="作る",
                source="窓から見た夕暮れの色",
            )
        self.assertIn("task:1", out)
        self.assertIn("作る", out)
        note_id = self.nm.ensure_desire_note(self.persona_id)
        tasks = self.ptm.list_tasks(self.persona_id, note_id=note_id)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["parent_kind"], PARENT_NOTE)
        self.assertEqual(t["stage"], STAGE_CANDIDATE)  # 候補 = 木の外
        self.assertEqual(t["desire_type"], "作る")
        # 接地の維持 (旧 desire_add の source 規律そのまま)
        self.assertEqual(t["desire_source"], "窓から見た夕暮れの色")

    def test_seed_without_type_is_unclassified(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_seed.purpose_seed(title="新しい言語を学びたい")
        self.assertIn("未分類", out)

    def test_seed_rejects_invalid_type(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_seed.purpose_seed(title="なにか", type="遊ぶ")
        self.assertIn("Error", out)
        self.assertIn("遊ぶ", out)

    # ------------------------------------------------------------------
    # purpose_adopt — 候補の採用 (接ぎ木) / 枝への小目標追加
    # ------------------------------------------------------------------

    def _seed_candidate(self, title="読書会を開きたい"):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_seed.purpose_seed(title=title, source="友人の一言")
        self.assertIn("task:", out)
        note_id = self.nm.ensure_desire_note(self.persona_id)
        return self.ptm.list_tasks(self.persona_id, note_id=note_id)[-1]

    def test_adopt_candidate_without_parent_stands_first_tier(self):
        cand = self._seed_candidate()
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(candidate_ref=cand["task_ref"])
        self.assertIn("採用しました", out)
        self.assertIn("第一階層", out)
        adopted = self.ptm.get_task(cand["id"], persona_id=self.persona_id)
        self.assertEqual(adopted["stage"], STAGE_ADOPTED)
        self.assertIsNone(adopted["parent_kind"])  # 親なし採用ノード

    def test_adopt_candidate_under_track(self):
        cand = self._seed_candidate()
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(
                candidate_ref=cand["task_ref"], parent_ref="track:1",
            )
        self.assertIn("採用しました", out)
        adopted = self.ptm.get_task(cand["id"], persona_id=self.persona_id)
        self.assertEqual(adopted["stage"], STAGE_ADOPTED)
        self.assertEqual(adopted["parent_kind"], PARENT_TRACK)
        self.assertEqual(adopted["track_id"], self.track_id)

    def test_adopt_rejects_non_candidate(self):
        # 既に採用済み (track 内小目標) は候補ではない → エラー
        task = self.ptm.create_task(
            persona_id=self.persona_id, title="採用済み",
            parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
        )
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(candidate_ref=task["task_ref"])
        self.assertIn("Error", out)

    def test_adopt_title_creates_task_in_track(self):
        # 旧 task_add 後継: 枝に新しい小目標を直接作る
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(title="資料を調べる", parent_ref="track:1")
        self.assertIn("小目標を追加しました", out)
        tasks = self.ptm.list_tasks(self.persona_id, track_id=self.track_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["parent_kind"], PARENT_TRACK)

    def test_adopt_title_requires_parent(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(title="宙に浮く小目標")
        self.assertIn("Error", out)
        self.assertIn("purpose_seed", out)  # 候補にしたいなら seed への誘導

    def test_adopt_requires_exactly_one_of_candidate_or_title(self):
        with persona_context(self.persona_id, self.persona_dir):
            neither = _mod_adopt.purpose_adopt()
            both = _mod_adopt.purpose_adopt(
                candidate_ref="task:1", title="両方", parent_ref="track:1",
            )
        self.assertIn("Error", neither)
        self.assertIn("Error", both)

    def test_adopt_unknown_candidate_reports_error(self):
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_adopt.purpose_adopt(candidate_ref="task:999")
        self.assertIn("Error", out)

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
        "purpose_seed", "purpose_adopt", "purpose_decompose",
        "purpose_step", "purpose_close",
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
    """4+1 本全てが spell=True で露出し、説明が動詞の違いを明文化していること。"""

    def test_all_registered_as_spells(self):
        for mod, name in (
            (_mod_seed, "purpose_seed"),
            (_mod_adopt, "purpose_adopt"),
            (_mod_decompose, "purpose_decompose"),
            (_mod_step, "purpose_step"),
            (_mod_close, "purpose_close"),
        ):
            sch = mod.schema()
            self.assertEqual(sch.name, name)
            self.assertTrue(sch.spell)
            self.assertTrue(sch.spell_display_name)

    def test_seed_and_adopt_descriptions_distinguish_verbs(self):
        # seed=生む / adopt=接ぐ の違いが説明文に焼き込まれている
        seed_desc = _mod_seed.schema().description
        adopt_desc = _mod_adopt.schema().description
        self.assertIn("候補", seed_desc)
        self.assertIn("purpose_adopt", seed_desc)
        self.assertIn("接ぎます", adopt_desc)
        self.assertIn("purpose_seed", adopt_desc)


if __name__ == "__main__":
    unittest.main()
