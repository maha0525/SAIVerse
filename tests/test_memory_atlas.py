"""Memory Atlas ファサード saiverse/memory_atlas.py のテスト (concept_consolidation.md)。

検証項目:
- ref 解決 (m:N / core / c:N / ch:N / task:N stub / 不正形式)
- read_page: Memopedia / コア記憶(全件・1件) / Chronicle の内容整形、貼られた写真の表示
- open_page/close_page: コア記憶は常時開で拒否、Memopedia/Chronicle は desk.py に委譲、
  机が溢れたときの LRU 追い出し通知
- search_pages: Memopedia + Chronicle 横断検索、0 件フォールバック
- Chronicle の short_id backfill (legacy DB からの一回きり移行、新規行の自動採番)

実 DB (一時ディレクトリの memory.db) を使い、Embedder だけ patch する
(tests/test_marker_store_memory.py の流儀)。
"""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from saiverse import clock
from saiverse import memory_atlas as atlas


class DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


class _AtlasTestBase(unittest.TestCase):
    """SAIMemoryAdapter を実 DB (一時ディレクトリ) で立てる共通土台。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self._tmp.name) / "personas" / "tester"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        os.environ["SAIMEMORY_MEMORY"] = "1"
        self.addCleanup(self._cleanup_temp)
        self.addCleanup(os.environ.pop, "SAIMEMORY_MEMORY", None)
        self.addCleanup(os.environ.pop, "SAIVERSE_DESK_BUDGET_CHARS", None)
        self.addCleanup(clock.disable_virtual)

        patcher = patch("saiverse_memory.adapter.Embedder", DummyEmbedder)
        self.addCleanup(patcher.stop)
        patcher.start()

        from saiverse_memory import SAIMemoryAdapter

        self.adapter = SAIMemoryAdapter(
            "tester", persona_dir=self.persona_dir, resource_id="tester"
        )
        self.addCleanup(self.adapter.close)

    def _cleanup_temp(self):
        gc.collect()
        try:
            self._tmp.cleanup()
        except OSError:
            # Windows: SQLite ハンドル解放タイミング次第で rmdir が
            # WinError 145 を間欠で返す。取り残しは OS に任せる。
            pass

    def _make_memopedia_page(self, title="用語A", content="本文A"):
        from sai_memory.memopedia import Memopedia

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        return memopedia.create_page(parent_id="root_terms", title=title, content=content)

    def _make_chronicle_entry(self, content="1日目のできごと"):
        from sai_memory.arasuji.storage import create_entry, init_arasuji_tables

        # SAIMemoryAdapter は Memopedia/core_memory と違い arasuji テーブルを
        # __init__ で eager 初期化しない (既存の遅延初期化パターン)。
        init_arasuji_tables(self.adapter.conn)
        return create_entry(
            self.adapter.conn, level=1, content=content, source_ids=["msg1"],
            source_count=1, message_count=1,
        )


class RefParsingTests(unittest.TestCase):
    def test_parses_memopedia_ref(self):
        self.assertEqual(atlas._parse_ref("m:5"), ("memopedia", "5"))

    def test_parses_core_all(self):
        self.assertEqual(atlas._parse_ref("core"), ("core_all", None))
        self.assertEqual(atlas._parse_ref("CORE"), ("core_all", None))

    def test_parses_core_one(self):
        self.assertEqual(atlas._parse_ref("c:3"), ("core_one", "3"))

    def test_parses_chronicle_ref_distinct_from_core(self):
        # "ch:" は "c:" と衝突しない (2 文字プレフィックスの境界)
        self.assertEqual(atlas._parse_ref("ch:7"), ("chronicle", "7"))

    def test_parses_task_stub_ref(self):
        self.assertEqual(atlas._parse_ref("task:2"), ("task", "2"))

    def test_unrecognized_ref_raises(self):
        with self.assertRaises(atlas.AtlasRefError):
            atlas._parse_ref("unknown:1")

    def test_empty_ref_raises(self):
        with self.assertRaises(atlas.AtlasRefError):
            atlas._parse_ref("")


class ReadPageTests(_AtlasTestBase):
    def test_read_core_empty(self):
        result = atlas.read_page(self.adapter, "core")
        self.assertIn("コア記憶はまだありません", result)

    def test_read_core_all_lists_every_item(self):
        from sai_memory.core_memory import add_core_memory

        add_core_memory(self.adapter.conn, "1件目")
        add_core_memory(self.adapter.conn, "2件目")
        result = atlas.read_page(self.adapter, "core")
        self.assertIn("c:1", result)
        self.assertIn("1件目", result)
        self.assertIn("c:2", result)
        self.assertIn("2件目", result)

    def test_read_core_one_found(self):
        from sai_memory.core_memory import add_core_memory

        new_id = add_core_memory(self.adapter.conn, "刻んだ内容")
        result = atlas.read_page(self.adapter, f"c:{new_id}")
        self.assertIn("刻んだ内容", result)
        self.assertIn(f"c:{new_id}", result)

    def test_read_core_one_not_found(self):
        result = atlas.read_page(self.adapter, "c:999")
        self.assertIn("見つかりません", result)

    def test_read_memopedia_page(self):
        page = self._make_memopedia_page(title="桃太郎", content="鬼退治に行った")
        result = atlas.read_page(self.adapter, f"m:{page.short_id}")
        self.assertIn("桃太郎", result)
        self.assertIn("鬼退治に行った", result)
        self.assertIn(f"m:{page.short_id}", result)

    def test_read_memopedia_page_not_found(self):
        result = atlas.read_page(self.adapter, "m:999")
        self.assertIn("見つかりません", result)

    def test_read_chronicle_entry(self):
        entry = self._make_chronicle_entry(content="祭りに参加した")
        result = atlas.read_page(self.adapter, f"ch:{entry.short_id}")
        self.assertIn("祭りに参加した", result)
        self.assertIn(f"ch:{entry.short_id}", result)

    def test_read_chronicle_entry_not_found(self):
        result = atlas.read_page(self.adapter, "ch:999")
        self.assertIn("見つかりません", result)

    def test_read_task_returns_p2b_stub(self):
        result = atlas.read_page(self.adapter, "task:1")
        self.assertIn("P2b", result)

    def test_read_page_shows_pasted_photos(self):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.photos import add_photo

        new_id = add_core_memory(self.adapter.conn, "写真つきの記憶")
        ref = f"c:{new_id}"
        add_photo(self.adapter.conn, message_id="m1", quote="根拠の一言", pasted_to=ref)

        result = atlas.read_page(self.adapter, ref)
        self.assertIn("[写真]", result)
        self.assertIn("根拠の一言", result)

    def test_read_page_without_photos_omits_photo_section(self):
        from sai_memory.core_memory import add_core_memory

        new_id = add_core_memory(self.adapter.conn, "写真なし")
        result = atlas.read_page(self.adapter, f"c:{new_id}")
        self.assertNotIn("[写真]", result)


class OpenClosePageTests(_AtlasTestBase):
    def test_open_core_all_is_rejected(self):
        result = atlas.open_page(self.adapter, "core")
        self.assertIn("常時開いています", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_core_one_is_rejected(self):
        result = atlas.open_page(self.adapter, "c:1")
        self.assertIn("常時開いています", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_core_is_rejected(self):
        result = atlas.close_page(self.adapter, "c:1")
        self.assertIn("閉じられません", result)

    def test_open_memopedia_page_registers_desk_item(self):
        page = self._make_memopedia_page()
        result = atlas.open_page(self.adapter, f"m:{page.short_id}")
        self.assertIn(f"m:{page.short_id}", result)
        self.assertIn("机に開きました", result)
        self.assertIn(f"m:{page.short_id}", self._desk_refs())

    def test_open_unknown_memopedia_ref_reports_not_found(self):
        result = atlas.open_page(self.adapter, "m:999")
        self.assertIn("見つかりません", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_chronicle_entry_registers_desk_item(self):
        entry = self._make_chronicle_entry()
        result = atlas.open_page(self.adapter, f"ch:{entry.short_id}")
        self.assertIn(f"ch:{entry.short_id}", self._desk_refs())
        self.assertIn("机に開きました", result)

    def test_open_task_returns_stub(self):
        result = atlas.open_page(self.adapter, "task:1")
        self.assertIn("P2b", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_after_open_removes_desk_item(self):
        page = self._make_memopedia_page()
        atlas.open_page(self.adapter, f"m:{page.short_id}")
        result = atlas.close_page(self.adapter, f"m:{page.short_id}")
        self.assertIn("机から閉じました", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_not_open_reports_message(self):
        page = self._make_memopedia_page()
        result = atlas.close_page(self.adapter, f"m:{page.short_id}")
        self.assertIn("開かれていません", result)

    def test_open_evicts_lru_when_budget_exceeded(self):
        # 1件 (title 1字 + 本文50字 = 51字) は収まるが、2件 (102字) は収まらない
        # 予算に設定し、後から開いた方だけが残ることを検証する。
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "60"
        page1 = self._make_memopedia_page(title="A", content="あ" * 50)
        page2 = self._make_memopedia_page(title="B", content="い" * 50)

        atlas.open_page(self.adapter, f"m:{page1.short_id}")
        result = atlas.open_page(self.adapter, f"m:{page2.short_id}")

        self.assertIn("棚に戻しました", result)
        # 予算超過で最も古く触られていた A が追い出され、B だけが机に残る
        self.assertEqual(self._desk_refs(), {f"m:{page2.short_id}"})

    def test_reopen_same_page_does_not_duplicate(self):
        page = self._make_memopedia_page()
        atlas.open_page(self.adapter, f"m:{page.short_id}")
        atlas.open_page(self.adapter, f"m:{page.short_id}")
        self.assertEqual(len(self._desk_refs()), 1)

    def test_read_touches_open_page(self):
        # 机に開いたページを read すると last_touched_at が更新される
        # (touch の定義 = read/write/clip が触った扱い。読んでいる最中の
        #  ページが LRU に追い出される穴の回帰テスト、メイン修正)
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        page = self._make_memopedia_page()
        ref = f"m:{page.short_id}"
        atlas.open_page(self.adapter, ref)
        opened = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        atlas.read_page(self.adapter, ref)

        touched = list_open(self.adapter.conn)[0]
        self.assertEqual(touched.last_touched_at, int(clock.now().timestamp()))
        self.assertGreater(touched.last_touched_at, opened.last_touched_at)
        # opened_at は初回のまま (touch であって再 open ではない)
        self.assertEqual(touched.opened_at, opened.opened_at)

    def test_read_chronicle_touches_open_page(self):
        # chronicle 経路 (ch:N) でも read が touch になる
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        entry = self._make_chronicle_entry()
        ref = f"ch:{entry.short_id}"
        atlas.open_page(self.adapter, ref)
        opened = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        atlas.read_page(self.adapter, ref)

        touched = list_open(self.adapter.conn)[0]
        self.assertGreater(touched.last_touched_at, opened.last_touched_at)

    def test_read_does_not_open_closed_page(self):
        # read は既定の行為 — 机の場所は取らない (勝手に開かない)
        page = self._make_memopedia_page()
        atlas.read_page(self.adapter, f"m:{page.short_id}")
        self.assertEqual(self._desk_refs(), set())

    def test_open_oversized_page_is_not_self_evicted(self):
        # 予算より大きいページを open しても本人は追い出されない
        # (「開きました」と「棚に戻しました」の同居矛盾の回帰テスト、メイン修正)
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "10"
        page = self._make_memopedia_page(title="巨大", content="あ" * 100)
        ref = f"m:{page.short_id}"

        result = atlas.open_page(self.adapter, ref)

        self.assertIn("机に開きました", result)
        self.assertNotIn("棚に戻しました", result)  # evicted は空 = 通知も出ない
        self.assertEqual(self._desk_refs(), {ref})  # 「開きました」が真

    def test_open_oversized_page_evicts_others_but_not_self(self):
        # 巨大ページを開くと既存の他ページは追い出されるが、本人だけは残る
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "60"
        small = self._make_memopedia_page(title="小", content="い" * 30)
        big = self._make_memopedia_page(title="大", content="あ" * 100)

        atlas.open_page(self.adapter, f"m:{small.short_id}")
        result = atlas.open_page(self.adapter, f"m:{big.short_id}")

        self.assertIn("机に開きました", result)
        self.assertIn(f"m:{small.short_id}", result)  # 追い出し通知は先客
        self.assertEqual(self._desk_refs(), {f"m:{big.short_id}"})

    def _desk_refs(self):
        from sai_memory.desk import list_open

        return {item.ref for item in list_open(self.adapter.conn)}


class SearchPagesTests(_AtlasTestBase):
    def test_search_no_query_prompts_for_one(self):
        result = atlas.search_pages(self.adapter, "")
        self.assertIn("検索語を指定してください", result)

    def test_search_no_match(self):
        result = atlas.search_pages(self.adapter, "存在しないはずのキーワード12345")
        self.assertIn("見つかりませんでした", result)

    def test_search_finds_memopedia_and_chronicle(self):
        page = self._make_memopedia_page(title="花火大会", content="夏の思い出")
        entry = self._make_chronicle_entry(content="花火大会に行った日")

        result = atlas.search_pages(self.adapter, "花火大会")
        self.assertIn("Memopedia", result)
        self.assertIn(f"m:{page.short_id}", result)
        self.assertIn("Chronicle", result)
        self.assertIn(f"ch:{entry.short_id}", result)


class ChronicleShortIdBackfillTests(unittest.TestCase):
    """既存 (short_id 列なし) DB からの一回きり backfill と新規採番。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        # 旧スキーマ (short_id 列が存在しない状態) を再現。
        self.conn.execute(
            """
            CREATE TABLE arasuji_entries (
                id TEXT PRIMARY KEY,
                level INTEGER NOT NULL,
                content TEXT NOT NULL,
                source_ids_json TEXT NOT NULL,
                start_time INTEGER,
                end_time INTEGER,
                source_count INTEGER NOT NULL,
                message_count INTEGER NOT NULL,
                parent_id TEXT,
                is_consolidated INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.conn.execute(
            "INSERT INTO arasuji_entries VALUES "
            "('old-2', 1, '2番目に古い', '[]', 100, 100, 1, 1, NULL, 0, 200)"
        )
        self.conn.execute(
            "INSERT INTO arasuji_entries VALUES "
            "('old-1', 1, '一番古い', '[]', 50, 50, 1, 1, NULL, 0, 100)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_backfill_assigns_by_created_at_order(self):
        from sai_memory.arasuji.storage import get_entry_by_short_id, init_arasuji_tables

        init_arasuji_tables(self.conn)

        row1 = self.conn.execute(
            "SELECT short_id FROM arasuji_entries WHERE id = 'old-1'"
        ).fetchone()
        row2 = self.conn.execute(
            "SELECT short_id FROM arasuji_entries WHERE id = 'old-2'"
        ).fetchone()
        # created_at 昇順 (old-1=100 が old-2=200 より先) で 1, 2 が振られる。
        # (挿入順は old-2 → old-1 なので、挿入順採番なら逆になるはず)
        self.assertEqual(row1[0], 1)
        self.assertEqual(row2[0], 2)

        entry = get_entry_by_short_id(self.conn, 1)
        self.assertEqual(entry.id, "old-1")

    def test_backfill_runs_once_new_entries_continue_numbering(self):
        from sai_memory.arasuji.storage import create_entry, init_arasuji_tables

        init_arasuji_tables(self.conn)
        new_entry = create_entry(
            self.conn, level=1, content="新規", source_ids=[],
            source_count=0, message_count=0,
        )
        self.assertEqual(new_entry.short_id, 3)

        init_arasuji_tables(self.conn)  # 再 init で壊れず・再採番しない
        row = self.conn.execute(
            "SELECT short_id FROM arasuji_entries WHERE id = 'old-1'"
        ).fetchone()
        self.assertEqual(row[0], 1)


if __name__ == "__main__":
    unittest.main()
