"""Tests for unified recall: embedding storage and search."""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock

import numpy as np

from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import create_entry
from sai_memory.memopedia import init_memopedia_tables
from sai_memory.memopedia.storage import create_page, create_fragment
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.storage import add_message, init_db, replace_message_embeddings
from sai_memory.unified_recall import (
    RecallHit,
    _keyword_excerpt,
    count_chronicle_embeddings,
    count_fragment_embeddings,
    count_memopedia_embeddings,
    embed_chronicle_entries,
    embed_memopedia_fragments,
    embed_memopedia_pages,
    get_chronicle_entries_without_embeddings,
    get_fragments_without_embeddings,
    get_memopedia_pages_without_embeddings,
    reconstruct_message_excerpt,
    store_chronicle_embedding,
    store_fragment_embedding,
    store_memopedia_embedding,
    unified_recall,
)


class DummyEmbedder:
    """Embedder that returns deterministic vectors based on text hash."""
    model_name = "test"

    def embed(self, texts, *, is_query=False):
        vectors = []
        for text in texts:
            # Create a deterministic vector from text
            np.random.seed(hash(text) % (2**31))
            vec = np.random.randn(64).tolist()
            # Normalize
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = (np.array(vec) / norm).tolist()
            vectors.append(vec)
        return vectors


class TestChronicleEmbeddings(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_store_and_count(self):
        create_entry(
            self.conn, level=1, content="test summary",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20, entry_id="e1",
        )
        store_chronicle_embedding(self.conn, "e1", [0.1, 0.2, 0.3])
        self.assertEqual(count_chronicle_embeddings(self.conn), 1)

    def test_entries_without_embeddings(self):
        create_entry(
            self.conn, level=1, content="has embedding",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20, entry_id="e1",
        )
        create_entry(
            self.conn, level=1, content="no embedding",
            source_ids=[], start_time=2000, end_time=3000,
            source_count=1, message_count=20, entry_id="e2",
        )
        store_chronicle_embedding(self.conn, "e1", [0.1, 0.2])

        missing = get_chronicle_entries_without_embeddings(self.conn, level=1)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0][0], "e2")

    def test_batch_embed(self):
        for i in range(5):
            create_entry(
                self.conn, level=1, content=f"summary {i}",
                source_ids=[], start_time=i * 1000, end_time=(i + 1) * 1000,
                source_count=1, message_count=20,
            )

        embedder = DummyEmbedder()
        n = embed_chronicle_entries(self.conn, embedder, level=1)
        self.assertEqual(n, 5)
        self.assertEqual(count_chronicle_embeddings(self.conn), 5)

        # Running again should embed 0 (all done)
        n = embed_chronicle_entries(self.conn, embedder, level=1)
        self.assertEqual(n, 0)


