"""コア記憶 scene 種別 (実会話の切り抜き) のテスト。

対象:
- sai_memory/memory/storage.py: get_conversation_window_around (窓切り出し)
- sai_memory/core_memory.py: add_core_memory の kind/metadata 拡張
- builtin_data/tools/memory_clip.py (mode='transcribe' paste_to='core'、旧
  core_memory_add_scene 後継): スペル本体 (原文忠実性・ID両形式)
- sea/head_pipeline/sections/core_memory.py: scene の render (コードブロック枠・3要素・
  snapshot 往復の後方互換)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from sai_memory.core_memory import add_core_memory, init_core_memory_table, list_core_memories
from sai_memory.memory.storage import (
    add_message,
    get_conversation_window_around,
    get_or_create_thread,
    init_db,
)
from sea.head_pipeline.sections.core_memory import (
    CoreMemoryItem,
    CoreMemorySection,
    CoreMemorySnapshot,
)


class GetConversationWindowAroundTest(unittest.TestCase):
    """窓切り出しの正確性 (アンカー中心・フィルタ除外・端で短くなるケース)。"""

    def setUp(self):
        self.conn = init_db(":memory:")
        get_or_create_thread(self.conn, "thread-1", resource_id="tester")

    def tearDown(self):
        self.conn.close()

    def _add(self, role: str, content: str, **kwargs) -> str:
        return add_message(
            self.conn, thread_id="thread-1", role=role, content=content,
            resource_id="tester", **kwargs,
        )

    def test_anchor_centered_window(self):
        ids = []
        for i in range(10):
            role = "user" if i % 2 == 0 else "model"
            ids.append(self._add(role, f"msg{i}"))
        anchor = ids[5]
        window = get_conversation_window_around(self.conn, anchor, rounds=2)
        # rounds=2 -> window=4 件ずつ。中央 (index 5) の前後4件ずつ + アンカー = 9件
        self.assertEqual(len(window), 9)
        self.assertEqual([m.content for m in window], [f"msg{i}" for i in range(1, 10)])
        # アンカー自身が含まれる
        self.assertIn(anchor, [m.id for m in window])

    def test_excludes_system_notice_messages(self):
        # Track切替通知等のシステム通知は role=user・タグなしで保存される世代があり、
        # content が '<system>' で始まる。scene (口調のアンカー) に混入すると手本が
        # 劣化するため、窓からも検索からも除外される (2026-07-04)。
        ids = []
        for i in range(6):
            role = "user" if i % 2 == 0 else "model"
            ids.append(self._add(role, f"msg{i}"))
        self._add("user", "<system>## Track切替通知\n作業に戻ります</system>")
        ids.append(self._add("model", "msg6"))
        anchor = ids[5]
        window = get_conversation_window_around(self.conn, anchor, rounds=2)
        contents = [m.content for m in window]
        self.assertTrue(all(not c.startswith("<system>") for c in contents))
        self.assertIn("msg6", contents)  # 通知を飛ばして実会話は拾う

    def test_search_excludes_system_notice_messages(self):
        from sai_memory.memory.storage import search_conversation_messages
        self._add("user", "スタックチャンの話をしよう")
        self._add("user", "<system>## Track切替通知: スタックチャン関連</system>")
        hits = search_conversation_messages(self.conn, ["スタックチャン"])
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].content.startswith("<system>"))

    def test_window_shrinks_at_start_of_conversation(self):
        # rounds=2 -> window=4 件/方向欲しいが、先頭アンカーの前には何も無いので
        # before 側は 0 件に自然に縮む (after は要求通り4件)。
        ids = []
        for i in range(8):
            role = "user" if i % 2 == 0 else "model"
            ids.append(self._add(role, f"msg{i}"))
        anchor = ids[0]  # 先頭アンカー -> before 側は無い
        window = get_conversation_window_around(self.conn, anchor, rounds=2)
        self.assertEqual(window[0].id, anchor)
        self.assertEqual([m.content for m in window], [f"msg{i}" for i in range(0, 5)])

    def test_window_shrinks_at_end_of_conversation(self):
        # rounds=2 -> window=4 件/方向欲しいが、末尾アンカーの後には何も無いので
        # after 側は 0 件に自然に縮む (before は要求通り4件)。
        ids = []
        for i in range(8):
            role = "user" if i % 2 == 0 else "model"
            ids.append(self._add(role, f"msg{i}"))
        anchor = ids[-1]
        window = get_conversation_window_around(self.conn, anchor, rounds=2)
        self.assertEqual(window[-1].id, anchor)
        self.assertEqual([m.content for m in window], [f"msg{i}" for i in range(3, 8)])

    def test_excludes_handy_tool_and_spell_and_event_message_tags(self):
        self._add("user", "before")
        self._add(
            "model", "tool call log",
            metadata={"tags": ["conversation", "handy_tool"]},
        )
        self._add(
            "model", "spell log",
            metadata={"tags": ["conversation", "spell"]},
        )
        self._add(
            "model", "event notice",
            metadata={"tags": ["internal", "event_message"]},
        )
        anchor = self._add("user", "anchor")
        self._add("model", "after")

        window = get_conversation_window_around(self.conn, anchor, rounds=5)
        contents = [m.content for m in window]
        self.assertIn("before", contents)
        self.assertIn("after", contents)
        self.assertNotIn("tool call log", contents)
        self.assertNotIn("spell log", contents)
        self.assertNotIn("event notice", contents)
        self.assertEqual(len(window), 3)  # before, anchor, after のみ

    def test_excludes_sub_line_and_meta_judgment_line_roles(self):
        self._add("user", "before")
        self._add("model", "worker step", line_role="sub_line")
        self._add("model", "meta decision", line_role="meta_judgment")
        anchor = self._add("user", "anchor")
        window = get_conversation_window_around(self.conn, anchor, rounds=5)
        contents = [m.content for m in window]
        self.assertNotIn("worker step", contents)
        self.assertNotIn("meta decision", contents)

    def test_excludes_non_conversation_roles(self):
        self._add("system", "system note")
        self._add("host", "host note")
        anchor = self._add("user", "anchor")
        window = get_conversation_window_around(self.conn, anchor, rounds=5)
        contents = [m.content for m in window]
        self.assertNotIn("system note", contents)
        self.assertNotIn("host note", contents)

    def test_includes_assistant_role_openai_style_import(self):
        # 実 DB 実査 (2026-07-04): インポートされた ChatGPT 由来のログでは
        # persona 応答の role が 'model' でなく 'assistant' で保存されている。
        before = self._add("user", "before")
        anchor = self._add("assistant", "assistant reply")
        window = get_conversation_window_around(self.conn, anchor, rounds=2)
        contents = [m.content for m in window]
        self.assertIn("before", contents)
        self.assertIn("assistant reply", contents)
        self.assertEqual(window[0].id, before)

    def test_missing_anchor_returns_none(self):
        self.assertIsNone(get_conversation_window_around(self.conn, "no-such-id", rounds=3))

    def test_anchor_itself_excluded_returns_none(self):
        anchor = self._add(
            "model", "tool log", metadata={"tags": ["conversation", "handy_tool"]},
        )
        self.assertIsNone(get_conversation_window_around(self.conn, anchor, rounds=3))

    def test_content_is_byte_exact_not_altered(self):
        # 原文忠実性: 改行・記号・絵文字混じりでも一切改変されない。
        tricky = "ソフィーーーー！！！いけたーーーー！！\n『引用符』と`バッククォート`と```三連```も含む。"
        anchor = self._add("user", tricky)
        window = get_conversation_window_around(self.conn, anchor, rounds=1)
        self.assertEqual(window[0].content, tricky)


class AddCoreMemoryKindMetadataTest(unittest.TestCase):
    """sai_memory/core_memory.py の kind/metadata 拡張。"""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        init_core_memory_table(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_default_kind_is_note_backward_compat(self):
        add_core_memory(self.conn, "普通のメモ")
        item = list_core_memories(self.conn)[0]
        self.assertEqual(item.kind, "note")
        self.assertIsNone(item.metadata)

    def test_scene_kind_and_metadata_stored(self):
        meta = json.dumps({"anchor_id": "m1", "message_ids": ["m1"], "date_range": ["2025-01-12", "2025-01-12"]})
        add_core_memory(self.conn, "user「やった」\nエア「よかった」", kind="scene", metadata=meta)
        item = list_core_memories(self.conn)[0]
        self.assertEqual(item.kind, "scene")
        self.assertEqual(json.loads(item.metadata)["anchor_id"], "m1")


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_manager(persona_name="Tester"):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="tester", HOME_CITYID=city.CITYID, AINAME=persona_name))
        db.commit()
    finally:
        db.close()
    return engine, SimpleNamespace(SessionLocal=Session)


class CoreMemoryAddSceneToolTest(unittest.TestCase):
    """スペル本体 (旧 core_memory_add_scene → memory_clip mode='transcribe'
    paste_to='core' に統合、P2c-4a): 決定論コピー・ID両形式・存在しないID。"""

    def setUp(self):
        from tool_loader import load_builtin_tool

        self._tmp = tempfile.TemporaryDirectory()
        self.persona_path = Path(self._tmp.name) / "personas" / "tester"
        self.persona_path.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        self.mod = load_builtin_tool("memory_clip")
        self.engine, self.manager = _make_manager(persona_name="エア")
        self.addCleanup(self.engine.dispose)

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def tearDown(self):
        os.environ.pop("SAIMEMORY_MEMORY", None)

    def _seed_conversation(self):
        from saiverse_memory import SAIMemoryAdapter
        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter._db_lock:
                thread_id = adapter.conn.execute(
                    "SELECT id FROM threads LIMIT 1"
                ).fetchone()
                if thread_id is None:
                    from sai_memory.memory.storage import get_or_create_thread
                    get_or_create_thread(adapter.conn, "main", resource_id="tester")
                    thread_id = "main"
                else:
                    thread_id = thread_id[0]
                from sai_memory.memory.storage import add_message
                ids = []
                ids.append(add_message(adapter.conn, thread_id=thread_id, role="user", content="ソフィーーーー！！！いけたーーーー！！", resource_id="tester"))
                ids.append(add_message(adapter.conn, thread_id=thread_id, role="model", content="やったね、まはー！", resource_id="tester"))
                ids.append(add_message(adapter.conn, thread_id=thread_id, role="user", content="ずっとこの瞬間を待ってた", resource_id="tester"))
        finally:
            adapter.close()
        return ids

    def test_add_scene_decisive_copy_no_alteration(self):
        from tools.context import persona_context
        ids = self._seed_conversation()
        with persona_context("tester", self.persona_path, self.manager):
            out = self.mod.memory_clip(
                anchor=ids[1], rounds=2, paste_to="core", mode="transcribe",
            )
        self.assertIn("転写しました", out)
        self.assertIn("c:1", out)

        from saiverse_memory import SAIMemoryAdapter
        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter._db_lock:
                items = list_core_memories(adapter.conn)
        finally:
            adapter.close()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "scene")
        self.assertIn("ソフィーーーー！！！いけたーーーー！！", items[0].content)
        self.assertIn("[エア]: やったね、まはー！", items[0].content)

    def test_accepts_uri_form_message_id(self):
        from tools.context import persona_context
        ids = self._seed_conversation()
        uri = f"saiverse://self/message/{ids[1]}"
        with persona_context("tester", self.persona_path, self.manager):
            out = self.mod.memory_clip(
                anchor=uri, rounds=1, paste_to="core", mode="transcribe",
            )
        self.assertIn("転写しました", out)

    def test_missing_message_id_returns_error(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            out = self.mod.memory_clip(
                anchor="no-such-id", rounds=1, paste_to="core", mode="transcribe",
            )
        self.assertIn("見つからないか、実会話ではありません", out)

    def test_assistant_role_openai_style_import_labeled_as_persona(self):
        # 実 DB 実査 (2026-07-04): インポートログでは persona 応答の role が
        # 'assistant' (OpenAI 系呼称) で保存されている。ラベルがペルソナ名になること。
        from saiverse_memory import SAIMemoryAdapter
        from sai_memory.memory.storage import add_message, get_or_create_thread
        from tools.context import persona_context

        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter._db_lock:
                get_or_create_thread(adapter.conn, "imported", resource_id="tester")
                anchor_id = add_message(
                    adapter.conn, thread_id="imported", role="assistant",
                    content="いけたーーーー！！！！！！！", resource_id="tester",
                )
        finally:
            adapter.close()

        with persona_context("tester", self.persona_path, self.manager):
            out = self.mod.memory_clip(
                anchor=anchor_id, rounds=1, paste_to="core", mode="transcribe",
            )
        self.assertIn("転写しました", out)

        adapter2 = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter2._db_lock:
                items = list_core_memories(adapter2.conn)
        finally:
            adapter2.close()
        self.assertIn("[エア]: いけたーーーー！！！！！！！", items[0].content)


class CoreMemorySceneRenderTest(unittest.TestCase):
    """render のコードブロック枠・3要素・snapshot 往復の後方互換。"""

    def test_render_scene_has_codeblock_date_and_past_record_label(self):
        meta = json.dumps({"anchor_id": "m1", "message_ids": ["m1"], "date_range": ["2025-01-12", "2025-01-12"]})
        item = CoreMemoryItem(memory_id=5, content='user「ソフィーーーー！！！いけたーーーー！！」\nエア「……」', kind="scene", metadata=meta)
        snap = CoreMemorySnapshot(items=(item,))
        rendered = CoreMemorySection().render(snap)
        self.assertIsNotNone(rendered)
        text = rendered.text
        self.assertIn("```", text)
        self.assertIn("2025-01-12", text)
        self.assertIn("過去の実際のやり取りの記録", text)
        self.assertIn("ソフィーーーー！！！いけたーーーー！！", text)
        self.assertIn("[c:5]", text)

    def test_render_note_unchanged(self):
        item = CoreMemoryItem(memory_id=1, content="普通のメモ")
        snap = CoreMemorySnapshot(items=(item,))
        rendered = CoreMemorySection().render(snap)
        self.assertIn("- [c:1] 普通のメモ", rendered.text)
        self.assertNotIn("```", rendered.text)

    def test_snapshot_roundtrip_with_scene_kind(self):
        meta = json.dumps({"anchor_id": "m1"})
        item = CoreMemoryItem(memory_id=1, content="user「A」", kind="scene", metadata=meta)
        snap = CoreMemorySnapshot(items=(item,))
        section = CoreMemorySection()
        restored = section.deserialize_snapshot(section.serialize_snapshot(snap))
        self.assertEqual(restored, snap)

    def test_deserialize_old_snapshot_without_kind_metadata_backward_compat(self):
        # 旧 snapshot: kind/metadata キーが無い dict のみ。
        old_payload = json.dumps({"items": [{"memory_id": 1, "content": "旧メモ"}]})
        section = CoreMemorySection()
        restored = section.deserialize_snapshot(old_payload)
        self.assertEqual(restored.items[0].kind, "note")
        self.assertIsNone(restored.items[0].metadata)
        self.assertEqual(restored.items[0].content, "旧メモ")


if __name__ == "__main__":
    unittest.main()
