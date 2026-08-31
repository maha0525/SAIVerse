"""MEMOPEDIA_INDEX_ENABLED トグルの回帰テスト。

記憶アーキv2 §7.1 (2026-07-04) で Memopedia 索引の head 常時掲示は廃止されたが、
per-persona トグル ``MEMOPEDIA_INDEX_ENABLED`` (database/models.py) で
MemopediaIndexSection (P4-d) が目次を head に render できる。このトグルは
「Memopedia 全ページ一覧の常時表示（旧方式）」への後方互換として作られたもの。

P4-d (2026-07-11) でフラグの解決先を MemoryWeaveSection → MemopediaIndexSection
に一本化した際、旧経路 ``get_memory_weave_context`` の ``include_memopedia`` /
``_get_memopedia_context`` (summary あり・深さ制限なし) が本番未使用の死にコード
として残っていた。2026-07-14 にこれらを削除し、``MemopediaIndexSection`` の
目次描画を summary あり・深さ制限なしの旧方式相当へ修正した（[OPEN]/★/件数の
P4-d 改善は残す）。旧経路を直接叩いていた本ファイルのテストは
``tests/test_p4d_memopedia_index_section.py`` へ実質的に統合済みのため、
本ファイルは以下2つの関所のみを検証する:

1. ``get_memory_weave_context`` — Memopedia 索引に一切関与しないこと
   (include_memopedia 引数はもう存在しない。渡すと TypeError になる)。
2. ``MemopediaIndexSection._resolve_memopedia_index_enabled`` — DB 列の解決ロジック。
   AI レコードの値をそのまま bool として読み出せることを確認する。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from builtin_data.tools.get_memory_weave_context import get_memory_weave_context
from sai_memory.memopedia import Memopedia, init_memopedia_tables


class MemopediaIndexNotInWeaveContextTest(unittest.TestCase):
    """get_memory_weave_context は Memopedia 索引の掲示にもう一切関与しない
    (2026-07-14: P4-d 一本化に伴い include_memopedia 引数ごと削除)。"""

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

    def test_no_memopedia_kind_in_messages(self):
        messages = get_memory_weave_context(
            persona_id="test_persona",
            persona_dir=self.persona_dir,
        )
        kinds = {m["metadata"]["__memory_weave_type__"] for m in messages}
        self.assertNotIn("memopedia", kinds)

    def test_include_memopedia_kwarg_no_longer_accepted(self):
        """旧引数 include_memopedia は削除済み。渡すと TypeError になることを
        明示的に確認する (呼び出し元が誤って復活させないための回帰)。"""
        with self.assertRaises(TypeError):
            get_memory_weave_context(
                persona_id="test_persona",
                persona_dir=self.persona_dir,
                include_memopedia=True,
            )


class ResolveMemopediaIndexEnabledTest(unittest.TestCase):
    """MemopediaIndexSection._resolve_memopedia_index_enabled の DB 列解決を検証する。

    P4-d: フラグ解決ロジックは MemoryWeaveSection から MemopediaIndexSection に移った。
    """

    def _make_section(self):
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        return MemopediaIndexSection()

    def _make_ctx(self, manager, persona_id):
        """LineHeadInput の最小 duck-type mock を返す。"""
        class FakeCtx:
            pass

        ctx = FakeCtx()
        ctx.manager = manager
        ctx.persona_id = persona_id
        ctx.persona = None
        return ctx

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

        ctx = self._make_ctx(FakeManager(), "test_persona")
        self.assertTrue(section._resolve_memopedia_index_enabled(ctx))

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

        ctx = self._make_ctx(FakeManager(), "test_persona")
        self.assertFalse(section._resolve_memopedia_index_enabled(ctx))

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

        ctx = self._make_ctx(FakeManager(), "test_persona")
        self.assertFalse(section._resolve_memopedia_index_enabled(ctx))


if __name__ == "__main__":
    unittest.main()
