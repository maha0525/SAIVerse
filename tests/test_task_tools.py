"""Task スペルの結合テスト (タスク一本化後 / unified_task_model.md §5)。

タスクは統合 persona_task テーブル (main DB) に一本化され、task:N 参照で指す。
スペル (task_add / task_done / task_update_step) は module レベルで
``SessionLocal`` に束縛した manager singleton を持つため、テストは load 後の
module 上で singleton を temp DB 版に差し替える ([[reference_test_infrastructure]]:
動的ロードは patch.object 必須)。
"""
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import ActionTrack, Base
from saiverse.persona_task_manager import PARENT_TRACK, PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool
from tools.context import persona_context

_mod_add = load_builtin_tool("task_add")
_mod_done = load_builtin_tool("task_done")
_mod_step = load_builtin_tool("task_update_step")
_mod_decompose = load_builtin_tool("task_decompose")


class TaskToolsTestCase(unittest.TestCase):
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

        # load 済みスペル module の manager singleton を temp DB 版へ差し替え
        _mod_add._track_manager = self.tm
        _mod_done._task_manager = self.ptm
        _mod_step._task_manager = self.ptm
        _mod_decompose._task_manager = self.ptm

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

    def test_task_add_assigns_ref(self) -> None:
        with persona_context(self.persona_id, self.persona_dir):
            out = _mod_add.task_add(track_id="track:1", title="調査する")
        self.assertIn("task:1", out)
        tasks = self.ptm.list_tasks(self.persona_id, track_id=self.track_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["parent_kind"], PARENT_TRACK)

    def test_task_done_by_ref(self) -> None:
        with persona_context(self.persona_id, self.persona_dir):
            _mod_add.task_add(track_id="track:1", title="やること")
            out = _mod_done.task_done(task_ref="task:1")
        self.assertIn("タスク完了", out)
        tasks = self.ptm.list_tasks(self.persona_id, track_id=self.track_id)
        self.assertEqual(tasks[0]["status"], "completed")

    def test_task_update_step_by_ref(self) -> None:
        # ステップ付きタスクを直接作る (decompose 相当)
        task = self.ptm.create_task(
            persona_id=self.persona_id, title="分解済みタスク",
            parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
            steps=[{"title": "下調べ"}, {"title": "実作業"}],
        )
        ref = task["task_ref"]
        with persona_context(self.persona_id, self.persona_dir):
            msg, snippet, _ = _mod_step.task_update_step(
                task_ref=ref, step_position=1, status="completed",
            )
        self.assertIn("Updated step 1", msg)
        self.assertIsNotNone(snippet.history_snippet)
        refreshed = self.ptm.get_task(task["id"], persona_id=self.persona_id)
        self.assertEqual(refreshed["steps"][0]["status"], "completed")
        # auto_advance: active_step が次 (step2) に進む
        self.assertEqual(refreshed["active_step_id"], refreshed["steps"][1]["id"])


    def test_task_decompose_writes_steps(self) -> None:
        # canonical 形式 (配列引数) のスペル行をパースして decompose を実行する
        from sea.runtime_llm import _coerce_spell_args, _parse_spell_lines

        with persona_context(self.persona_id, self.persona_dir):
            self.ptm.create_task(
                persona_id=self.persona_id, title="分解対象",
                parent_kind=PARENT_TRACK, track_id=self.track_id, auto_activate=False,
            )
            line = (
                "/spell name='task_decompose' args={\"task_ref\":\"task:1\","
                "\"steps\":[{\"title\":\"下調べ\"},{\"title\":\"実作業\"},{\"title\":\"仕上げ\"}]}"
            )
            name, args, _meta, _raw = _parse_spell_lines(line)[0]
            args = _coerce_spell_args(name, args)
            self.assertEqual(len(args["steps"]), 3)  # 配列引数が canonical で解析される
            out = _mod_decompose.task_decompose(**args)

        self.assertIn("task:1", out)
        tasks = self.ptm.list_tasks(self.persona_id, track_id=self.track_id)
        self.assertEqual(len(tasks[0]["steps"]), 3)
        # active_step は最初のステップ
        self.assertEqual(tasks[0]["active_step_id"], tasks[0]["steps"][0]["id"])


if __name__ == "__main__":
    unittest.main()
