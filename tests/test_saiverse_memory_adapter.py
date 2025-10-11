from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


class ActiveThreadAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name)
        os.environ["SAIMEMORY_MEMORY"] = "0"

        # Lazy import to apply environment overrides before settings load.
        from saiverse_memory.adapter import SAIMemoryAdapter

        self.adapter_cls = SAIMemoryAdapter

    def tearDown(self) -> None:
        self._tmp.cleanup()
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _create_adapter(self):
        return self.adapter_cls("tester", persona_dir=self.persona_dir)

    def test_default_persona_suffix(self) -> None:
        adapter = self._create_adapter()
        thread_id = adapter._thread_id()
        self.assertEqual(thread_id, "tester:__persona__")

    def test_reads_active_state_file(self) -> None:
        adapter = self._create_adapter()
        state_path = self.persona_dir / "active_state.json"
        state_path.write_text(json.dumps({"active_thread_id": "uuid-123"}), encoding="utf-8")

        thread_id = adapter._thread_id()
        self.assertEqual(thread_id, "tester:uuid-123")

    def test_thread_suffix_override(self) -> None:
        adapter = self._create_adapter()
        state_path = self.persona_dir / "active_state.json"
        state_path.write_text(json.dumps({"active_thread_id": "uuid-123"}), encoding="utf-8")

        thread_id = adapter._thread_id(thread_suffix="custom")
        self.assertEqual(thread_id, "tester:custom")

    def test_building_id_prioritised(self) -> None:
        adapter = self._create_adapter()
        thread_id = adapter._thread_id("room-42")
        self.assertEqual(thread_id, "tester:room-42")


if __name__ == "__main__":
    unittest.main()
