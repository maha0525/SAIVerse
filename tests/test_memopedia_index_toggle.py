"""MEMOPEDIA_INDEX_ENABLED 後方互換トグルの回帰テスト。

記憶アーキv2 §7.1 (2026-07-04) で Memopedia 索引の head 常時掲示は廃止されたが、
per-persona トグル ``MEMOPEDIA_INDEX_ENABLED`` (database/models.py) で旧方式を
復活できるようにした (後方互換)。

このテストは2つの関所を検証する:
1. ``get_memory_weave_context(include_memopedia=...)`` — capture が呼ぶ実処理側。
   ON/OFF で memopedia entry の有無が切り替わることを直接確認する。
2. ``MemoryWeaveSection._resolve_memopedia_index_enabled`` — DB 列の解決ロジック。
   AI レコードの値をそのまま bool として読み出せることを確認する。

``_compose_messages`` (sea/head_pipeline/integration.py) は snapshot.entries を
kind 問わず展開する汎用実装であり、memopedia 固有の分岐は無い (8fb7449 でも
残存を確認済み)。そのため本テストでは capture 側の memopedia entry 生成のみを
対象とし、composition 側は別途 test_memory_weave_track_dump.py 等の既存カバレッジ
に委ねる。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
from sai_memory.memopedia import Memopedia, init_memopedia_tables


class MemopediaIndexToggleTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.persona_dir = self._tmpdir.name
        self.db_path = Path(self.persona_dir) / "memory.db"

        conn = sqlite3.connect(str(self.db_path))
        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        memopedia.create_page(
            parent_id="root_terms",
            title="テストページ",
            summary="テスト用の概要",
            content="テスト用の本文",
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_include_memopedia_false_omits_memopedia_entry(self):
        messages = get_memory_weave_context(
            persona_id="test_persona",
            persona_dir=self.persona_dir,
            include_memopedia=False,
        )
        kinds = {m["metadata"]["__memory_weave_type__"] for m in messages}
        self.assertNotIn("memopedia", kinds)

    def test_include_memopedia_true_includes_memopedia_entry(self):
        messages = get_memory_weave_context(
            persona_id="test_persona",
            persona_dir=self.persona_dir,
            include_memopedia=True,
        )
        memopedia_messages = [
            m for m in messages
            if m["metadata"]["__memory_weave_type__"] == "memopedia"
        ]
        self.assertEqual(len(memopedia_messages), 1)
        self.assertIn("テストページ", memopedia_messages[0]["content"])
        self.assertIn("テスト用の概要", memopedia_messages[0]["content"])

    def test_default_include_memopedia_is_false(self):
        # デフォルト引数のみで呼んだ場合、記憶アーキv2 §7.1 の既定 (廃止) に従う。
        messages = get_memory_weave_context(
            persona_id="test_persona",
            persona_dir=self.persona_dir,
        )
        kinds = {m["metadata"]["__memory_weave_type__"] for m in messages}
        self.assertNotIn("memopedia", kinds)


class ResolveMemopediaIndexEnabledTest(unittest.TestCase):
    """MemoryWeaveSection._resolve_memopedia_index_enabled の DB 列解決を検証する。"""

    def _make_section(self):
        from sea.head_pipeline.sections.memory_weave import MemoryWeaveSection
        return MemoryWeaveSection()

    def test_resolves_true_when_column_set(self):
        section = self._make_section()

        class FakeAI:
            MEMOPEDIA_INDEX_ENABLED = True

        class FakeQuery:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return FakeAI()

        class FakeDB:
            def query(self, *_args, **_kwargs):
                return FakeQuery()

            def close(self):
                pass

        class FakeManager:
            def SessionLocal(self):
                return FakeDB()

        self.assertTrue(
            section._resolve_memopedia_index_enabled(FakeManager(), "test_persona")
        )

    def test_resolves_false_by_default(self):
        section = self._make_section()

        class FakeAI:
            MEMOPEDIA_INDEX_ENABLED = False

        class FakeQuery:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return FakeAI()

        class FakeDB:
            def query(self, *_args, **_kwargs):
                return FakeQuery()

            def close(self):
                pass

        class FakeManager:
            def SessionLocal(self):
                return FakeDB()

        self.assertFalse(
            section._resolve_memopedia_index_enabled(FakeManager(), "test_persona")
        )

    def test_resolves_false_when_persona_not_found(self):
        section = self._make_section()

        class FakeQuery:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return None

        class FakeDB:
            def query(self, *_args, **_kwargs):
                return FakeQuery()

            def close(self):
                pass

        class FakeManager:
            def SessionLocal(self):
                return FakeDB()

        self.assertFalse(
            section._resolve_memopedia_index_enabled(FakeManager(), "test_persona")
        )


if __name__ == "__main__":
    unittest.main()
