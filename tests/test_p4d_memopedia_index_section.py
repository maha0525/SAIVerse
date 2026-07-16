"""P4-d head 目次（MemopediaIndexSection）の単体テスト。

検証スコープ:
1. flag OFF → capture は index_enabled=False を返し、render は None
2. flag ON → capture は toc_markdown を持ち、render はテキストを返す
3. TOC に summary が含まれる（2026-07-14: 旧方式の後方互換復元。
   P4-d 実装時に summary 非表示へすり替わっていた回帰の修正）
4. ★(is_important) マーカーが出る
5. diff 通知（capture_changes_since）
6. serialize / deserialize のラウンドトリップ
7. MemoryWeaveSection が Memopedia 索引に一切関与しないことを確認
   (2026-07-14: get_memory_weave_context から include_memopedia 引数ごと削除)
"""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_mem_conn() -> sqlite3.Connection:
    from sai_memory.memopedia import init_memopedia_tables
    conn = sqlite3.connect(":memory:")
    init_memopedia_tables(conn)
    return conn


def _make_ctx(index_enabled: bool, mem_conn=None):
    """LineHeadInput 相当の fake を返す。"""
    class FakeSAIMem:
        conn = None

    class FakePersona:
        sai_memory = FakeSAIMem()

    persona = FakePersona()
    if mem_conn is not None:
        persona.sai_memory.conn = mem_conn

    class FakeAI:
        MEMOPEDIA_INDEX_ENABLED = index_enabled

    class FakeQuery:
        def filter_by(self, **kwargs):
            return self

        def first(self):
            return FakeAI()

    class FakeDB:
        def query(self, *args, **kwargs):
            return FakeQuery()

        def close(self):
            pass

    class FakeManager:
        def SessionLocal(self):
            return FakeDB()

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.manager = FakeManager()
    ctx.persona_id = "test_persona"
    ctx.persona = persona
    return ctx


# ---------------------------------------------------------------------------
# 1. flag OFF → render None
# ---------------------------------------------------------------------------

class MemopediaIndexSectionOffTest(unittest.TestCase):
    def test_capture_returns_index_enabled_false_when_off(self):
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=False, mem_conn=_make_mem_conn())
        snap = section.capture(ctx)
        self.assertFalse(snap.index_enabled)
        self.assertIsNone(snap.toc_markdown)

    def test_render_returns_none_when_off(self):
        from sea.head_pipeline.sections.memopedia_index import (
            MemopediaIndexSection,
            MemopediaIndexSnapshot,
        )
        section = MemopediaIndexSection()
        snap = MemopediaIndexSnapshot(
            captured_at=time.time(), pages=(), index_enabled=False, toc_markdown=None
        )
        self.assertIsNone(section.render(snap))


# ---------------------------------------------------------------------------
# 2. flag ON → render non-None
# ---------------------------------------------------------------------------

class MemopediaIndexSectionOnTest(unittest.TestCase):
    def setUp(self):
        self.mem_conn = _make_mem_conn()
        from sai_memory.memopedia import Memopedia
        mem = Memopedia(self.mem_conn)
        mem.create_page(
            parent_id="root_people",
            title="テスト人物",
            summary="テスト概要",
            content="テスト本文",
        )
        self.mem_conn.commit()

    def test_capture_returns_toc_when_on(self):
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=True, mem_conn=self.mem_conn)
        snap = section.capture(ctx)
        self.assertTrue(snap.index_enabled)
        self.assertIsNotNone(snap.toc_markdown)
        self.assertIn("テスト人物", snap.toc_markdown)

    def test_render_returns_rendered_section_when_on(self):
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=True, mem_conn=self.mem_conn)
        snap = section.capture(ctx)
        rendered = section.render(snap)
        self.assertIsNotNone(rendered)
        self.assertIn("テスト人物", rendered.text)

    def test_toc_contains_summary_text(self):
        """2026-07-14 裁定: トグルは旧方式 (summary あり) への後方互換のため、
        summary を表示する。"""
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=True, mem_conn=self.mem_conn)
        snap = section.capture(ctx)
        self.assertIn("テスト概要", snap.toc_markdown or "")


# ---------------------------------------------------------------------------
# 3. ★(is_important) マーカー
# ---------------------------------------------------------------------------

class ImportantMarkerTest(unittest.TestCase):
    def test_important_page_has_star_marker(self):
        mem_conn = _make_mem_conn()
        from sai_memory.memopedia import Memopedia
        mem = Memopedia(mem_conn)
        page = mem.create_page(
            parent_id="root_terms",
            title="重要コンセプト",
            summary="very important",
            content="本文",
        )
        mem.set_important(page.id, True)
        mem_conn.commit()

        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=True, mem_conn=mem_conn)
        snap = section.capture(ctx)
        self.assertIsNotNone(snap.toc_markdown)
        self.assertIn("★", snap.toc_markdown)

    def test_desk_open_page_has_open_marker(self):
        """[OPEN] は机 (desk_items) 基準 (検収修正 2026-07-11)。

        get_tree の is_open (旧 PageState、thread 単位) は thread_id=None で
        常に False のため、素通しでは [OPEN] が永遠に点かなかった。目次の
        「開いている」は机の実体 (desk_items) を見る。
        """
        from sai_memory import desk as desk_mod

        mem_conn = _make_mem_conn()
        from sai_memory.memopedia import Memopedia
        mem = Memopedia(mem_conn)
        page = mem.create_page(
            parent_id="root_terms", title="机の上のページ", summary="s", content="本文",
        )
        desk_mod.init_desk_tables(mem_conn)
        desk_mod.open_item(mem_conn, f"memopedia:{page.short_id}")
        mem_conn.commit()

        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=True, mem_conn=mem_conn)
        snap = section.capture(ctx)
        self.assertIsNotNone(snap.toc_markdown)
        self.assertIn("[OPEN]", snap.toc_markdown)
        self.assertIn("机の上のページ", snap.toc_markdown)


