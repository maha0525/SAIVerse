from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sai_memory.memory.storage import get_messages_last

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

    def test_metadata_links_expand_content(self) -> None:
        os.environ["SAIMEMORY_MEMORY"] = "1"

        class DummyEmbedder:
            def __init__(self, model: str | None = None) -> None:
                self.model_name = model

            def embed(self, texts):
                return [[0.0] * 3 for _ in texts]

        with patch("saiverse_memory.adapter.Embedder", DummyEmbedder):
            adapter = self.adapter_cls("tester", persona_dir=self.persona_dir)
            try:
                sns_suffix = "sns-thread"
                sns_thread_id = adapter._thread_id(None, thread_suffix=sns_suffix)
                adapter.append_persona_message(
                    {
                        "role": "user",
                        "content": "SNSを眺めていたよ",
                        "timestamp": "2025-01-01T00:00:00",
                        "embedding_chunks": 0,
                    },
                    thread_suffix=sns_suffix,
                )
                adapter.append_persona_message(
                    {
                        "role": "assistant",
                        "content": "猫ロボットの動画が面白かった",
                        "timestamp": "2025-01-01T00:01:00",
                        "embedding_chunks": 0,
                    },
                    thread_suffix=sns_suffix,
                )
                with adapter._db_lock:
                    sns_messages = get_messages_last(adapter.conn, sns_thread_id, 5)
                    anchor = sns_messages[-1]

                metadata = {
                    "other_thread_messages": [
                        {
                            "thread_id": sns_thread_id,
                            "message_id": anchor.id,
                            "range_before": 1,
                            "range_after": 0,
                        }
                    ]
                }
                adapter.append_persona_message(
                    {
                        "role": "system",
                        "content": "moved from sns-thread",
                        "timestamp": "2025-01-01T00:02:00",
                        "embedding_chunks": 0,
                        "metadata": metadata,
                    }
                )

                messages = adapter.recent_persona_messages(5000)
                linked = [m for m in messages if "linked-thread" in m.get("content", "")]
                self.assertTrue(linked, "Expected linked thread snippet in recent messages")
                self.assertIn("tester:sns-thread", linked[0]["content"])
                self.assertIn("猫ロボットの動画が面白かった", linked[0]["content"])
            finally:
                adapter.close()
        os.environ["SAIMEMORY_MEMORY"] = "0"


if __name__ == "__main__":
    unittest.main()
