"""層1マーカーの保存経路テスト (life_concept_map.md §9.1 / P3)。

対象:
- SEARuntime._store_memory (sea/runtime.py): ペルソナ生成テキスト (assistant)
  の SAIMemory 永続化点。マーカー剥離 + photos テーブルへの観測点 (点写真) 保存を検証
- SAIMemoryAdapter: photos テーブルの init 配線と add_photos API
- RuntimeEmitters.emit_say (sea/runtime_emitters.py): 建物履歴 (表示系シンク)
  への剥離のみ (mark は作らない)

実 DB (一時ディレクトリの memory.db) を使い、Embedder だけ patch する
(tests/test_core_memory_scene.py の流儀)。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sai_memory.photos import list_photos


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class StoreMemoryMarkerTest(unittest.TestCase):
    """_store_memory: (a) 保存本文からマーカーが剥がれる (b) mark 行が生まれる。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name) / "personas" / "tester"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter
        from sea.runtime import SEARuntime

        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_dir, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)
        self.runtime = SEARuntime(SimpleNamespace(building_histories={}))
        self.persona = SimpleNamespace(
            persona_id="tester",
            sai_memory=self.adapter,
            current_building_id=None,
        )

    def _cleanup_temp(self):
        import gc
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows では SQLite ハンドル解放のタイミング次第で rmdir が
            # WinError 145 (not empty) を間欠で返す (PermissionError では
            # なく素の OSError)。一時ディレクトリの取り残しは OS に任せる。
            pass

    def _stored_content(self, message_id: str) -> str:
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT content FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def test_assistant_text_is_stripped_and_marks_saved(self):
        mid = self.runtime._store_memory(
            self.persona,
            "今日は==言葉の標本==(task:3)を見つけた。==夕方の音==も良かった。",
            role="assistant",
            return_message_id=True,
        )
        self.assertTrue(mid)

        # (a) 保存本文からマーカー記法が剥がれている
        content = self._stored_content(mid)
        self.assertEqual(content, "今日は言葉の標本を見つけた。夕方の音も良かった。")
        self.assertNotIn("==", content)

        # (b) 点写真が message_id・quote・purpose_ref 付きで生まれている
        with self.adapter._db_lock:
            photos = list_photos(self.adapter.conn, message_id=mid)
        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0].quote, "言葉の標本")
        self.assertEqual(photos[0].purpose_ref, "task:3")
        self.assertEqual(photos[1].quote, "夕方の音")
        self.assertIsNone(photos[1].purpose_ref)
        # quote は保存本文からの逐語引用として成立する (引用アンカー §9.1)
        for photo in photos:
            self.assertIn(photo.quote, content)

    def test_plain_assistant_text_is_untouched_and_no_marks(self):
        text = "マーカーの無い普通の発言。"
        mid = self.runtime._store_memory(
            self.persona, text, role="assistant", return_message_id=True
        )
        self.assertTrue(mid)
        self.assertEqual(self._stored_content(mid), text)
        with self.adapter._db_lock:
            self.assertEqual(list_photos(self.adapter.conn, message_id=mid), [])

    def test_non_assistant_role_is_not_processed(self):
        # spell 結果等は role="user" で保存される。マーカー処理の対象外
        # (層1はペルソナ自身の生成テキストに打たれる §9.1)
        text = "[結果] a ==b== c"
        mid = self.runtime._store_memory(
            self.persona, text, role="user", return_message_id=True
        )
        self.assertTrue(mid)
        self.assertEqual(self._stored_content(mid), text)
        with self.adapter._db_lock:
            self.assertEqual(list_photos(self.adapter.conn, message_id=mid), [])

    def test_photos_table_initialized_by_adapter(self):
        # adapter __init__ が init_photos_tables を配線している (再オープンも冪等)
        with self.adapter._db_lock:
            row = self.adapter.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='photos'"
            ).fetchone()
        self.assertIsNotNone(row)


class EmitSayStripTest(unittest.TestCase):
    """emit_say (表示系シンク): 建物履歴 payload からマーカーが剥がれる。"""

    def test_emit_say_strips_markers_from_building_payload(self):
        from sea.runtime import SEARuntime

        manager = SimpleNamespace(
            building_histories={"b1": []},
            occupants={"b1": ["pid"]},
            user_presence_status="offline",
            gateway_handle_ai_replies=Mock(),
            unity_gateway=None,
        )
        runtime = SEARuntime(manager)
        history_manager = SimpleNamespace(add_to_building_only=Mock(return_value=None))
        persona = SimpleNamespace(persona_id="pid", history_manager=history_manager)

        runtime._emit_say(persona, "b1", "見て、==これ==(task:1)だよ")

        history_manager.add_to_building_only.assert_called_once()
        payload = history_manager.add_to_building_only.call_args.args[1]
        self.assertEqual(payload["content"], "見て、これだよ")
        # gateway にも剥離済みの本文が流れる
        gateway_args = manager.gateway_handle_ai_replies.call_args.args
        self.assertEqual(gateway_args[2], ["見て、これだよ"])


if __name__ == "__main__":
    unittest.main()