# ---------------------------------------------------------------------------
# 4. capture_changes_since (diff 通知基盤)
# ---------------------------------------------------------------------------

class CaptureChangesSinceTest(unittest.TestCase):
    def test_detects_newly_created_page(self):
        mem_conn = _make_mem_conn()
        since = time.time() - 1  # 1秒前を基準に

        # since より後にページを作る
        from sai_memory.memopedia import Memopedia
        mem = Memopedia(mem_conn)
        mem.create_page(
            parent_id="root_terms",
            title="新しいページ",
            summary="",
            content="",
        )
        mem_conn.commit()

        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        ctx = _make_ctx(index_enabled=False, mem_conn=mem_conn)
        snap = section.capture_changes_since(ctx, since)
        titles = [p.title for p in snap.pages]
        self.assertIn("新しいページ", titles)


# ---------------------------------------------------------------------------
# 5. serialize / deserialize ラウンドトリップ
# ---------------------------------------------------------------------------

class SerializeRoundtripTest(unittest.TestCase):
    def test_roundtrip_with_toc(self):
        from sea.head_pipeline.sections.memopedia_index import (
            MemopediaIndexSection,
            MemopediaIndexSnapshot,
            MemopediaPageEntry,
        )
        section = MemopediaIndexSection()
        entry = MemopediaPageEntry(
            page_id="pg1",
            title="タイトル",
            created_at=1000,
            updated_at=2000,
            is_deleted=False,
        )
        snap = MemopediaIndexSnapshot(
            captured_at=12345.0,
            pages=(entry,),
            index_enabled=True,
            toc_markdown="### テスト\n- [-] **ページ**",
        )
        data = section.serialize_snapshot(snap)
        restored = section.deserialize_snapshot(data)
        self.assertEqual(restored.captured_at, 12345.0)
        self.assertTrue(restored.index_enabled)
        self.assertIn("テスト", restored.toc_markdown)
        self.assertEqual(len(restored.pages), 1)
        self.assertEqual(restored.pages[0].title, "タイトル")

    def test_roundtrip_without_toc(self):
        from sea.head_pipeline.sections.memopedia_index import (
            MemopediaIndexSection,
            MemopediaIndexSnapshot,
        )
        section = MemopediaIndexSection()
        snap = MemopediaIndexSnapshot(
            captured_at=999.0,
            pages=(),
            index_enabled=False,
            toc_markdown=None,
        )
        data = section.serialize_snapshot(snap)
        restored = section.deserialize_snapshot(data)
        self.assertFalse(restored.index_enabled)
        self.assertIsNone(restored.toc_markdown)

    def test_legacy_snapshot_without_p4d_fields(self):
        """P4-d 以前の snapshot JSON（index_enabled / toc_markdown 無し）を
        deserialize してもクラッシュしない（後方互換）。"""
        from sea.head_pipeline.sections.memopedia_index import MemopediaIndexSection
        section = MemopediaIndexSection()
        legacy = json.dumps({"captured_at": 500.0, "pages": []}, ensure_ascii=False)
        restored = section.deserialize_snapshot(legacy)
        self.assertFalse(restored.index_enabled)
        self.assertIsNone(restored.toc_markdown)


# ---------------------------------------------------------------------------
# 6. MemoryWeaveSection は Memopedia 索引に一切関与しない (P4-d 移行)
# ---------------------------------------------------------------------------

class MemoryWeaveP4dMigrationTest(unittest.TestCase):
    """P4-d: MemoryWeaveSection の capture が get_memory_weave_context を
    include_memopedia 引数なしで呼ぶことを確認する。

    2026-07-14: get_memory_weave_context から include_memopedia 引数自体を
    削除した (Memopedia 索引描画は MemopediaIndexSection に一本化済みで、
    この引数は死にコードだったため)。本テストはその整合を検証する。
    """

    def test_weave_capture_calls_get_memory_weave_context_without_include_memopedia(self):
        from sea.head_pipeline.sections.memory_weave import MemoryWeaveSection

        section = MemoryWeaveSection()

        class FakeDB:
            def query(self, *a, **kw):
                class Q:
                    def filter_by(self, **kw):
                        return self
                    def first(self):
                        class AI:
                            MEMORY_WEAVE_CONTEXT = True
                        return AI()
                return Q()
            def close(self):
                pass

        class FakeManager:
            def SessionLocal(self):
                return FakeDB()

        class FakeSAIMem:
            persona_dir = None
            conn = None

        class FakeHistoryMgr:
            metabolism_anchor_message_id = None

        class FakePersona:
            sai_memory = FakeSAIMem()
            history_manager = FakeHistoryMgr()

        class FakeCtx:
            persona_id = "test_persona"
            persona = FakePersona()
            manager = FakeManager()

        called_args = {}

        def fake_get_memory_weave_context(**kwargs):
            called_args.update(kwargs)
            return []

        # get_memory_weave_context は capture 内で関数スコープ import されるため、
        # import 元のモジュール側をパッチする
        with patch(
            "builtin_data.tools.get_memory_weave_context.get_memory_weave_context",
            side_effect=fake_get_memory_weave_context,
        ), patch(
            "tools.context.persona_context"
        ) as mock_ctx:
            mock_ctx.return_value.__enter__ = lambda s: s
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            section.capture(FakeCtx())

        # 2026-07-14: include_memopedia 引数はもう存在しないため、渡されないことを確認
        self.assertNotIn("include_memopedia", called_args)


if __name__ == "__main__":
    unittest.main()
