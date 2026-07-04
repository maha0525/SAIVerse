"""コア記憶スペル (core_memory_add / update / remove) のテスト。

動的ロードのツールなので tool_loader.load_builtin_tool で読み込み、SAIMemoryAdapter
の Embedder は DummyEmbedder に patch する (Windows SQLite locking 対策で
addCleanup を LIFO 登録)。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User


class DummyEmbedder:
    def __init__(self, model=None, **kwargs) -> None:
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def _make_manager(core_budget=None):
    """CORE_MEMORY_CHAR_BUDGET を持つ AI レコード付きの疑似 manager を返す。"""
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
        db.add(AI(
            AIID="tester", HOME_CITYID=city.CITYID, AINAME="Tester",
            CORE_MEMORY_CHAR_BUDGET=core_budget,
        ))
        db.commit()
    finally:
        db.close()
    return engine, SimpleNamespace(SessionLocal=Session)


class CoreMemoryToolsTest(unittest.TestCase):
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

        self.add_mod = load_builtin_tool("core_memory_add")
        self.update_mod = load_builtin_tool("core_memory_update")
        self.remove_mod = load_builtin_tool("core_memory_remove")

        # 既定 (budget 未設定 → 2000) の manager。個別テストで差し替え可。
        self.engine, self.manager = _make_manager(core_budget=None)
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

    def _read_all(self):
        from saiverse_memory import SAIMemoryAdapter
        from sai_memory.core_memory import list_core_memories
        adapter = SAIMemoryAdapter("tester", persona_dir=self.persona_path, resource_id="tester")
        try:
            with adapter._db_lock:
                return list_core_memories(adapter.conn)
        finally:
            adapter.close()

    def test_add_success(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            out = self.add_mod.core_memory_add(content="まはーの誕生日は1月14日")
        self.assertIn("c:1", out)
        self.assertIn("追加しました", out)
        self.assertIn("目安 2,000 字", out)  # 既定 budget
        items = self._read_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "まはーの誕生日は1月14日")

    def test_add_empty_rejected(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            out = self.add_mod.core_memory_add(content="   ")
        self.assertIn("Error", out)
        self.assertEqual(self._read_all(), [])

    def test_update_accepts_c_prefix_and_bare(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            self.add_mod.core_memory_add(content="旧い内容")
            # c:1 形式
            out1 = self.update_mod.core_memory_update(memory_id="c:1", content="新しい内容A")
            # 数字だけ形式
            out2 = self.update_mod.core_memory_update(memory_id="1", content="新しい内容B")
        self.assertIn("c:1", out1)
        self.assertIn("書き換えました", out1)
        self.assertIn("書き換えました", out2)
        self.assertEqual(self._read_all()[0].content, "新しい内容B")

    def test_update_missing_id(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            out = self.update_mod.core_memory_update(memory_id="c:999", content="x")
        self.assertIn("見つかりません", out)

    def test_update_bad_id_format(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            out = self.update_mod.core_memory_update(memory_id="abc", content="x")
        self.assertIn("形式が正しくありません", out)

    def test_remove_success_and_missing(self):
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, self.manager):
            self.add_mod.core_memory_add(content="消す対象")
            out_ok = self.remove_mod.core_memory_remove(memory_id="c:1")
            out_missing = self.remove_mod.core_memory_remove(memory_id="c:1")
        self.assertIn("削除しました", out_ok)
        self.assertIn("見つかりません", out_missing)
        self.assertEqual(self._read_all(), [])

    def test_budget_exceeded_notice_but_succeeds(self):
        # budget=10 の小さな manager に差し替え。11 字を刻むと超過通知が出るが成功する。
        engine, manager = _make_manager(core_budget=10)
        self.addCleanup(engine.dispose)
        from tools.context import persona_context
        with persona_context("tester", self.persona_path, manager):
            out = self.add_mod.core_memory_add(content="あ" * 11)
        # 操作は成功 (切り詰め・拒否はしない)。
        self.assertIn("追加しました", out)
        self.assertIn("目安 10 字", out)
        self.assertIn("目安を超えています", out)
        # 内容は切り詰められず 11 字そのまま。
        self.assertEqual(len(self._read_all()[0].content), 11)


if __name__ == "__main__":
    unittest.main()
