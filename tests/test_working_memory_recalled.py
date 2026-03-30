"""Tests for working memory recalled_ids management."""

import threading
import unittest

from sai_memory.memory.storage import init_db
from saiverse_memory.adapter import SAIMemoryAdapter


class TestRecalledIds(unittest.TestCase):
    """Test recalled_ids CRUD in SAIMemoryAdapter."""

    def setUp(self):
        self.conn = init_db(":memory:", check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS working_memory (
                persona_id TEXT PRIMARY KEY,
                data TEXT,
                updated_at REAL
            )
        """)
        self.conn.commit()

        self.adapter = SAIMemoryAdapter.__new__(SAIMemoryAdapter)
        self.adapter.persona_id = "test_persona"
        self.adapter.conn = self.conn
        self.adapter._db_lock = threading.Lock()

    def tearDown(self):
        self.conn.close()

    def _make_ready(self):
        type(self.adapter)._ready = property(lambda self: True)

    def test_get_recalled_ids_empty(self):
        self._make_ready()
        self.assertEqual(self.adapter.get_recalled_ids(), [])

    def test_add_recalled_id(self):
        self._make_ready()
        self.adapter.add_recalled_id("chronicle", "entry_1", "Test Entry", "uri://1")
        ids = self.adapter.get_recalled_ids()
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids[0]["type"], "chronicle")
        self.assertEqual(ids[0]["id"], "entry_1")
        self.assertIn("recalled_at", ids[0])

    def test_add_duplicate_refreshes_position(self):
        self._make_ready()
        self.adapter.add_recalled_id("chronicle", "entry_1", "First", "uri://1")
        self.adapter.add_recalled_id("memopedia", "page_1", "Second", "uri://2")
        self.adapter.add_recalled_id("chronicle", "entry_1", "First Updated", "uri://1")
        ids = self.adapter.get_recalled_ids()
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0]["id"], "page_1")
        self.assertEqual(ids[1]["id"], "entry_1")
        self.assertEqual(ids[1]["title"], "First Updated")

    def test_fifo_eviction(self):
        self._make_ready()
        for i in range(12):
            self.adapter.add_recalled_id("chronicle", f"entry_{i}", f"E{i}", f"uri://{i}")
        ids = self.adapter.get_recalled_ids()
        self.assertEqual(len(ids), 10)
        remaining = [item["id"] for item in ids]
        self.assertNotIn("entry_0", remaining)
        self.assertNotIn("entry_1", remaining)
        self.assertIn("entry_2", remaining)
        self.assertIn("entry_11", remaining)

    def test_remove_recalled_id(self):
        self._make_ready()
        self.adapter.add_recalled_id("chronicle", "entry_1", "Test", "uri://1")
        self.adapter.add_recalled_id("memopedia", "page_1", "Test2", "uri://2")
        self.assertTrue(self.adapter.remove_recalled_id("entry_1"))
        ids = self.adapter.get_recalled_ids()
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids[0]["id"], "page_1")

    def test_remove_nonexistent_returns_false(self):
        self._make_ready()
        self.assertFalse(self.adapter.remove_recalled_id("nonexistent"))

    def test_clear_recalled_ids(self):
        self._make_ready()
        self.adapter.add_recalled_id("chronicle", "entry_1", "Test", "uri://1")
        self.adapter.add_recalled_id("memopedia", "page_1", "Test2", "uri://2")
        self.assertEqual(self.adapter.clear_recalled_ids(), 2)
        self.assertEqual(self.adapter.get_recalled_ids(), [])

    def test_clear_empty_returns_zero(self):
        self._make_ready()
        self.assertEqual(self.adapter.clear_recalled_ids(), 0)

    def test_not_ready_returns_defaults(self):
        type(self.adapter)._ready = property(lambda self: False)
        self.assertEqual(self.adapter.get_recalled_ids(), [])
        self.adapter.add_recalled_id("chronicle", "entry_1", "Test", "uri://1")
        self.assertEqual(self.adapter.get_recalled_ids(), [])
        self.assertFalse(self.adapter.remove_recalled_id("entry_1"))
        self.assertEqual(self.adapter.clear_recalled_ids(), 0)

    def test_preserves_other_working_memory_keys(self):
        self._make_ready()
        wm = {"situation_snapshot": {"building": "hall_1"}, "custom_key": 42}
        self.adapter.save_working_memory(wm)
        self.adapter.add_recalled_id("chronicle", "entry_1", "Test", "uri://1")
        wm = self.adapter.load_working_memory()
        self.assertEqual(wm["situation_snapshot"]["building"], "hall_1")
        self.assertEqual(wm["custom_key"], 42)
        self.assertEqual(len(wm["recalled_ids"]), 1)


if __name__ == "__main__":
    unittest.main()
