"""CoreMemorySection (コア記憶→head) のテスト。

- capture / render / serialize 往復 (raw sqlite conn を持つ疑似 persona)
- 合成システムプロンプトへの出現 (関所の end-to-end): register_default_sections で
  組んだ pipeline 経由で render_head_messages を呼び、composed system message に
  コア記憶本文が含まれることを実 DB (temp) で実証する。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sai_memory.core_memory import add_core_memory, init_core_memory_table
from sea.head_pipeline.sections.core_memory import (
    CoreMemoryItem,
    CoreMemorySection,
    CoreMemorySnapshot,
)
from sea.head_pipeline.types import LineHeadInput


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _ctx(persona):
    return LineHeadInput(
        persona_id="tester", line_id="main", line_role="main_line",
        model_key="claude-opus-4-8", current_building_id="b",
        persona=persona, manager=None,
    )


class CoreMemorySectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_core_memory_table(self.conn)
        self.persona = SimpleNamespace(
            sai_memory=SimpleNamespace(conn=self.conn, _db_lock=threading.RLock())
        )

    def tearDown(self):
        self.conn.close()

    def test_capture_empty_renders_none(self):
        snap = CoreMemorySection().capture(_ctx(self.persona))
        self.assertEqual(snap.items, ())
        self.assertIsNone(CoreMemorySection().render(snap))

    def test_capture_no_sai_memory_returns_empty(self):
        snap = CoreMemorySection().capture(_ctx(SimpleNamespace(sai_memory=None)))
        self.assertEqual(snap.items, ())

    def test_capture_and_render(self):
        add_core_memory(self.conn, "まはーの誕生日は1月14日")
        add_core_memory(self.conn, "私は「エア」という名前")
        snap = CoreMemorySection().capture(_ctx(self.persona))
        self.assertEqual(len(snap.items), 2)
        rendered = CoreMemorySection().render(snap)
        self.assertIsNotNone(rendered)
        self.assertIn("## コア記憶", rendered.text)
        self.assertIn("- [core:1] まはーの誕生日は1月14日", rendered.text)
        self.assertIn("- [core:2] 私は「エア」という名前", rendered.text)

    def test_diff_never_notifies(self):
        old = CoreMemorySnapshot(items=())
        new = CoreMemorySnapshot(items=(CoreMemoryItem(memory_id=1, content="x"),))
        self.assertEqual(CoreMemorySection().diff_to_notifications(old, new), [])

    def test_snapshot_roundtrip(self):
        snap = CoreMemorySnapshot(items=(
            CoreMemoryItem(memory_id=1, content="A"),
            CoreMemoryItem(memory_id=2, content="B"),
        ))
        section = CoreMemorySection()
        restored = section.deserialize_snapshot(section.serialize_snapshot(snap))
        self.assertEqual(restored, snap)


class CoreMemoryComposedSystemPromptTest(unittest.TestCase):
    """関所の end-to-end: register_default_sections → render_head_messages で
    合成された system message にコア記憶本文が到達することを実 DB で実証する。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def test_core_memory_reaches_composed_system_prompt(self):
        from saiverse_memory import SAIMemoryAdapter
        from sea.head_pipeline.pipeline import HeadPipeline
        from sea.head_pipeline.registry import HeadSectionRegistry
        from sea.head_pipeline.sections import register_default_sections
        from sea.head_pipeline.integration import render_head_messages

        # add → adapter が core_memories テーブルを init 済み、そこへ書き込む。
        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter._db_lock:
                add_core_memory(adapter.conn, "まはーの誕生日は1月14日")
                add_core_memory(adapter.conn, "私は「エア」という名前")
        finally:
            adapter.close()

        # 再オープンした adapter を持つ persona で head を合成する。
        adapter2 = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        self.addCleanup(adapter2.close)
        persona = SimpleNamespace(
            persona_id="tester", default_model="claude-opus-4-8",
            sai_memory=adapter2,
        )
        manager = SimpleNamespace(SessionLocal=None)

        registry = HeadSectionRegistry()
        register_default_sections(registry)
        pipeline = HeadPipeline(registry=registry, store=None)

        # runtime_context.py と同じ固定 enabled_sections で core_memory を含める。
        enabled = {
            "common_prompt", "persona_self", "core_memory", "building", "spell_list",
            "autonomy_modes", "self_image", "desk",
        }
        messages = render_head_messages(
            persona, manager, "b", enabled_sections=enabled, pipeline=pipeline,
        )
        system_msgs = [m for m in messages if m.get("role") == "system"]
        self.assertTrue(system_msgs, "system message が合成されていること")
        combined = "\n".join(m["content"] for m in system_msgs)
        self.assertIn("## コア記憶", combined)
        self.assertIn("まはーの誕生日は1月14日", combined)
        self.assertIn("私は「エア」という名前", combined)


if __name__ == "__main__":
    unittest.main()
