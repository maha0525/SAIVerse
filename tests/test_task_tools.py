import json
import tempfile
import unittest
from pathlib import Path

from persona.tasks import TaskStorage
from tools.context import persona_context
from tools.defs.task_change_active import task_change_active
from tools.defs.task_close import task_close
from tools.defs.task_request_creation import task_request_creation
from tools.defs.task_update_step import task_update_step


class TaskToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".saiverse"
        self.persona_id = "tester"
        self.persona_dir = self.root / "personas" / self.persona_id
        self.persona_dir.mkdir(parents=True)
        self.storage = TaskStorage(self.persona_id, base_dir=self.root)
        # Seed two tasks
        self.storage.create_task(
            title="短編小説タスクA",
            goal="短編小説Aを完成させる",
            summary="短編小説Aの執筆",
            notes=None,
            steps=[
                {"title": "プロット"},
                {"title": "本文"},
            ],
            actor=self.persona_id,
        )
        self.storage.create_task(
            title="短編小説タスクB",
            goal="短編小説Bを完成させる",
            summary="短編小説Bの執筆",
            notes=None,
            steps=[{"title": "プロットB"}],
            actor=self.persona_id,
        )

    def tearDown(self) -> None:
        self.storage.close()
        self.tmp.cleanup()

    def test_task_change_active_and_update_flow(self) -> None:
        with persona_context(self.persona_id, self.persona_dir):
            msg, snippet, _ = task_change_active()
            self.assertIn("Activated task", msg)
            self.assertIsNotNone(snippet.history_snippet)

            msg, snippet, _ = task_update_step(step_position=1, status="completed")
            self.assertIn("Updated step 1", msg)
            self.assertIsNotNone(snippet.history_snippet)

        refreshed = TaskStorage(self.persona_id, base_dir=self.root)
        try:
            active_tasks = refreshed.list_tasks(statuses=["active"], limit=1, include_steps=True)
            self.assertTrue(active_tasks)
            active = active_tasks[0]
            self.assertEqual(active.steps[0].status, "completed")
        finally:
            refreshed.close()

    def test_task_close_auto_activates_next(self) -> None:
        with persona_context(self.persona_id, self.persona_dir):
            task_change_active()
            msg, snippet, _ = task_close(status="completed", reason="done")
            self.assertIn("Marked task", msg)
            self.assertIsNotNone(snippet.history_snippet)

        refreshed = TaskStorage(self.persona_id, base_dir=self.root)
        try:
            active_tasks = refreshed.list_tasks(statuses=["active"], limit=1, include_steps=True)
            self.assertTrue(active_tasks)
            self.assertIn("タスクA", active_tasks[0].title)
        finally:
            refreshed.close()

    def test_task_request_creation_logs_entry(self) -> None:
        with persona_context(self.persona_id, self.persona_dir):
            msg, snippet, _ = task_request_creation(summary="新しい創作タスク", context="テーマは星空")
            self.assertIn("Task creation request", msg)
            self.assertIsNotNone(snippet.history_snippet)

        log_path = self.persona_dir / "task_requests.jsonl"
        if log_path.exists():
            entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["summary"], "新しい創作タスク")
        else:
            storage = TaskStorage(self.persona_id, base_dir=self.root)
            try:
                tasks = storage.list_tasks()
                self.assertTrue(any(task.summary == "新しい創作タスク" for task in tasks))
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
