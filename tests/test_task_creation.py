import json
import os
import tempfile
import unittest
from pathlib import Path

from persona.tasks.creation import TaskCreationProcessor


class TaskCreationProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name) / ".saiverse"
        self.persona_dir = self.base / "personas" / "tester"
        self.persona_dir.mkdir(parents=True)
        self.requests_file = self.persona_dir / "task_requests.jsonl"
        entry = {
            "id": "req-1",
            "summary": "星空を題材にした短編を書きたい",
            "context": "主人公は天文学者",
            "priority": "normal",
            "persona_id": "tester",
            "created_at": "2025-11-01T12:00:00Z",
        }
        self.requests_file.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        os.environ["SAIVERSE_TASK_CREATION_USE_LLM"] = "0"

    def tearDown(self) -> None:
        os.environ.pop("SAIVERSE_TASK_CREATION_USE_LLM", None)
        self.tmp.cleanup()

    def test_process_pending_requests_creates_task(self) -> None:
        processor = TaskCreationProcessor(self.persona_dir)
        processed = processor.process_pending_requests()
        self.assertEqual(processed, ["req-1"])
        storage_file = self.persona_dir / "tasks.db"
        self.assertTrue(storage_file.exists())
        # Ensure requests file removed after processing
        self.assertFalse(self.requests_file.exists())


if __name__ == "__main__":
    unittest.main()
