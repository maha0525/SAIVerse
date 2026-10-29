from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sai_memory.memory.storage import SYSTEM_NOTICE_KEY, get_messages_last, is_system_notice


class DummyEmbedder:
    def __init__(self, model: str | None = None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class ThreadSwitchToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"

        # Register temp dir cleanup first (LIFO → runs last, after all adapter closes)
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter

        self.adapter_cls = SAIMemoryAdapter
        self.adapter = self.adapter_cls("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(self.adapter.close)

    def _cleanup_temp(self) -> None:
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self) -> None:
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def test_switch_active_thread_creates_metadata_link(self) -> None:
        sns_suffix = "sns"
        talk_suffix = "talk"

        # Seed origin thread messages
        self.adapter.append_persona_message(
            {
                "role": "user",
                "content": "SNSを眺めていたよ",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "embedding_chunks": 0,
            },
            thread_suffix=sns_suffix,
        )
        self.adapter.append_persona_message(
            {
                "role": "assistant",
                "content": "猫ロボットの動画が面白かった",
                "timestamp": "2025-01-01T00:01:00+00:00",
                "embedding_chunks": 0,
            },
            thread_suffix=sns_suffix,
        )
        self.adapter.close()

        state_file = self.persona_path / "active_state.json"
        state_file.write_text(json.dumps({"active_thread_id": sns_suffix}), encoding="utf-8")

        from tool_loader import load_builtin_tool
        switch_active_thread = load_builtin_tool("thread_switch").switch_active_thread

        from tools.context import persona_context
        result, snippet, _ = None, None, None
        with persona_context("tester", self.persona_path):
            result, snippet, _ = switch_active_thread(
                target_thread=talk_suffix,
                summary="[スレッド移動] SNSから田中さんとの会話へ移動。",
                range_before=1,
            )

        self.assertIn("tester:talk", result)
        self.assertIsNotNone(snippet.history_snippet)

        adapter = None
        from saiverse_memory import SAIMemoryAdapter

        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(lambda: adapter and adapter.close())

        with adapter._db_lock:  # type: ignore[attr-defined]
            talk_thread_id = f"tester:{talk_suffix}"
            messages = get_messages_last(adapter.conn, talk_thread_id, 5)
            self.assertTrue(messages, "target thread should have messages")
            new_message = messages[-1]
            self.assertEqual(new_message.role, "system")
            self.assertIn("SNSから田中さんとの会話", new_message.content)
            self.assertIsInstance(new_message.metadata, dict)
            linked = new_message.metadata.get("other_thread_messages")
            self.assertIsInstance(linked, list)
            link_entry = linked[0]
            self.assertEqual(link_entry["thread_id"], "tester:sns")
            self.assertEqual(link_entry["range_before"], 1)
            self.assertEqual(link_entry["range_after"], 0)
            # 機構の行なので、システム通知の印 (系統名) を持つ
            self.assertEqual(new_message.metadata.get(SYSTEM_NOTICE_KEY), "thread_switch")
            self.assertTrue(is_system_notice(
                {"role": new_message.role, "metadata": new_message.metadata},
            ))

        updated_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(updated_state["active_thread_id"], talk_suffix)

    def test_switch_without_persona_id_uses_environment(self) -> None:
        sns_suffix = "sns"
        follow_suffix = "follow"

        adapter = self.adapter_cls("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            adapter.append_persona_message(
                {
                    "role": "assistant",
                    "content": "環境変数テスト",
                    "timestamp": "2025-02-01T00:00:00+00:00",
                    "embedding_chunks": 0,
                },
                thread_suffix=sns_suffix,
            )
        finally:
            adapter.close()

        state_file = self.persona_path / "active_state.json"
        state_file.write_text(json.dumps({"active_thread_id": sns_suffix}), encoding="utf-8")

        from tool_loader import load_builtin_tool
        switch_active_thread = load_builtin_tool("thread_switch").switch_active_thread
        from tools.context import persona_context

        with persona_context("tester", self.persona_path):
            switch_active_thread(target_thread=follow_suffix, summary="env call")

        adapter = self.adapter_cls("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(lambda: adapter.close())
        with adapter._db_lock:  # type: ignore[attr-defined]
            follow_thread_id = f"tester:{follow_suffix}"
            messages = get_messages_last(adapter.conn, follow_thread_id, 5)
            self.assertTrue(messages)
            inserted = messages[-1]
            self.assertEqual(inserted.role, "system")

    def test_default_note_is_a_system_notice_the_migration_recognises(self) -> None:
        """summary を渡さないときの本文も通知の印付きで書き、移行の規則 (版 4) と系統名が揃う。"""
        from database.system_notice_migration import (
            THREAD_SWITCH_KIND,
            classify_unmarked_system_message,
        )

        adapter = self.adapter_cls("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            adapter.append_persona_message(
                {
                    "role": "user",
                    "content": "元のスレッドの発言",
                    "timestamp": "2025-03-01T00:00:00+00:00",
                    "embedding_chunks": 0,
                },
                thread_suffix="sns",
            )
        finally:
            adapter.close()
        (self.persona_path / "active_state.json").write_text(
            json.dumps({"active_thread_id": "sns"}), encoding="utf-8",
        )

        from tool_loader import load_builtin_tool
        module = load_builtin_tool("thread_switch")
        from tools.context import persona_context

        with persona_context("tester", self.persona_path):
            module.switch_active_thread(target_thread="talk")

        adapter = self.adapter_cls("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(lambda: adapter.close())
        with adapter._db_lock:  # type: ignore[attr-defined]
            inserted = get_messages_last(adapter.conn, "tester:talk", 5)[-1]
        self.assertEqual(inserted.role, "system")
        self.assertEqual(inserted.content, "[thread switch] moved from tester:sns")
        self.assertEqual(inserted.metadata.get(SYSTEM_NOTICE_KEY), module.THREAD_SWITCH_NOTICE_KIND)
        self.assertEqual(module.THREAD_SWITCH_NOTICE_KIND, THREAD_SWITCH_KIND)
        # 印の無い旧い行として見ても、移行の規則は同じ系統名を返す
        unmarked = {k: v for k, v in inserted.metadata.items() if k != SYSTEM_NOTICE_KEY}
        self.assertEqual(
            classify_unmarked_system_message(unmarked, inserted.content), THREAD_SWITCH_KIND,
        )
        self.assertEqual(
            classify_unmarked_system_message({}, inserted.content), THREAD_SWITCH_KIND,
        )

    def test_switch_without_origin_messages_raises(self) -> None:
        follow_suffix = "follow-no-origin"

        from tool_loader import load_builtin_tool
        switch_active_thread = load_builtin_tool("thread_switch").switch_active_thread
        from tools.context import persona_context

        with persona_context("tester", self.persona_path):
            with self.assertRaises(ValueError):
                switch_active_thread(target_thread=follow_suffix, summary="リンクなし移動")


if __name__ == "__main__":
    unittest.main()
