"""Memory Atlas ファサード saiverse/memory_atlas.py のテスト (concept_consolidation.md)。

検証項目:
- ref 解決 (memopedia:N / core / core:N / chronicle:N / clip:N / task:N stub / 不正形式)
- read_page: Memopedia / コア記憶(全件・1件) / Chronicle の内容整形、貼られたクリップの表示
- open_page/close_page: コア記憶は常時開で拒否、Memopedia/Chronicle は desk.py に委譲、
  机が溢れたときの LRU 追い出し通知
- search_pages: Memopedia + Chronicle 横断検索、0 件フォールバック
- Chronicle の short_id backfill (legacy DB からの一回きり移行、新規行の自動採番)
- P2b: クリップの読み出し (clip:N — 点=対象メッセージ+引用箇所 / 範囲=生ログ全文)、
  貼られたクリップの抜粋描画 (常に抜粋 + memory_read clip:N への誘導)、
  write_page (memopedia:N 追記・core 新規・core:N 上書き・ch/p 拒否)、
  make_clip (点=逐語引用の実在検証 / 範囲=SCENE と同じ窓 / 参照貼りのみ / touch)

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

    def _add_conversation(self, texts):
        """user/model 交互の実会話メッセージを積み、message id のリストを返す。"""
        from sai_memory.memory.storage import add_message

        ids = []
        role = "user"
        for text in texts:
            ids.append(add_message(self.adapter.conn, "main", role, text))
            role = "model" if role == "user" else "user"
        return ids


class RefParsingTests(unittest.TestCase):
    def test_parses_memopedia_ref(self):
        self.assertEqual(atlas._parse_ref("memopedia:5"), ("memopedia", "5"))

    def test_parses_core_all(self):
        self.assertEqual(atlas._parse_ref("core"), ("core_all", None))
        self.assertEqual(atlas._parse_ref("CORE"), ("core_all", None))

    def test_parses_core_one(self):
        self.assertEqual(atlas._parse_ref("core:3"), ("core_one", "3"))

    def test_parses_chronicle_ref_distinct_from_core(self):
        # "chronicle:" は "core:" と衝突しない (2 文字プレフィックスの境界)
        self.assertEqual(atlas._parse_ref("chronicle:7"), ("chronicle", "7"))

    def test_parses_task_stub_ref(self):
        self.assertEqual(atlas._parse_ref("task:2"), ("task", "2"))

    def test_parses_clip_ref(self):
        self.assertEqual(atlas._parse_ref("clip:4"), ("clip", "4"))

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
        self.assertIn("core:1", result)
        self.assertIn("1件目", result)
        self.assertIn("core:2", result)
        self.assertIn("2件目", result)

    def test_read_core_one_found(self):
        from sai_memory.core_memory import add_core_memory

        new_id = add_core_memory(self.adapter.conn, "刻んだ内容")
        result = atlas.read_page(self.adapter, f"core:{new_id}")
        self.assertIn("刻んだ内容", result)
        self.assertIn(f"core:{new_id}", result)

    def test_read_core_one_not_found(self):
        result = atlas.read_page(self.adapter, "core:999")
        self.assertIn("見つかりません", result)

    def test_read_memopedia_page(self):
        page = self._make_memopedia_page(title="桃太郎", content="鬼退治に行った")
        result = atlas.read_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn("桃太郎", result)
        self.assertIn("鬼退治に行った", result)
        self.assertIn(f"memopedia:{page.short_id}", result)

    def test_read_memopedia_page_not_found(self):
        result = atlas.read_page(self.adapter, "memopedia:999")
        self.assertIn("見つかりません", result)

    def test_read_chronicle_entry(self):
        entry = self._make_chronicle_entry(content="祭りに参加した")
        result = atlas.read_page(self.adapter, f"chronicle:{entry.short_id}")
        self.assertIn("祭りに参加した", result)
        self.assertIn(f"chronicle:{entry.short_id}", result)

    def test_read_chronicle_entry_not_found(self):
        result = atlas.read_page(self.adapter, "chronicle:999")
        self.assertIn("見つかりません", result)

    def test_read_task_without_manager_reports_missing_context(self):
        # task:N の read は P2c-1 で解決済み。ただし目的の木は main DB 在住
        # なので manager (world 文脈) なしでは読めない — 丁寧に案内する
        result = atlas.read_page(self.adapter, "task:1")
        self.assertIn("world 文脈", result)

    def test_read_page_shows_pasted_clips(self):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.clips import add_clip

        new_id = add_core_memory(self.adapter.conn, "クリップつきの記憶")
        ref = f"core:{new_id}"
        clip = add_clip(
            self.adapter.conn, message_id="m1", quote="根拠の一言", pasted_to=ref
        )

        result = atlas.read_page(self.adapter, ref)
        self.assertIn(f"[クリップ clip:{clip.short_id}]", result)
        self.assertIn("根拠の一言", result)

    def test_read_page_without_clips_omits_clip_section(self):
        from sai_memory.core_memory import add_core_memory

        new_id = add_core_memory(self.adapter.conn, "クリップなし")
        result = atlas.read_page(self.adapter, f"core:{new_id}")
        self.assertNotIn("[クリップ", result)


class OpenClosePageTests(_AtlasTestBase):
    def test_open_core_all_is_rejected(self):
        result = atlas.open_page(self.adapter, "core")
        self.assertIn("常時開いています", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_core_one_is_rejected(self):
        result = atlas.open_page(self.adapter, "core:1")
        self.assertIn("常時開いています", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_core_is_rejected(self):
        result = atlas.close_page(self.adapter, "core:1")
        self.assertIn("閉じられません", result)

    def test_open_memopedia_page_registers_desk_item(self):
        page = self._make_memopedia_page()
        result = atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn(f"memopedia:{page.short_id}", result)
        self.assertIn("机に開きました", result)
        self.assertIn(f"memopedia:{page.short_id}", self._desk_refs())

    def test_open_returns_page_content_inline(self):
        """開く＝読む行為を兼ねる: 結果にページ本文が含まれる (2026-07-11 まはー指摘)。

        机の head セクションは次の Metabolism まで凍結されるため、ここで本文を
        返さないと「開いたのに中身が見えず read を撃ち直す二度手間」になる。
        """
        page = self._make_memopedia_page(title="琥珀色の聖域", content="温かみのある光")
        result = atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn("琥珀色の聖域", result)
        self.assertIn("温かみのある光", result)

    def test_open_unknown_memopedia_ref_reports_not_found(self):
        result = atlas.open_page(self.adapter, "memopedia:999")
        self.assertIn("見つかりません", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_chronicle_entry_registers_desk_item(self):
        entry = self._make_chronicle_entry()
        result = atlas.open_page(self.adapter, f"chronicle:{entry.short_id}")
        self.assertIn(f"chronicle:{entry.short_id}", self._desk_refs())
        self.assertIn("机に開きました", result)

    def test_open_task_is_rejected_as_read_only(self):
        # 目的の木の退役 (2026-08-23) 以後、task:N は机に開けない。実在確認より
        # 前に断るので manager の有無に関わらず同じ文面が返る (manager 込みの
        # 回帰は TaskDeskTests でカバーする)。
        result = atlas.open_page(self.adapter, "task:1")
        self.assertIn("机に開けません", result)
        self.assertIn("memory_read task:1", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_clip_is_rejected_with_read_hint(self):
        # クリップは机の対象外 (参照であってページではない)。read への誘導を返す
        result = atlas.open_page(self.adapter, "clip:1")
        self.assertIn("memory_read clip:1", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_clip_reports_not_applicable(self):
        result = atlas.close_page(self.adapter, "clip:1")
        self.assertIn("机の対象外", result)

    def test_close_after_open_removes_desk_item(self):
        page = self._make_memopedia_page()
        atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        result = atlas.close_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn("机から閉じました", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_not_open_reports_message(self):
        page = self._make_memopedia_page()
        result = atlas.close_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn("開かれていません", result)

    def test_open_evicts_lru_when_budget_exceeded(self):
        # 1件 (title 1字 + 本文50字 = 51字) は収まるが、2件 (102字) は収まらない
        # 予算に設定し、後から開いた方だけが残ることを検証する。
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "60"
        page1 = self._make_memopedia_page(title="A", content="あ" * 50)
        page2 = self._make_memopedia_page(title="B", content="い" * 50)

        atlas.open_page(self.adapter, f"memopedia:{page1.short_id}")
        result = atlas.open_page(self.adapter, f"memopedia:{page2.short_id}")

        self.assertIn("棚に戻しました", result)
        # 予算超過で最も古く触られていた A が追い出され、B だけが机に残る
        self.assertEqual(self._desk_refs(), {f"memopedia:{page2.short_id}"})

    def test_reopen_same_page_does_not_duplicate(self):
        page = self._make_memopedia_page()
        atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertEqual(len(self._desk_refs()), 1)

    def test_read_touches_open_page(self):
        # 机に開いたページを read すると last_touched_at が更新される
        # (touch の定義 = read/write/clip が触った扱い。読んでいる最中の
        #  ページが LRU に追い出される穴の回帰テスト、メイン修正)
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        page = self._make_memopedia_page()
        ref = f"memopedia:{page.short_id}"
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
        # chronicle 経路 (chronicle:N) でも read が touch になる
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        entry = self._make_chronicle_entry()
        ref = f"chronicle:{entry.short_id}"
        atlas.open_page(self.adapter, ref)
        opened = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        atlas.read_page(self.adapter, ref)

        touched = list_open(self.adapter.conn)[0]
        self.assertGreater(touched.last_touched_at, opened.last_touched_at)

    def test_read_does_not_open_closed_page(self):
        # read は既定の行為 — 机の場所は取らない (勝手に開かない)
        page = self._make_memopedia_page()
        atlas.read_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertEqual(self._desk_refs(), set())

    def test_open_oversized_page_is_not_self_evicted(self):
        # 予算より大きいページを open しても本人は追い出されない
        # (「開きました」と「棚に戻しました」の同居矛盾の回帰テスト、メイン修正)
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "10"
        page = self._make_memopedia_page(title="巨大", content="あ" * 100)
        ref = f"memopedia:{page.short_id}"

        result = atlas.open_page(self.adapter, ref)

        self.assertIn("机に開きました", result)
        self.assertNotIn("棚に戻しました", result)  # evicted は空 = 通知も出ない
        self.assertEqual(self._desk_refs(), {ref})  # 「開きました」が真

    def test_open_oversized_page_evicts_others_but_not_self(self):
        # 巨大ページを開くと既存の他ページは追い出されるが、本人だけは残る
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "60"
        small = self._make_memopedia_page(title="小", content="い" * 30)
        big = self._make_memopedia_page(title="大", content="あ" * 100)

        atlas.open_page(self.adapter, f"memopedia:{small.short_id}")
        result = atlas.open_page(self.adapter, f"memopedia:{big.short_id}")

        self.assertIn("机に開きました", result)
        self.assertIn(f"memopedia:{small.short_id}", result)  # 追い出し通知は先客
        self.assertEqual(self._desk_refs(), {f"memopedia:{big.short_id}"})

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
        self.assertIn(f"memopedia:{page.short_id}", result)
        self.assertIn("Chronicle", result)
        self.assertIn(f"chronicle:{entry.short_id}", result)


class ReadClipTests(_AtlasTestBase):
    """clip:N — クリップを読む＝そのクリップが写す土地 (生ログ) を見に行く。"""

    def test_read_point_clip_shows_message_and_quote(self):
        from sai_memory.clips import add_clip

        ids = self._add_conversation(["今日は晴れだった", "いい天気でしたね"])
        clip = add_clip(self.adapter.conn, message_id=ids[0], quote="晴れだった")

        result = atlas.read_page(self.adapter, clip.ref)
        self.assertIn(f"clip:{clip.short_id}", result)
        self.assertIn("引用箇所: 「晴れだった」", result)
        # 対象メッセージの本文全体が読める
        self.assertIn("今日は晴れだった", result)

    def test_read_range_clip_shows_full_transcript(self):
        from sai_memory.clips import add_clip

        texts = [f"発言その{i}" for i in range(1, 7)]
        ids = self._add_conversation(texts)
        clip = add_clip(
            self.adapter.conn, message_id=ids[0], message_id_end=ids[-1]
        )

        result = atlas.read_page(self.adapter, clip.ref)
        self.assertIn(f"全{len(texts)}メッセージ", result)
        # 範囲の生ログ全文 — 先頭も末尾も省略されない
        for text in texts:
            self.assertIn(text, result)

    def test_read_clip_not_found(self):
        result = atlas.read_page(self.adapter, "clip:999")
        self.assertIn("見つかりません", result)

    def test_read_clip_invalid_key(self):
        result = atlas.read_page(self.adapter, "clip:abc")
        self.assertIn("不正", result)

    def test_read_clip_does_not_touch_desk(self):
        # クリップを読んでも机は動かない (読んだのは土地であってページではない)
        from sai_memory.desk import list_open
        from sai_memory.clips import add_clip

        ids = self._add_conversation(["ひとこと", "ふたこと"])
        clip = add_clip(self.adapter.conn, message_id=ids[0], quote="ひとこと")
        atlas.read_page(self.adapter, clip.ref)
        self.assertEqual(list_open(self.adapter.conn), [])


class ClipExcerptRenderingTests(_AtlasTestBase):
    """貼られたクリップの描画は常に抜粋 (ページがクリップに食われない)。"""

    def _paste_range_clip_to_core(self, n_messages=8):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.clips import add_clip

        texts = [f"長い会話の発言{i}" for i in range(1, n_messages + 1)]
        ids = self._add_conversation(texts)
        core_id = add_core_memory(self.adapter.conn, "クリップつきの記憶")
        clip = add_clip(
            self.adapter.conn, message_id=ids[0], message_id_end=ids[-1],
            pasted_to=f"core:{core_id}",
        )
        return core_id, clip, texts

    def test_range_clip_excerpt_truncates_and_points_to_read(self):
        core_id, clip, texts = self._paste_range_clip_to_core()

        result = atlas.read_page(self.adapter, f"core:{core_id}")
        # 抜粋: 先頭数行は見える
        self.assertIn(texts[0], result)
        # 末尾は省略される (丸ごと載せない)
        self.assertNotIn(texts[-1], result)
        # 「全Nメッセージ・M字、前後省略 — memory_read clip:N で全文」の案内
        self.assertIn(f"全{len(texts)}メッセージ", result)
        self.assertIn("前後省略", result)
        self.assertIn(f"memory_read clip:{clip.short_id} で全文", result)

    def test_range_clip_excerpt_shows_label(self):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.clips import add_clip

        ids = self._add_conversation(["最初の一言", "返事"])
        core_id = add_core_memory(self.adapter.conn, "ラベルつき")
        add_clip(
            self.adapter.conn, message_id=ids[0], message_id_end=ids[-1],
            quote="初対面の夜", pasted_to=f"core:{core_id}",
        )
        result = atlas.read_page(self.adapter, f"core:{core_id}")
        self.assertIn("ラベル: 初対面の夜", result)

    def test_range_clip_with_lost_endpoints_degrades_gracefully(self):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.clips import add_clip

        core_id = add_core_memory(self.adapter.conn, "端が失われたクリップ")
        add_clip(
            self.adapter.conn, message_id="gone-1", message_id_end="gone-2",
            pasted_to=f"core:{core_id}",
        )
        result = atlas.read_page(self.adapter, f"core:{core_id}")
        self.assertIn("読み出せません", result)


class WritePageTests(_AtlasTestBase):
    def test_write_core_creates_new_core_memory(self):
        from sai_memory.core_memory import list_core_memories

        result = atlas.write_page(self.adapter, "core", "恒常知識その1")
        self.assertIn("core:1", result)
        self.assertIn("常時開", result)
        items = list_core_memories(self.adapter.conn)
        self.assertEqual([cm.content for cm in items], ["恒常知識その1"])

    def test_write_core_budget_note_when_exceeded(self):
        result = atlas.write_page(
            self.adapter, "core", "あ" * 100, core_budget=50,
        )
        self.assertIn("目安を超えています", result)

    def test_write_core_one_overwrites(self):
        from sai_memory.core_memory import add_core_memory, get_core_memory

        mid = add_core_memory(self.adapter.conn, "古い内容")
        result = atlas.write_page(self.adapter, f"core:{mid}", "新しい内容")
        self.assertIn("上書きしました", result)
        self.assertEqual(get_core_memory(self.adapter.conn, mid).content, "新しい内容")

    def test_write_core_one_not_found(self):
        result = atlas.write_page(self.adapter, "core:999", "内容")
        self.assertIn("見つかりません", result)

    def test_write_core_one_refuses_a_scene_memory(self):
        """⭐ 場面の記憶 (実会話の写し) は memory_write からも書き換えられない。

        スルースの update だけを塞いでも、同じことが memory_write スペルから
        通ってしまう (2026-08-22 Codex 横断走査の指摘)。守りたいのは「写しが
        改変されないこと」なので、ペルソナ側の口は全部同じ規則で断る。
        docs/issues/archive/sluice_truncated_scene_update.md
        """
        from sai_memory.core_memory import add_core_memory, get_core_memory

        mid = add_core_memory(self.adapter.conn, "ユーザー「おはよう」", kind="scene")
        result = atlas.write_page(self.adapter, f"core:{mid}", "書き換えた本文")

        self.assertIn("場面の記憶", result)
        self.assertIn("対象外", result)
        # 写しは無傷
        self.assertEqual(
            get_core_memory(self.adapter.conn, mid).content, "ユーザー「おはよう」",
        )

    def test_write_memopedia_appends_with_edit_history(self):
        from sai_memory.memopedia import Memopedia

        page = self._make_memopedia_page(title="覚え書き", content="最初の本文")
        result = atlas.write_page(self.adapter, f"memopedia:{page.short_id}", "追記した一文")
        self.assertIn("追記しました", result)
        self.assertIn("覚え書き", result)

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        updated = memopedia.get_page(page.id)
        self.assertIn("最初の本文", updated.content)
        self.assertIn("追記した一文", updated.content)
        # 編集来歴が残る経路 (append_to_content) を通っている
        history = memopedia.get_page_edit_history(page.id)
        self.assertIn("append", [h.edit_type for h in history])

    def test_write_memopedia_touches_open_page(self):
        # write も touch (touch の定義 = read/write/clip)
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        page = self._make_memopedia_page()
        ref = f"memopedia:{page.short_id}"
        atlas.open_page(self.adapter, ref)
        opened = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        atlas.write_page(self.adapter, ref, "追記")

        touched = list_open(self.adapter.conn)[0]
        self.assertGreater(touched.last_touched_at, opened.last_touched_at)

    def test_write_memopedia_not_found(self):
        result = atlas.write_page(self.adapter, "memopedia:999", "内容")
        self.assertIn("見つかりません", result)

    def test_write_chronicle_is_rejected(self):
        result = atlas.write_page(self.adapter, "chronicle:1", "内容")
        self.assertIn("書けません", result)

    def test_write_clip_is_rejected(self):
        result = atlas.write_page(self.adapter, "clip:1", "内容")
        self.assertIn("書けません", result)

    def test_write_task_returns_stub(self):
        result = atlas.write_page(self.adapter, "task:1", "内容")
        self.assertIn("今後対応予定", result)

    def test_write_empty_content_is_rejected(self):
        result = atlas.write_page(self.adapter, "core", "   ")
        self.assertIn("空にできません", result)


class MakeClipTests(_AtlasTestBase):
    def test_clip_point_clip_with_valid_quote(self):
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["大事な一言があった", "そうですね"])
        result = atlas.make_clip(self.adapter, ids[0], quote="大事な一言")

        clips = list_clips(self.adapter.conn)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].quote, "大事な一言")
        self.assertFalse(clips[0].is_range)
        self.assertIsNone(clips[0].pasted_to)  # 貼り先未指定 = 土壌プール
        self.assertIn(f"clip:{clips[0].short_id}", result)
        self.assertIn("「大事な一言」", result)

    def test_clip_point_clip_quote_mismatch_rejected(self):
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["本文はこれだけ", "はい"])
        result = atlas.make_clip(self.adapter, ids[0], quote="本文に無い言葉")

        self.assertIn("見つかりませんでした", result)
        self.assertEqual(list_clips(self.adapter.conn), [])  # 撮られない

    def test_clip_point_clip_message_not_found(self):
        result = atlas.make_clip(self.adapter, "no-such-message", quote="何か")
        self.assertIn("見つかりません", result)

    def test_clip_range_clip_around_anchor(self):
        from sai_memory.clips import list_clips

        texts = [f"会話{i}" for i in range(1, 8)]
        ids = self._add_conversation(texts)
        anchor = ids[3]  # 中央
        result = atlas.make_clip(self.adapter, anchor, rounds=1)

        clips = list_clips(self.adapter.conn)
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].is_range)
        # rounds=1 → アンカー±2件 = 全5メッセージ
        self.assertIn("全5メッセージ", result)
        self.assertIn(f"clip:{clips[0].short_id}", result)

    def test_clip_with_paste_to_memopedia_normalizes_and_pastes(self):
        from sai_memory.clips import list_clips

        page = self._make_memopedia_page()
        ids = self._add_conversation(["貼る対象の発言", "了解"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="貼る対象", paste_to=f"memopedia:{page.short_id}",
        )
        clips = list_clips(self.adapter.conn)
        self.assertEqual(clips[0].pasted_to, f"memopedia:{page.short_id}")
        self.assertIn("貼りました", result)

    def test_clip_with_paste_to_core_memory(self):
        from sai_memory.core_memory import add_core_memory
        from sai_memory.clips import list_clips

        core_id = add_core_memory(self.adapter.conn, "貼り先のコア記憶")
        ids = self._add_conversation(["根拠になる発言", "なるほど"])
        atlas.make_clip(
            self.adapter, ids[0], quote="根拠になる発言", paste_to=f"core:{core_id}",
        )
        clips = list_clips(self.adapter.conn)
        self.assertEqual(clips[0].pasted_to, f"core:{core_id}")

    def test_clip_paste_to_unknown_page_rejected_without_saving(self):
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["発言", "返事"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="発言", paste_to="memopedia:999",
        )
        self.assertIn("見つかりません", result)
        self.assertEqual(list_clips(self.adapter.conn), [])

    def test_clip_paste_to_task_is_p2c_stub_without_saving(self):
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["発言", "返事"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="発言", paste_to="task:1",
        )
        self.assertIn("今後対応予定", result)
        self.assertEqual(list_clips(self.adapter.conn), [])

    def test_clip_touches_open_paste_target(self):
        # clip も touch (touch の定義 = read/write/clip)
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        page = self._make_memopedia_page()
        ref = f"memopedia:{page.short_id}"
        atlas.open_page(self.adapter, ref)
        opened = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        ids = self._add_conversation(["触る発言", "返事"])
        atlas.make_clip(self.adapter, ids[0], quote="触る発言", paste_to=ref)

        touched = list_open(self.adapter.conn)[0]
        self.assertGreater(touched.last_touched_at, opened.last_touched_at)

    def test_clip_range_anchor_not_conversation_rejected(self):
        # 実会話でないアンカー (ツール実行ログ相当) は範囲クリップにできない
        from sai_memory.memory.storage import add_message

        mid = add_message(
            self.adapter.conn, "main", "user", "spellの結果ログ",
            metadata={"tags": ["spell"]},
        )
        result = atlas.make_clip(self.adapter, mid)
        self.assertIn("実会話ではありません", result)


class DeletePageTests(_AtlasTestBase):
    """memory_delete / delete_page (P2c-0 決定1: soft-delete の統一動詞)。"""

    def test_delete_core_memory_soft_deletes(self):
        from sai_memory.core_memory import add_core_memory, list_deleted_core_memories

        mid = add_core_memory(self.adapter.conn, "消される恒常知識")
        result = atlas.delete_page(self.adapter, f"core:{mid}")

        self.assertIn("ごみ箱", result)
        # soft-delete: read は「見つかりません」だが行はごみ箱に残る
        self.assertIn("見つかりません", atlas.read_page(self.adapter, f"core:{mid}"))
        trash = list_deleted_core_memories(self.adapter.conn)
        self.assertEqual([cm.id for cm in trash], [mid])

    def test_delete_core_memory_not_found(self):
        result = atlas.delete_page(self.adapter, "core:999")
        self.assertIn("見つかりません", result)

    def test_delete_memopedia_page_soft_deletes(self):
        page = self._make_memopedia_page(title="消される本", content="消える中身")
        ref = f"memopedia:{page.short_id}"
        result = atlas.delete_page(self.adapter, ref)

        self.assertIn("ごみ箱", result)
        # read は「見つかりません」(soft-delete 済みは Atlas の読み口で弾く)
        self.assertIn("見つかりません", atlas.read_page(self.adapter, ref))
        # 行そのものは残る (is_deleted=1 のごみ箱)
        row = self.adapter.conn.execute(
            "SELECT is_deleted FROM memopedia_pages WHERE id = ?", (page.id,)
        ).fetchone()
        self.assertEqual(row[0], 1)

    def test_delete_memopedia_page_closes_desk(self):
        from sai_memory.desk import list_open

        page = self._make_memopedia_page(title="机の上で消える本")
        ref = f"memopedia:{page.short_id}"
        atlas.open_page(self.adapter, ref)

        result = atlas.delete_page(self.adapter, ref)
        self.assertIn("机からも下ろしました", result)
        self.assertEqual(list_open(self.adapter.conn), [])

    def test_delete_memopedia_not_on_desk_omits_desk_note(self):
        page = self._make_memopedia_page(title="棚のまま消える本")
        result = atlas.delete_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertNotIn("机からも", result)

    def test_delete_already_deleted_page_reports_not_found(self):
        page = self._make_memopedia_page(title="二度消される本")
        ref = f"memopedia:{page.short_id}"
        atlas.delete_page(self.adapter, ref)
        result = atlas.delete_page(self.adapter, ref)
        self.assertIn("見つかりません", result)

    def test_delete_rejects_chronicle(self):
        entry = self._make_chronicle_entry()
        result = atlas.delete_page(self.adapter, f"chronicle:{entry.short_id}")
        self.assertIn("消せません", result)

    def test_delete_rejects_clip(self):
        result = atlas.delete_page(self.adapter, "clip:1")
        self.assertIn("消せません", result)
        self.assertIn("歴史", result)

    def test_delete_rejects_core_all(self):
        result = atlas.delete_page(self.adapter, "core")
        self.assertIn("消せません", result)

    def test_delete_task_is_rejected_without_naming_a_retired_spell(self):
        # 目的の木の退役 (2026-08-23) 前は purpose_close へ誘導していた文面。
        # スペルが消えたので、消せないことだけを本人に返す。
        result = atlas.delete_page(self.adapter, "task:1")
        self.assertIn("消せません", result)
        self.assertNotIn("purpose_close", result)


class ClipTranscribeTests(_AtlasTestBase):
    """memory_clip mode='transcribe' (P2c-0 決定2: 転写は clip のモードに畳む)。"""

    def test_transcribe_range_to_core_shows_budget_note_when_exceeded(self):
        # 旧 core_memory_add_scene にあった budget 超過通知を範囲転写でも出す
        # (P2c-4a: memory_write(core) にはあったが clip transcribe(core) に無かった)。
        ids = self._add_conversation([f"発言{i}" for i in range(1, 6)])
        result = atlas.make_clip(
            self.adapter, ids[2], paste_to="core", mode="transcribe",
            core_budget=10,
        )
        self.assertIn("目安を超えています", result)

    def test_transcribe_point_to_core_shows_budget_note_when_exceeded(self):
        ids = self._add_conversation(["刻みたい一言があった", "そうですね"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="刻みたい一言",
            paste_to="core", mode="transcribe", core_budget=5,
        )
        self.assertIn("目安を超えています", result)

    def test_transcribe_range_to_core_matches_old_scene_logic(self):
        # core 宛の範囲転写 = 旧 core_memory_add_scene と同一の共有ロジック
        # (create_scene_core_memory)。transcript が完全一致すること
        from sai_memory.core_memory import format_scene_transcript, list_core_memories
        from sai_memory.memory.storage import get_conversation_window_around
        from sai_memory.clips import list_clips_pasted_to

        ids = self._add_conversation([f"発言{i}" for i in range(1, 6)])
        anchor = ids[2]
        result = atlas.make_clip(
            self.adapter, anchor, paste_to="core", mode="transcribe",
        )

        self.assertIn("転写しました", result)
        items = list_core_memories(self.adapter.conn)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "scene")  # 共有ロジックの証跡
        expected = format_scene_transcript(
            get_conversation_window_around(self.adapter.conn, anchor, rounds=3),
            "tester",
        )
        self.assertEqual(items[0].content, expected)
        # 転写でもクリップ (由来参照) は撮られる
        clips = list_clips_pasted_to(self.adapter.conn, f"core:{items[0].id}")
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].is_range)
        self.assertIn(clips[0].ref, result)

    def test_transcribe_point_to_core_creates_core_memory(self):
        from sai_memory.core_memory import list_core_memories
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["刻みたい一言があった", "そうですね"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="刻みたい一言",
            paste_to="core", mode="transcribe",
        )
        self.assertIn("転写しました", result)
        items = list_core_memories(self.adapter.conn)
        self.assertEqual([cm.content for cm in items], ["刻みたい一言"])
        clips = list_clips(self.adapter.conn)
        self.assertEqual(clips[0].pasted_to, f"core:{items[0].id}")
        self.assertFalse(clips[0].is_range)

    def test_transcribe_range_to_memopedia_burns_transcript(self):
        from sai_memory.core_memory import format_scene_transcript
        from sai_memory.memopedia import Memopedia
        from sai_memory.memory.storage import get_conversation_window_around
        from sai_memory.clips import list_clips_pasted_to

        page = self._make_memopedia_page(title="転写先", content="既存の本文")
        ref = f"memopedia:{page.short_id}"
        ids = self._add_conversation([f"会話{i}" for i in range(1, 6)])
        anchor = ids[2]

        result = atlas.make_clip(
            self.adapter, anchor, paste_to=ref, mode="transcribe",
        )
        self.assertIn("転写しました", result)

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        updated = memopedia.get_page(page.id)
        expected = format_scene_transcript(
            get_conversation_window_around(self.adapter.conn, anchor, rounds=3),
            "tester",
        )
        self.assertIn("既存の本文", updated.content)  # 追記 (上書きしない)
        self.assertIn(expected, updated.content)
        # 編集来歴が残る append 経路
        history = memopedia.get_page_edit_history(page.id)
        self.assertIn("append", [h.edit_type for h in history])
        # クリップ (由来参照) も撮られて貼られる
        clips = list_clips_pasted_to(self.adapter.conn, ref)
        self.assertEqual(len(clips), 1)
        self.assertTrue(clips[0].is_range)

    def test_transcribe_point_to_memopedia_burns_quote(self):
        from sai_memory.memopedia import Memopedia
        from sai_memory.clips import list_clips_pasted_to

        page = self._make_memopedia_page(title="引用の転写先", content="本文")
        ref = f"memopedia:{page.short_id}"
        ids = self._add_conversation(["残したい言葉が出た", "はい"])

        result = atlas.make_clip(
            self.adapter, ids[0], quote="残したい言葉",
            paste_to=ref, mode="transcribe",
        )
        self.assertIn("転写しました", result)
        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        self.assertIn("残したい言葉", memopedia.get_page(page.id).content)
        clips = list_clips_pasted_to(self.adapter.conn, ref)
        self.assertFalse(clips[0].is_range)
        self.assertEqual(clips[0].quote, "残したい言葉")

    def test_transcribe_point_quote_mismatch_rejected(self):
        from sai_memory.core_memory import list_core_memories
        from sai_memory.clips import list_clips

        ids = self._add_conversation(["本文はこれだけ", "はい"])
        result = atlas.make_clip(
            self.adapter, ids[0], quote="本文に無い言葉",
            paste_to="core", mode="transcribe",
        )
        self.assertIn("見つかりませんでした", result)
        self.assertEqual(list_core_memories(self.adapter.conn), [])
        self.assertEqual(list_clips(self.adapter.conn), [])

    def test_transcribe_requires_paste_to(self):
        ids = self._add_conversation(["何か", "はい"])
        result = atlas.make_clip(self.adapter, ids[0], mode="transcribe")
        self.assertIn("Error", result)
        self.assertIn("paste_to", result)

    def test_transcribe_rejects_existing_core_target(self):
        from sai_memory.core_memory import add_core_memory

        mid = add_core_memory(self.adapter.conn, "既存のコア記憶")
        ids = self._add_conversation(["何か", "はい"])
        result = atlas.make_clip(
            self.adapter, ids[0], paste_to=f"core:{mid}", mode="transcribe",
        )
        self.assertIn("できません", result)
        self.assertIn("core", result)  # 新規に刻む "core" への誘導

    def test_invalid_mode_rejected(self):
        ids = self._add_conversation(["何か", "はい"])
        result = atlas.make_clip(self.adapter, ids[0], mode="burn")
        self.assertIn("Error", result)

    def test_attach_default_does_not_burn_content(self):
        # 既定 (attach) は従来どおり参照貼りのみ — 本文は変わらない
        from sai_memory.memopedia import Memopedia

        page = self._make_memopedia_page(title="参照貼り先", content="元の本文")
        ids = self._add_conversation([f"会話{i}" for i in range(1, 6)])
        atlas.make_clip(self.adapter, ids[2], paste_to=f"memopedia:{page.short_id}")

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        self.assertEqual(memopedia.get_page(page.id).content, "元の本文")


class WriteCreatePageTests(_AtlasTestBase):
    """memory_write の新規ページ作成 (P2c-0 決定3)。"""

    def test_title_creates_new_page_with_history(self):
        from sai_memory.memopedia import Memopedia

        result = atlas.write_page(
            self.adapter, content="最初の本文", title="新しい知識", category="events",
        )
        self.assertIn("新しいページを作りました", result)
        self.assertIn("events", result)

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        page = memopedia.find_by_title("新しい知識")
        self.assertIsNotNone(page)
        self.assertEqual(page.content, "最初の本文")
        self.assertEqual(page.parent_id, "root_events")  # カテゴリ root の下
        # 編集来歴が残る create 経路
        history = memopedia.get_page_edit_history(page.id)
        self.assertIn("create", [h.edit_type for h in history])

    def test_title_default_category_is_terms(self):
        from sai_memory.memopedia import Memopedia

        atlas.write_page(self.adapter, content="本文", title="無指定カテゴリ")
        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        page = memopedia.find_by_title("無指定カテゴリ")
        self.assertEqual(page.parent_id, "root_terms")

    def test_title_duplicate_guides_to_append(self):
        page = self._make_memopedia_page(title="既にある本")
        result = atlas.write_page(
            self.adapter, content="本文", title="既にある本",
        )
        self.assertIn("既にあります", result)
        self.assertIn(f"memopedia:{page.short_id}", result)  # 追記への誘導

    def test_title_invalid_category_rejected(self):
        result = atlas.write_page(
            self.adapter, content="本文", title="変なカテゴリ", category="dreams",
        )
        self.assertIn("Error", result)

    def test_ref_and_title_are_mutually_exclusive(self):
        page = self._make_memopedia_page()
        both = atlas.write_page(
            self.adapter, f"memopedia:{page.short_id}", "本文", title="両方指定",
        )
        neither = atlas.write_page(self.adapter, content="本文だけ")
        self.assertIn("Error", both)
        self.assertIn("Error", neither)


class TaskReadTests(_AtlasTestBase):
    """task:N (目的ノード) の read_page 解決 (P2c-1)。

    目的の木は main DB (persona_task) 在住なので、adapter に加えて manager
    (SessionLocal を持つ world 文脈の shim) を渡す。
    """

    def setUp(self):
        super().setUp()
        from types import SimpleNamespace

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from database.models import Base

        self._engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self._engine)
        self.manager = SimpleNamespace(SessionLocal=sessionmaker(bind=self._engine))
        self.addCleanup(self._engine.dispose)

    def _make_task(self, **kwargs):
        from saiverse.persona_task_manager import PersonaTaskManager

        ptm = PersonaTaskManager(self.manager.SessionLocal)
        defaults = {
            "persona_id": "tester",  # _AtlasTestBase の adapter.persona_id と揃える
            "title": "語源メモをまとめる",
            "goal": "一冊のノートに仕上げる",
            "auto_activate": False,
        }
        defaults.update(kwargs)
        return ptm.create_task(**defaults)

    def test_read_task_renders_node(self):
        task = self._make_task(
            steps=[{"title": "下調べ"}, {"title": "清書"}],
            desire_source="図書館で読んだ語源の記事",
        )
        result = atlas.read_page(
            self.adapter, task["task_ref"], manager=self.manager,
        )
        self.assertIn("語源メモをまとめる", result)
        self.assertIn(task["task_ref"], result)
        self.assertIn("段階:", result)
        self.assertIn("状態:", result)
        self.assertIn("目標: 一冊のノートに仕上げる", result)
        self.assertIn("由来: 図書館で読んだ語源の記事", result)  # 接地の証跡
        self.assertIn("下調べ", result)
        self.assertIn("清書", result)

    def test_read_task_shows_pasted_clips(self):
        from sai_memory.clips import add_clip

        task = self._make_task()
        add_clip(
            self.adapter.conn, message_id="m1", quote="きっかけの一言",
            pasted_to=task["task_ref"],
        )
        result = atlas.read_page(
            self.adapter, task["task_ref"], manager=self.manager,
        )
        self.assertIn("[クリップ", result)
        self.assertIn("きっかけの一言", result)

    def test_read_task_not_found(self):
        result = atlas.read_page(self.adapter, "task:999", manager=self.manager)
        self.assertIn("見つかりません", result)

class TaskDeskTests(TaskReadTests):
    """task:N (目的ノード) と机 — 目的の木の退役 (2026-08-23) 後の姿。

    新規に開く口は閉じた (open は断る) が、退役より前に机へ開かれた行を
    本人が下ろせなくなると困るので、close と机の描画 (snapshot_desk) は
    残置。したがって「既に机にある」状態は desk へ直接置いて作る。
    setUp/_make_task は TaskReadTests から継承 (manager 込みの土台を共用)。
    """

    def _put_on_desk(self, ref: str):
        """退役前に開かれた机の行を模す (open_page はもう task を受けない)。"""
        from sai_memory.desk import open_item

        open_item(self.adapter.conn, ref)

    def test_open_task_is_rejected_even_when_node_exists(self):
        # 実在する目的ノードでも机には開けない (読み取り専用の残置)。
        task = self._make_task()
        ref = task["task_ref"]
        result = atlas.open_page(self.adapter, ref, manager=self.manager)
        self.assertIn("机に開けません", result)
        self.assertIn("memory_read", result)
        self.assertEqual(self._desk_refs(), set())

    def test_open_unknown_task_is_rejected_the_same_way(self):
        # 実在確認より前に断るので、未知の番号でも文面は同じ (存在の漏洩もない)。
        result = atlas.open_page(self.adapter, "task:999", manager=self.manager)
        self.assertIn("机に開けません", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_task_removes_desk_item(self):
        task = self._make_task()
        ref = task["task_ref"]
        self._put_on_desk(ref)
        result = atlas.close_page(self.adapter, ref, manager=self.manager)
        self.assertIn("机から閉じました", result)
        self.assertEqual(self._desk_refs(), set())

    def test_close_task_without_manager_reports_not_found(self):
        # close は実在確認を通るので、manager が無いと task:N は解決できない
        task = self._make_task()
        self._put_on_desk(task["task_ref"])
        result = atlas.close_page(self.adapter, task["task_ref"])
        self.assertIn("見つかりません", result)

    def test_snapshot_renders_open_task_like_read_task(self):
        task = self._make_task(
            steps=[{"title": "下調べ"}], desire_source="きっかけ",
        )
        ref = task["task_ref"]
        self._put_on_desk(ref)

        pages, evicted, dropped = atlas.snapshot_desk(self.adapter, manager=self.manager)

        self.assertEqual(evicted, [])
        self.assertEqual(dropped, [])
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].ref, ref)
        self.assertIn("語源メモをまとめる", pages[0].text)
        self.assertIn("下調べ", pages[0].text)

    def test_snapshot_drops_terminal_task(self):
        # 完了/中止 (TERMINAL_TASK_STATUSES) の目的ノードは soft-delete された
        # Memopedia ページと同じ「無い」扱い — 机から自動で下ろされる
        # (P3c①② 設計: 既存の存在チェックの仕組みに乗せる、新機構は作らない)。
        from saiverse.persona_task_manager import PersonaTaskManager

        task = self._make_task()
        ref = task["task_ref"]
        self._put_on_desk(ref)

        ptm = PersonaTaskManager(self.manager.SessionLocal)
        ptm.update_task_status(
            task["id"], status="completed", actor=None, persona_id="tester",
        )

        pages, evicted, dropped = atlas.snapshot_desk(self.adapter, manager=self.manager)
        self.assertEqual(pages, [])
        self.assertEqual(evicted, [])
        self.assertEqual(dropped, [ref])
        self.assertEqual(self._desk_refs(), set())

    def _desk_refs(self):
        from sai_memory.desk import list_open

        return {item.ref for item in list_open(self.adapter.conn)}


class SnapshotDeskTests(_AtlasTestBase):
    """snapshot_desk — Metabolism 時の机の確定処理 (head 机セクションの材料)。

    戻り値は (pages, evicted, dropped) の 3-tuple — evicted=机の溢れ (LRU) /
    dropped=実体の消失。理由が違うものを同じ「溢れたため」と言わない。
    """

    def test_snapshot_returns_desk_page_views(self):
        page = self._make_memopedia_page(title="開いた本", content="開いた中身")
        ref = f"memopedia:{page.short_id}"
        atlas.open_page(self.adapter, ref, purpose_ref="task:2")

        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)

        self.assertEqual(evicted, [])
        self.assertEqual(dropped, [])
        self.assertEqual(len(pages), 1)
        view = pages[0]
        self.assertEqual(view.ref, ref)
        self.assertEqual(view.purpose_ref, "task:2")
        # text は read_page 相当の整形本文
        self.assertIn("開いた本", view.text)
        self.assertIn("開いた中身", view.text)

    def test_snapshot_pages_ordered_by_opened_at(self):
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        first = self._make_memopedia_page(title="先")
        second = self._make_memopedia_page(title="後")
        atlas.open_page(self.adapter, f"memopedia:{first.short_id}")
        clock.advance_to(datetime(2026, 7, 6, 9, 20, 0))
        atlas.open_page(self.adapter, f"memopedia:{second.short_id}")

        pages, _, _ = atlas.snapshot_desk(self.adapter)
        self.assertEqual(
            [p.ref for p in pages],
            [f"memopedia:{first.short_id}", f"memopedia:{second.short_id}"],
        )

    def test_snapshot_does_not_touch(self):
        # 机の一覧描画は「触った」に数えない — Metabolism のたびに全ページ
        # touch すると鮮度差が消えて LRU が壊れる
        from sai_memory.desk import list_open

        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        page = self._make_memopedia_page()
        atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        before = list_open(self.adapter.conn)[0]

        clock.advance_to(datetime(2026, 7, 6, 10, 0, 0))
        atlas.snapshot_desk(self.adapter)

        after = list_open(self.adapter.conn)[0]
        self.assertEqual(after.last_touched_at, before.last_touched_at)
        self.assertEqual(after.opened_at, before.opened_at)

    def test_snapshot_evicts_over_budget_into_evicted(self):
        # Metabolism 追い出しフック: 開いた時は収まっていた机が、節目の
        # 予算再評価で溢れていたら LRU で棚に戻る → evicted に入る
        clock.enable_virtual(datetime(2026, 7, 6, 9, 0, 0))
        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "10000"  # 開く時は余裕
        old_page = self._make_memopedia_page(title="古", content="あ" * 50)
        new_page = self._make_memopedia_page(title="新", content="い" * 50)
        atlas.open_page(self.adapter, f"memopedia:{old_page.short_id}")
        clock.advance_to(datetime(2026, 7, 6, 9, 10, 0))
        atlas.open_page(self.adapter, f"memopedia:{new_page.short_id}")

        os.environ["SAIVERSE_DESK_BUDGET_CHARS"] = "60"  # 節目には溢れている
        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)

        self.assertEqual(evicted, [f"memopedia:{old_page.short_id}"])  # LRU 最古から
        self.assertEqual(dropped, [])  # 溢れは dropped に混ざらない
        self.assertEqual([p.ref for p in pages], [f"memopedia:{new_page.short_id}"])

    def test_snapshot_drops_lost_entity_refs(self):
        # 実体が消えたページ (不正 ref を直接挿入して再現) → dropped に入る
        from sai_memory.desk import list_open, open_item

        open_item(self.adapter.conn, "memopedia:999")  # 存在しないページ
        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)

        self.assertEqual(pages, [])
        self.assertEqual(evicted, [])  # 実体消失は evicted に混ざらない
        self.assertIn("memopedia:999", dropped)
        self.assertEqual(list_open(self.adapter.conn), [])  # 机からも下りている

    def test_snapshot_drops_unparseable_refs_defensively(self):
        from sai_memory.desk import list_open, open_item

        open_item(self.adapter.conn, "garbage-ref")  # 解釈不能な ref
        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)

        self.assertEqual(pages, [])
        self.assertIn("garbage-ref", dropped)
        self.assertEqual(list_open(self.adapter.conn), [])

    def test_snapshot_survivors_keep_rendering_after_partial_drop(self):
        # 消えた ref と生きたページが混在しても、生きた方は描画される
        from sai_memory.desk import open_item

        page = self._make_memopedia_page(title="生存")
        atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        open_item(self.adapter.conn, "memopedia:999")

        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)
        self.assertEqual([p.ref for p in pages], [f"memopedia:{page.short_id}"])
        self.assertEqual(evicted, [])
        self.assertEqual(dropped, ["memopedia:999"])

    def test_snapshot_empty_desk(self):
        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)
        self.assertEqual(pages, [])
        self.assertEqual(evicted, [])
        self.assertEqual(dropped, [])


class SoftDeletedPageDeskTests(_AtlasTestBase):
    """soft-delete (is_deleted=1) されたページは机の対象外 (P2b テストで発見したバグの回帰)。

    resolve_page_ref / get_page は is_deleted を見ないため、_normalize_ref_for_desk
    側で弾かないと削除済みページが机に居座って本文を head に描画し続けていた。
    """

    def _delete_page(self, page):
        from sai_memory.memopedia import Memopedia

        memopedia = Memopedia(self.adapter.conn, db_lock=self.adapter._db_lock)
        self.assertTrue(memopedia.delete_page(page.id))

    def test_open_page_rejects_soft_deleted_page(self):
        from sai_memory.desk import list_open

        page = self._make_memopedia_page(title="消される本")
        self._delete_page(page)

        result = atlas.open_page(self.adapter, f"memopedia:{page.short_id}")
        self.assertIn("見つかりません", result)
        self.assertEqual(list_open(self.adapter.conn), [])

    def test_snapshot_drops_page_deleted_after_open(self):
        # 机に開いた後にページが削除される (本命シナリオ): snapshot_desk が
        # dropped に入れて机から下ろし、本文は head に残らない
        from sai_memory.desk import list_open

        page = self._make_memopedia_page(title="開いてから消える本", content="消える中身")
        ref = f"memopedia:{page.short_id}"
        atlas.open_page(self.adapter, ref)
        self._delete_page(page)

        pages, evicted, dropped = atlas.snapshot_desk(self.adapter)

        self.assertEqual(pages, [])  # 削除済みページの本文は描画されない
        self.assertEqual(evicted, [])
        self.assertEqual(dropped, [ref])
        self.assertEqual(list_open(self.adapter.conn), [])  # 机からも下りている


class ChronicleShortIdBackfillTests(unittest.TestCase):
    """既存 (short_id 列なし) DB からの一回きり backfill と新規採番。

    P3b (2026-07-11) で物理格納が memopedia_pages に統合されたため、旧
    ``arasuji_entries`` は「物理テーブル」ではなく init_arasuji_tables の
    一回きり移行元、以後は読み取り専用 VIEW になった。書き換え理由:
    このテストは旧テーブルの列を直接構築しており、旧物理レイアウトに
    依存していたため (物理格納を直接検査するテスト)。
    """

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


class ChronicleLegacyMigrationTest(unittest.TestCase):
    """旧 arasuji_entries テーブル → memopedia_pages への一回きり移行 (P3b)。

    P3a の CoreMemoryLegacyMigrationTest (tests/test_core_memory_storage.py)
    と同型: Lv1/Lv2 の親子・incomplete・Track Chronicle・short_id 付き/無し
    の行を含む legacy テーブルを仕込み、init_arasuji_tables 後にページ化・
    親子逆写像・chronicle:N 解決・旧テーブル DROP・冪等性を検証する。
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        # P2a 以降の旧スキーマ (thread_id/origin_track_id/is_incomplete/short_id あり) を再現。
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
                created_at INTEGER NOT NULL,
                thread_id TEXT,
                origin_track_id TEXT,
                is_incomplete INTEGER NOT NULL DEFAULT 0,
                short_id INTEGER
            )
            """
        )
        rows = [
            # Lv1 (未統合) — short_id 付き
            ("lv1-a", 1, "第一のあらすじ", '["m1","m2"]', 100, 200, 2, 2,
             None, 0, 100, None, None, 0, 5),
            # Lv1 (Lv2 に統合済み) — short_id 無し (backfill 対象)
            ("lv1-b", 1, "第二のあらすじ", '["m3"]', 201, 300, 1, 1,
             "lv2-a", 1, 200, None, None, 0, None),
            # Lv2 (lv1-b を統合)
            ("lv2-a", 2, "統合されたあらすじ", '["lv1-b"]', 201, 300, 1, 1,
             None, 0, 250, None, None, 0, None),
            # Track Chronicle の incomplete Lv1
            ("trk-1", 1, "作業途中のトラック記録", '["m4"]', 400, 410, 1, 1,
             None, 0, 400, "thread-x", "track-1", 1, None),
        ]
        self.conn.executemany(
            "INSERT INTO arasuji_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_migration_creates_pages_drops_legacy_table_and_maps_parent(self):
        from sai_memory.arasuji.storage import (
            get_entry,
            get_entry_by_short_id,
            init_arasuji_tables,
        )

        init_arasuji_tables(self.conn)

        # 旧テーブルは移行後 DROP される (旧 path を残さない)。
        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arasuji_entries'"
        ).fetchone()
        self.assertIsNone(legacy)
        # 互換 VIEW は張られている。
        view = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='arasuji_entries'"
        ).fetchone()
        self.assertIsNotNone(view)

        # 未統合 Lv1 (旧 parent_id=NULL) は VIEW 越しに None のまま見える
        # (物理的には root_chronicle 配下に reparent されている)。
        lv1_a = get_entry(self.conn, "lv1-a")
        self.assertIsNone(lv1_a.parent_id)
        self.assertEqual(lv1_a.content, "第一のあらすじ")
        self.assertEqual(lv1_a.source_ids, ["m1", "m2"])
        self.assertEqual(lv1_a.created_at, 100)
        page_a = self.conn.execute(
            "SELECT parent_id FROM memopedia_pages WHERE id = 'lv1-a'"
        ).fetchone()
        self.assertEqual(page_a[0], "root_chronicle")

        # 統合済み Lv1 (旧 parent_id=lv2-a) は VIEW でもそのまま lv2-a を指す。
        lv1_b = get_entry(self.conn, "lv1-b")
        self.assertEqual(lv1_b.parent_id, "lv2-a")
        self.assertTrue(lv1_b.is_consolidated)

        # short_id: 既存値 (5) は保全、欠落分は created_at 昇順で backfill される
        # (lv1-a=5 は既存値なので、他行の backfill 開始点は MAX+1=6 から)。
        # get_entry() は元々 short_id を SELECT しない (旧実装から変更なし) ので、
        # VIEW を直接引いて確認する。
        entry_a = get_entry_by_short_id(self.conn, 5)
        self.assertEqual(entry_a.id, "lv1-a")
        lv1_b_short_id = self.conn.execute(
            "SELECT short_id FROM arasuji_entries WHERE id = 'lv1-b'"
        ).fetchone()[0]
        self.assertIsNotNone(lv1_b_short_id)
        self.assertNotEqual(lv1_b_short_id, 5)

        # Track Chronicle (origin_track_id/thread_id/is_incomplete) が保全されている。
        trk = get_entry(self.conn, "trk-1")
        self.assertEqual(trk.origin_track_id, "track-1")
        self.assertTrue(trk.is_incomplete)

        # chronicle:N (short_id) 解決で lv1-a を引ける。
        by_short = get_entry_by_short_id(self.conn, entry_a.short_id)
        self.assertEqual(by_short.id, "lv1-a")

    def test_migration_is_idempotent(self):
        from sai_memory.arasuji.storage import get_entries_by_level, init_arasuji_tables

        init_arasuji_tables(self.conn)
        first = {e.id: e.content for e in get_entries_by_level(self.conn, 1)}
        init_arasuji_tables(self.conn)  # 2回目は旧テーブルが無いので no-op
        second = {e.id: e.content for e in get_entries_by_level(self.conn, 1)}
        self.assertEqual(first, second)

        # 新規追加は移行済みの最大 short_id の続きから採番される (衝突なし)。
        from sai_memory.arasuji.storage import create_entry
        new_entry = create_entry(
            self.conn, level=1, content="移行後の新規", source_ids=[],
            source_count=0, message_count=0,
        )
        existing_short_ids = {e.short_id for e in get_entries_by_level(self.conn, 1)}
        self.assertNotIn(new_entry.short_id, existing_short_ids - {new_entry.short_id})

    def test_migration_resumes_after_a_run_that_committed_pages_but_not_the_drop(self):
        """前回の移行が「ページの commit まで済んで DROP TABLE で倒れた」状態から
        再開できる。2026-09-02 の報告 (macOS、旧形式 1443 件): この状態で毎回
        ``UNIQUE constraint failed: memopedia_pages.id`` になり、Chronicle が空に
        見え続け、開くたびの失敗が Memopedia の読み込みを待たせていた。"""
        from sai_memory.arasuji.storage import (
            CATEGORY_CHRONICLE,
            ROOT_CHRONICLE_ID,
            _ensure_root_chronicle,
            get_entries_by_level,
            get_entry,
            init_arasuji_tables,
        )
        from sai_memory.memopedia.storage import create_page, init_memopedia_tables

        # 前回の途中経過を再現: 旧テーブルは残ったまま、一部の行だけがページになっている。
        # lv1-b は旧テーブルでは short_id 無し → 前回の移行が 6 を採番してページに書いた。
        init_memopedia_tables(self.conn)
        _ensure_root_chronicle(self.conn)
        for page_id, title, content, sid in (
            ("lv1-a", "chronicle:5", "第一のあらすじ", 5),
            ("lv1-b", "chronicle:6", "第二のあらすじ", 6),
        ):
            create_page(
                self.conn,
                parent_id=ROOT_CHRONICLE_ID,
                title=title,
                content=content,
                category=CATEGORY_CHRONICLE,
                is_trunk=False,
                metadata={
                    "level": 1, "source_ids": ["m1"], "start_time": 100, "end_time": 200,
                    "source_count": 1, "message_count": 1, "is_consolidated": 0,
                    "is_incomplete": 0, "origin_track_id": None, "thread_id": None,
                    "short_id": sid,
                },
                page_id=page_id,
            )
        self.conn.commit()

        with self.assertLogs("sai_memory.arasuji.storage", level="WARNING") as logs:
            init_arasuji_tables(self.conn)  # 以前はここで IntegrityError
        self.assertTrue(any("resumed" in line for line in logs.output))

        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arasuji_entries'"
        ).fetchone()
        self.assertIsNone(legacy)
        # 既にあったページは一つのまま、残りの行は今回ページになった。
        ids = sorted(e.id for level in (1, 2) for e in get_entries_by_level(self.conn, level))
        self.assertEqual(ids, ["lv1-a", "lv1-b", "lv2-a", "trk-1"])
        self.assertEqual(get_entry(self.conn, "lv1-a").content, "第一のあらすじ")
        self.assertEqual(get_entry(self.conn, "lv2-a").content, "統合されたあらすじ")
        # 今回移行した short_id 無しの行 (lv2-a / trk-1) は、前回ページ側で採番済みの
        # 6 を再利用せず、その続きから番号を受け取る。
        short_ids = [
            self.conn.execute(
                "SELECT short_id FROM arasuji_entries WHERE id = ?", (i,)
            ).fetchone()[0]
            for i in ("lv1-a", "lv1-b", "lv2-a", "trk-1")
        ]
        self.assertEqual(len(short_ids), len(set(short_ids)), short_ids)
        self.assertEqual(short_ids[:2], [5, 6])
        self.assertTrue(all(s > 6 for s in short_ids[2:]), short_ids)


    def test_migration_retries_once_when_another_connection_wins_the_race(self):
        """API はリクエストごとに移行へ入るので、二接続が同時に走ると負けた側は勝った側が
        commit した id で IntegrityError を踏む。それは「もう写されている」という答えなので、
        rollback して一度だけやり直し、既存ページを飛ばして DROP まで進む。"""
        import sqlite3 as _sqlite3
        from unittest import mock

        from sai_memory.arasuji import storage as arasuji_storage
        from sai_memory.arasuji.storage import (
            CATEGORY_CHRONICLE,
            ROOT_CHRONICLE_ID,
            get_entries_by_level,
            init_arasuji_tables,
        )
        from sai_memory.memopedia import storage as memopedia_storage

        winner = _sqlite3.connect(self.db_path)
        real_create_page = memopedia_storage.create_page
        state = {"raced": False}

        def racing_create_page(conn, **kwargs):
            # 移行の最初の行 (lv1-a) の直前に「別の接続」が同じ id を commit してしまう
            # (root_chronicle の用意など、移行以外の create_page は素通し)。
            if not state["raced"] and kwargs.get("page_id") == "lv1-a":
                state["raced"] = True
                memopedia_storage.init_memopedia_tables(winner)
                arasuji_storage._ensure_root_chronicle(winner)
                real_create_page(
                    winner,
                    parent_id=ROOT_CHRONICLE_ID,
                    title=kwargs["title"],
                    content=kwargs["content"],
                    category=CATEGORY_CHRONICLE,
                    is_trunk=False,
                    metadata=kwargs["metadata"],
                    page_id=kwargs["page_id"],
                )
                winner.close()
            return real_create_page(conn, **kwargs)

        with mock.patch.object(memopedia_storage, "create_page", side_effect=racing_create_page):
            init_arasuji_tables(self.conn)  # 一度 IntegrityError → rollback → 再試行で完走

        self.assertTrue(state["raced"])
        legacy = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='arasuji_entries'"
        ).fetchone()
        self.assertIsNone(legacy)
        ids = sorted(e.id for level in (1, 2) for e in get_entries_by_level(self.conn, level))
        self.assertEqual(ids, ["lv1-a", "lv1-b", "lv2-a", "trk-1"])
        # 同じ id のページが二重になっていない。
        count = self.conn.execute(
            "SELECT COUNT(*) FROM memopedia_pages WHERE category = ? AND is_trunk = 0",
            (CATEGORY_CHRONICLE,),
        ).fetchone()[0]
        self.assertEqual(count, 4)  # root_chronicle (trunk) は数えない

class RefGrammarAcceptanceTests(_AtlasTestBase):
    """A1「入口は広く、出口は一本」の恒久検査 (2026-07-15)。

    発端: 自動想起が流す ``saiverse://self/memopedia/45`` を統一グラマーの不変条件
    I2 に従って ``memopedia:45`` へ正しく変換したペルソナを、Atlas が自前パースで
    蹴っていた。Atlas が書式を自前で持つ限り同じ乖離がまた生えるため、「旧 prefix も
    正典単語も URI も同じページに着く」ことをここで固定する。
    """

    def _page(self):
        return self._make_memopedia_page()

    def test_all_notations_reach_the_same_page(self):
        page = self._page()
        sid = page.short_id
        canonical = atlas.read_page(self.adapter, f"memopedia:{sid}")
        for notation in (
            f"m:{sid}",                              # 旧 prefix (ペルソナの記憶に焼き付いている)
            f"saiverse://self/memopedia/{sid}",      # URI (自動想起が最も多く流す形)
            f"  memopedia:{sid}  ",                  # 前後空白
        ):
            with self.subTest(notation=notation):
                self.assertEqual(atlas.read_page(self.adapter, notation), canonical)

    def test_output_uses_canonical_word_only(self):
        # 旧 prefix で入力しても、返る文字列は正典単語に揃う (出口は一本)
        page = self._page()
        result = atlas.read_page(self.adapter, f"m:{page.short_id}")
        self.assertIn(f"memopedia:{page.short_id}", result)
        self.assertNotIn(f"m:{page.short_id}", result)

    def test_other_persona_uri_is_refused_not_misread(self):
        # 他ペルソナの URI を自分の番号として読んでしまう取り違えを防ぐ
        page = self._page()
        with self.assertRaises(atlas.AtlasRefError):
            atlas._parse_ref(f"saiverse://city_a/someone/memopedia/{page.short_id}")

    def test_unknown_notation_error_teaches_the_format(self):
        # 蹴るときは書式を教える (発端の事故では例示が無く、ペルソナは直せなかった)
        with self.assertRaises(atlas.AtlasRefError) as ctx:
            atlas._parse_ref("memopedeia:45")
        self.assertIn("memopedia:", str(ctx.exception))

    def test_valid_grammar_but_not_an_atlas_page_is_refused(self):
        # track:2 は文法としては正しいが地図帳のページではない
        with self.assertRaises(atlas.AtlasRefError) as ctx:
            atlas._parse_ref("track:2")
        self.assertIn("track", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
