import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from persona.tasks import TaskConflictError, TaskNotFoundError, TaskStorage


class TaskStorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base_dir = Path(self.tmpdir.name)
        self.storage = TaskStorage("persona_test", base_dir=base_dir)

    def tearDown(self) -> None:
        self.storage.close()
        self.tmpdir.cleanup()

    def test_initialization_creates_schema(self) -> None:
        conn = sqlite3.connect(self.storage.db_path)
        try:
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(user_version, 1)
            # ensure key tables exist
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("tasks", tables)
            self.assertIn("task_steps", tables)
            self.assertIn("task_history", tables)
        finally:
            conn.close()

    def test_create_and_list_task(self) -> None:
        task = self.storage.create_task(
            title="短編小説の執筆",
            goal="挿絵付き短編小説を完成させる",
            summary="短編小説を書いて挿絵案をまとめる",
            notes="最終的にテキストファイルで出力する",
            steps=[
                {"title": "テーマ決め"},
                {"title": "プロット作成"},
                {"title": "本文執筆"},
            ],
            actor="persona_test",
        )
        self.assertEqual(task.title, "短編小説の執筆")
        self.assertEqual(len(task.steps), 3)
        tasks = self.storage.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].goal, "挿絵付き短編小説を完成させる")

    def test_update_step_status(self) -> None:
        task = self.storage.create_task(
            title="試験タスク",
            goal="テストのためのタスク",
            summary="テストタスク",
            notes=None,
            steps=[{"title": "最初のステップ"}],
            actor="tester",
        )
        step_id = task.steps[0].id
        updated = self.storage.update_step_status(
            step_id,
            status="completed",
            notes="完了しました",
            actor="tester",
        )
        self.assertEqual(updated.steps[0].status, "completed")
        history = self.storage.fetch_history(task.id)
        self.assertTrue(
            any(entry.event_type == "update_step_status" for entry in history)
        )

    def test_conflict_detection(self) -> None:
        task = self.storage.create_task(
            title="競合テスト",
            goal="競合更新を検証",
            summary="競合テスト",
            notes=None,
            steps=[{"title": "ステップA"}],
            actor="tester",
        )
        # simulate stale version by manual update without version guard
        initial_version = task.version
        conn = sqlite3.connect(self.storage.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET version = version + 1 WHERE id = ?",
                (task.id,),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(TaskConflictError):
            self.storage.update_task_status(
                task.id,
                status="active",
                actor="tester",
                reason=None,
                expected_version=initial_version,
            )

        with self.assertRaises(TaskNotFoundError):
            self.storage.update_step_status(
                "unknown-step", status="completed", actor="tester"
            )


if __name__ == "__main__":
    unittest.main()
