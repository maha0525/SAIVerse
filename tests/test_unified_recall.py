"""Tests for unified recall: embedding storage and search."""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock

import numpy as np

from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import create_entry
from sai_memory.memopedia import init_memopedia_tables
from sai_memory.memopedia.storage import create_page
from sai_memory.memory.storage import init_db
from sai_memory.unified_recall import (
    RecallHit,
    count_chronicle_embeddings,
    count_memopedia_embeddings,
    embed_chronicle_entries,
    embed_memopedia_pages,
    get_chronicle_entries_without_embeddings,
    get_memopedia_pages_without_embeddings,
    store_chronicle_embedding,
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


if __name__ == "__main__":
    unittest.main()