class TestMemopediaEmbeddings(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        init_memopedia_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_store_and_count(self):
        create_page(
            self.conn, parent_id="root_people", title="Test",
            summary="A test page", content="", category="people",
        )
        pages = get_memopedia_pages_without_embeddings(self.conn)
        self.assertEqual(len(pages), 1)

        store_memopedia_embedding(self.conn, pages[0][0], [0.1, 0.2])
        self.assertEqual(count_memopedia_embeddings(self.conn), 1)

    def test_root_pages_excluded(self):
        # Root pages should not appear in missing list
        missing = get_memopedia_pages_without_embeddings(self.conn)
        self.assertEqual(len(missing), 0)  # Only root pages exist

    def test_batch_embed(self):
        for i in range(3):
            create_page(
                self.conn, parent_id="root_terms", title=f"Term {i}",
                summary=f"About term {i}", content="", category="terms",
            )

        embedder = DummyEmbedder()
        n = embed_memopedia_pages(self.conn, embedder)
        self.assertEqual(n, 3)
        self.assertEqual(count_memopedia_embeddings(self.conn), 3)


class TestFragmentEmbeddings(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        init_memopedia_tables(self.conn)
        # Create an entity page to host fragments
        self.page = create_page(
            self.conn, parent_id="root_people", title="PostgreSQL",
            summary="A relational database", content="", category="terms",
        )

    def tearDown(self):
        self.conn.close()

    def test_store_and_count(self):
        frag = create_fragment(
            self.conn, entity_id=self.page.id,
            content="SSLエラーが発生した", source_date="2026-05-30",
        )
        store_fragment_embedding(self.conn, frag.id, [0.1, 0.2, 0.3])
        self.assertEqual(count_fragment_embeddings(self.conn), 1)

    def test_fragments_without_embeddings(self):
        f1 = create_fragment(
            self.conn, entity_id=self.page.id,
            content="has embedding", source_date="2026-05-30",
        )
        f2 = create_fragment(
            self.conn, entity_id=self.page.id,
            content="no embedding", source_date="2026-05-30",
        )
        store_fragment_embedding(self.conn, f1.id, [0.1, 0.2])

        missing = get_fragments_without_embeddings(self.conn)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0][0], f2.id)

    def test_batch_embed(self):
        for i in range(5):
            create_fragment(
                self.conn, entity_id=self.page.id,
                content=f"fact {i} about PostgreSQL",
                source_date="2026-05-30",
            )

        embedder = DummyEmbedder()
        n = embed_memopedia_fragments(self.conn, embedder)
        self.assertEqual(n, 5)
        self.assertEqual(count_fragment_embeddings(self.conn), 5)

        # Running again should embed 0 (all done)
        n = embed_memopedia_fragments(self.conn, embedder)
        self.assertEqual(n, 0)


