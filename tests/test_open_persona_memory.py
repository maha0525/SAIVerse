"""memory 系スペルの DB ロック玉突き (P1) 修正の回帰テスト。

対象:
- tools/context.py: open_persona_memory — ロード済みペルソナの live adapter を
  再利用し、新品 SAIMemoryAdapter (= 新 SQLite 接続 + 初期化書き込み) を作らない。
  ペルソナ不在時のフォールバック新品 adapter は with 終了時に close される。
- saiverse_memory/adapter.py: __init__ の startup_backup opt-in —
  デフォルト False ではバックアップスレッドを起動しない。True + env 有効の
  ときだけ起動する (正規の構築点 = persona/bootstrap.py だけが True を渡す)。
- 代表ツール (memory_read / memory_search) が本体 adapter 経由で動くこと。

流儀: 実 DB (一時ディレクトリの memory.db) + Embedder patch は
tests/test_memory_atlas.py、live adapter 再利用の検証は
tests/test_people_get_adapter.py に倣う。ツールは動的ロードなので
tests/tool_loader.py 経由で import する。
"""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.context import open_persona_memory, persona_context


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class _MemoryTestBase(unittest.TestCase):
    """一時ディレクトリに live adapter を立てる共通土台。"""

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

        self.live_adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_dir, resource_id="tester"
        )
        self.addCleanup(self.live_adapter.close)
        self.manager = SimpleNamespace(
            personas={"tester": SimpleNamespace(sai_memory=self.live_adapter)},
        )

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows: SQLite ハンドル解放が rmtree に間に合わないことがある
            pass


class OpenPersonaMemoryHelperTest(_MemoryTestBase):
    def test_live_adapter_reused_without_new_construction(self):
        """ロード済みペルソナがいれば新品 adapter を一切構築しない。"""
        with persona_context("tester", self.persona_dir, manager=self.manager):
            with patch(
                "saiverse_memory.SAIMemoryAdapter",
                side_effect=AssertionError("new SAIMemoryAdapter must not be constructed"),
            ) as adapter_cls:
                with open_persona_memory() as adapter:
                    self.assertIs(adapter, self.live_adapter)
                adapter_cls.assert_not_called()
        # live adapter は with 終了後も閉じられない (所有権はペルソナ側)
        self.assertTrue(self.live_adapter.is_ready())
        self.live_adapter.conn.execute("SELECT 1")

    def test_fallback_adapter_closed_on_exit(self):
        """ロード済みペルソナ不在時のフォールバック新品は with 終了時に close。"""
        ghost_dir = Path(self._tmp.name) / "personas" / "ghost"
        ghost_dir.mkdir(parents=True, exist_ok=True)
        # manager にいないペルソナ ID → フォールバック経路
        with persona_context("ghost", ghost_dir, manager=self.manager):
            with open_persona_memory() as adapter:
                self.assertIsNot(adapter, self.live_adapter)
                self.assertTrue(adapter.is_ready())
                self.assertEqual(adapter.persona_id, "ghost")
                fallback_conn = adapter.conn
        with self.assertRaises(sqlite3.ProgrammingError):
            fallback_conn.execute("SELECT 1")

    def test_fallback_without_manager(self):
        """manager 未注入 (CLI 経路相当) でもフォールバックが動く。"""
        ghost_dir = Path(self._tmp.name) / "personas" / "ghost2"
        ghost_dir.mkdir(parents=True, exist_ok=True)
        with persona_context("ghost2", ghost_dir):
            with open_persona_memory() as adapter:
                self.assertTrue(adapter.is_ready())
                fallback_conn = adapter.conn
        with self.assertRaises(sqlite3.ProgrammingError):
            fallback_conn.execute("SELECT 1")

    def test_raises_without_active_persona(self):
        with self.assertRaises(RuntimeError):
            with open_persona_memory():
                pass


class StartupBackupOptInTest(unittest.TestCase):
    """SAIMemoryAdapter.__init__ の startup_backup opt-in (修正B)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name) / "personas" / "tester"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        self.addCleanup(os.environ.pop, "SAIMEMORY_BACKUP_ON_START", None)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            pass

    def _build(self, **kwargs):
        from saiverse_memory import SAIMemoryAdapter

        adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_dir, resource_id="tester", **kwargs
        )
        self.addCleanup(adapter.close)
        return adapter

    def test_default_does_not_start_backup_thread(self):
        """デフォルト (startup_backup=False) では env が有効でも起動しない。"""
        os.environ["SAIMEMORY_BACKUP_ON_START"] = "1"
        with patch("saiverse_memory.adapter.threading.Thread") as thread_cls:
            self._build()
        thread_cls.assert_not_called()

    def test_opt_in_with_env_enabled_starts_backup_thread(self):
        """startup_backup=True + env 有効で1回だけ起動する (ペルソナ登録経路)。"""
        os.environ["SAIMEMORY_BACKUP_ON_START"] = "1"
        with patch("saiverse_memory.adapter.threading.Thread") as thread_cls:
            adapter = self._build(startup_backup=True)
        thread_cls.assert_called_once()
        _, kwargs = thread_cls.call_args
        self.assertEqual(kwargs.get("target"), adapter._run_startup_backup)
        self.assertTrue(kwargs.get("daemon"))
        thread_cls.return_value.start.assert_called_once()

    def test_opt_in_respects_env_disabled(self):
        """startup_backup=True でも env 無効なら起動しない (AND 条件)。"""
        os.environ["SAIMEMORY_BACKUP_ON_START"] = "0"
        with patch("saiverse_memory.adapter.threading.Thread") as thread_cls:
            self._build(startup_backup=True)
        thread_cls.assert_not_called()


class MemoryToolsUseLiveAdapterTest(_MemoryTestBase):
    """代表ツールが本体 adapter 経由で動く (新品 adapter を構築しない)。"""

    def test_memory_read_uses_live_adapter(self):
        from tool_loader import load_builtin_tool

        memory_read = load_builtin_tool("memory_read").memory_read
        with persona_context("tester", self.persona_dir, manager=self.manager):
            with patch(
                "saiverse_memory.SAIMemoryAdapter",
                side_effect=AssertionError("new SAIMemoryAdapter must not be constructed"),
            ) as adapter_cls:
                result = memory_read("core")
                adapter_cls.assert_not_called()
        self.assertIn("コア記憶", result)
        # live adapter は閉じられていない
        self.assertTrue(self.live_adapter.is_ready())
        self.live_adapter.conn.execute("SELECT 1")

    def test_memory_search_uses_live_adapter(self):
        from tool_loader import load_builtin_tool

        memory_search = load_builtin_tool("memory_search").memory_search
        with persona_context("tester", self.persona_dir, manager=self.manager):
            with patch(
                "saiverse_memory.SAIMemoryAdapter",
                side_effect=AssertionError("new SAIMemoryAdapter must not be constructed"),
            ) as adapter_cls:
                result = memory_search("存在しない検索語")
                adapter_cls.assert_not_called()
        self.assertIn("見つかりませんでした", result)
        self.assertTrue(self.live_adapter.is_ready())


if __name__ == "__main__":
    unittest.main()