class TestUnifiedRecall(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        init_memopedia_tables(self.conn)
        self.embedder = DummyEmbedder()

    def tearDown(self):
        self.conn.close()

    def test_search_returns_results(self):
        # Create and embed Chronicle entries
        create_entry(
            self.conn, level=1, content="まはーと猫について話した",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20, entry_id="c1",
        )
        embed_chronicle_entries(self.conn, self.embedder, level=1)

        # Create and embed Memopedia pages
        create_page(
            self.conn, parent_id="root_people", title="まはー",
            summary="ユーザー。猫が好き。", content="", category="people",
        )
        embed_memopedia_pages(self.conn, self.embedder)

        # Search
        hits = unified_recall(self.conn, self.embedder, "猫", topk=5)
        self.assertGreater(len(hits), 0)

        # Check hit structure
        for hit in hits:
            self.assertIsInstance(hit, RecallHit)
            self.assertIn(hit.source_type, ("chronicle", "memopedia"))
            self.assertTrue(hit.uri.startswith("saiverse://"))
            self.assertIsInstance(hit.score, float)

    def test_search_chronicle_only(self):
        create_entry(
            self.conn, level=1, content="test content",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20,
        )
        embed_chronicle_entries(self.conn, self.embedder, level=1)

        create_page(
            self.conn, parent_id="root_terms", title="Test",
            summary="test", content="", category="terms",
        )
        embed_memopedia_pages(self.conn, self.embedder)

        hits = unified_recall(
            self.conn, self.embedder, "test",
            search_chronicle=True, search_memopedia=False,
        )
        for hit in hits:
            self.assertEqual(hit.source_type, "chronicle")

    def test_search_memopedia_only(self):
        create_entry(
            self.conn, level=1, content="test content",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20,
        )
        embed_chronicle_entries(self.conn, self.embedder, level=1)

        create_page(
            self.conn, parent_id="root_terms", title="Test",
            summary="test", content="", category="terms",
        )
        embed_memopedia_pages(self.conn, self.embedder)

        hits = unified_recall(
            self.conn, self.embedder, "test",
            search_chronicle=False, search_memopedia=True,
        )
        for hit in hits:
            self.assertEqual(hit.source_type, "memopedia")

    def test_empty_search(self):
        hits = unified_recall(self.conn, self.embedder, "anything", topk=5)
        self.assertEqual(len(hits), 0)

    def test_topk_limit(self):
        for i in range(10):
            create_entry(
                self.conn, level=1, content=f"summary {i}",
                source_ids=[], start_time=i * 1000, end_time=(i + 1) * 1000,
                source_count=1, message_count=20,
            )
        embed_chronicle_entries(self.conn, self.embedder, level=1)

        hits = unified_recall(self.conn, self.embedder, "test", topk=3)
        self.assertLessEqual(len(hits), 3)

    def test_search_fragments(self):
        # Create entity page + fragments
        page = create_page(
            self.conn, parent_id="root_terms", title="PostgreSQL",
            summary="A relational database", content="", category="terms",
        )
        create_fragment(
            self.conn, entity_id=page.id,
            content="SSLエラーが発生して接続できなかった",
            source_date="2026-05-30",
        )
        create_fragment(
            self.conn, entity_id=page.id,
            content="バックアップ設定を変更した",
            source_date="2026-05-30",
        )
        embed_memopedia_pages(self.conn, self.embedder)
        embed_memopedia_fragments(self.conn, self.embedder)

        hits = unified_recall(
            self.conn, self.embedder, "SSL",
            search_chronicle=False, search_memopedia=False, search_fragments=True,
        )
        self.assertGreater(len(hits), 0)
        for hit in hits:
            self.assertEqual(hit.source_type, "fragment")
            self.assertEqual(hit.entity_id, page.id)
            self.assertTrue(hit.uri.startswith("saiverse://"))

    def test_fragment_with_chronicle_link(self):
        # Create Chronicle entry
        create_entry(
            self.conn, level=1, content="PostgreSQLのSSL設定を修正した",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20, entry_id="c1",
        )
        # Create entity page + fragment linked to Chronicle
        page = create_page(
            self.conn, parent_id="root_terms", title="PostgreSQL",
            summary="A database", content="", category="terms",
        )
        create_fragment(
            self.conn, entity_id=page.id,
            content="SSL証明書の期限切れ",
            chronicle_entry_id="c1",
            source_date="2026-05-30",
        )
        embed_memopedia_fragments(self.conn, self.embedder)

        hits = unified_recall(
            self.conn, self.embedder, "SSL",
            search_chronicle=False, search_memopedia=False, search_fragments=True,
        )
        fragment_hits = [h for h in hits if h.source_type == "fragment"]
        self.assertGreater(len(fragment_hits), 0)
        self.assertEqual(fragment_hits[0].chronicle_entry_id, "c1")

    def test_search_all_sources(self):
        # Set up all three sources
        create_entry(
            self.conn, level=1, content="データベース移行の会話",
            source_ids=[], start_time=1000, end_time=2000,
            source_count=1, message_count=20,
        )
        embed_chronicle_entries(self.conn, self.embedder, level=1)

        page = create_page(
            self.conn, parent_id="root_terms", title="PostgreSQL",
            summary="リレーショナルデータベース", content="", category="terms",
        )
        embed_memopedia_pages(self.conn, self.embedder)

        create_fragment(
            self.conn, entity_id=page.id,
            content="データベースのバックアップ設定",
            source_date="2026-05-30",
        )
        embed_memopedia_fragments(self.conn, self.embedder)

        hits = unified_recall(self.conn, self.embedder, "データベース", topk=10)
        self.assertGreater(len(hits), 0)


class TestMessageExcerptReconstruction(unittest.TestCase):
    """rev4: メッセージヒットの表示 content を「マッチ箇所の抜粋」にする。"""

    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_reconstruct_picks_matched_chunk(self):
        # 長文メッセージを実際の chunk_text と同じパラメータ (既定 120/480) で
        # 分割し、embedding 行数と一致させた状態で、後半のチャンクを指定して
        # 復元すると、そのチャンクのテキストが返る (先頭ではない)。
        head = "無関係な導入部の文章です。" * 30
        target = "アイフィについての固有の話題がここにあります。"
        tail = "さらに続く関係ない話です。" * 30
        content = head + target + tail

        msg_id = add_message(self.conn, thread_id="t1", role="user", content=content)
        chunks = chunk_text(content, min_chars=120, max_chars=480)
        # ターゲット文を含むチャンクの index を特定
        target_idx = next(i for i, c in enumerate(chunks) if target in c)
        self.assertGreater(target_idx, 0, "test setup should place target beyond chunk 0")
        replace_message_embeddings(self.conn, msg_id, [[0.0] * 4 for _ in chunks])

        excerpt, used_idx = reconstruct_message_excerpt(self.conn, msg_id, target_idx, content)
        self.assertEqual(used_idx, target_idx)
        self.assertIn(target, excerpt)
        # 先頭200字だけのフォールバックでは含まれないはず (先頭は無関係な導入部)
        self.assertNotIn(target, content[:200])

    def test_reconstruct_falls_back_on_chunk_count_mismatch(self):
        # embedding 行数と再分割チャンク数が食い違う場合 (パラメータ変更等を模す)、
        # 整合性ガードにより先頭200字へフォールバックする。
        content = "先頭の文章です。" + "本文が続きます。" * 50
        msg_id = add_message(self.conn, thread_id="t1", role="user", content=content)
        # 実際のチャンク数とは無関係な行数を挿入 (1行だけ = 明らかに不一致)
        replace_message_embeddings(self.conn, msg_id, [[0.0] * 4])

        excerpt, used_idx = reconstruct_message_excerpt(self.conn, msg_id, 0, content)
        self.assertIsNone(used_idx)
        self.assertEqual(excerpt, content[:200])

    def test_reconstruct_empty_content(self):
        excerpt, used_idx = reconstruct_message_excerpt(self.conn, "m1", 0, "")
        self.assertEqual(excerpt, "")
        self.assertIsNone(used_idx)

    def test_keyword_excerpt_centers_on_match(self):
        content = "前置き。" * 60 + "アイフィの話をしよう。" + "後書き。" * 60
        excerpt = _keyword_excerpt(content, ["アイフィ"])
        self.assertIn("アイフィ", excerpt)
        # 先頭200字だけでは含まれない (前置きが200字を超えている)
        self.assertNotIn("アイフィ", content[:200])

    def test_keyword_excerpt_no_match_falls_back_to_head(self):
        content = "関係ない内容です。" * 60
        excerpt = _keyword_excerpt(content, ["存在しないキーワード"])
        self.assertEqual(excerpt, content[:200])

    def test_message_hit_content_is_excerpt_not_head(self):
        # unified_recall 経由で、embedding のみでヒットするケース (クエリ語がキーワード
        # としては本文と一致しない) を作り、表示 content が先頭200字固定でなく
        # マッチしたチャンクの抜粋になっていること・chunk_index が埋め込み由来で
        # 設定されることを確認する。
        head = "全然関係ない話が延々と続きます。" * 15
        target = "アイフィという固有の名前についての詳しい相談をしたい"
        content = head + target
        msg_id = add_message(self.conn, thread_id="t1", role="user", content=content)
        chunks = chunk_text(content, min_chars=120, max_chars=480)
        target_idx = next(i for i, c in enumerate(chunks) if target in c)
        self.assertGreater(target_idx, 0)

        class TargetedEmbedder:
            model_name = "test"

            def embed(self, texts, *, is_query=False):
                out = []
                for t in texts:
                    if is_query or target in t:
                        out.append([1.0, 0.0])
                    else:
                        out.append([0.0, 1.0])
                return out

        embedder = TargetedEmbedder()
        vectors = embedder.embed(chunks, is_query=False)
        replace_message_embeddings(self.conn, msg_id, vectors)

        # クエリに使う語は本文中の語と重ならない語にして、キーワードパスでは
        # ヒットしない (embedding 経由のみ) 状況を作る。
        hits = unified_recall(
            self.conn, embedder, "全く別の検索クエリ",
            search_chronicle=False, search_memopedia=False,
            search_fragments=False, search_messages=True,
        )
        message_hits = [h for h in hits if h.source_type == "message"]
        self.assertEqual(len(message_hits), 1)
        self.assertIn(target, message_hits[0].content)
        self.assertEqual(message_hits[0].chunk_index, target_idx)


if __name__ == "__main__":
    unittest.main()
