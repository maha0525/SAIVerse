"""Tests for Memopedia rollback using before-snapshot columns."""

import threading
import unittest

from sai_memory.memopedia import Memopedia
from sai_memory.memopedia.storage import (
    get_edit_by_id,
    get_page_edit_history,
)
from sai_memory.memory.storage import init_db


class TestMemopediaRollback(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = init_db(":memory:")
        self.lock = threading.RLock()
        self.memo = Memopedia(self.conn, db_lock=self.lock)
        # Use the seeded "people" trunk as parent.
        self.parent_id = "root_people"

    def tearDown(self) -> None:
        self.conn.close()

    def test_rollback_after_update_restores_before_state(self) -> None:
        page = self.memo.create_page(
            parent_id=self.parent_id,
            title="Alice",
            summary="initial summary",
            content="initial content",
        )

        updated = self.memo.update_page(
            page.id,
            title="Alice (updated)",
            summary="updated summary",
            content="updated content",
        )
        self.assertIsNotNone(updated)

        history = get_page_edit_history(self.conn, page.id)
        update_edit = next(e for e in history if e.edit_type == "update")
        self.assertEqual(update_edit.before_title, "Alice")
        self.assertEqual(update_edit.before_summary, "initial summary")
        self.assertEqual(update_edit.before_content, "initial content")

        rolled = self.memo.rollback_page(page.id, update_edit.id)
        self.assertIsNotNone(rolled)
        self.assertEqual(rolled.title, "Alice")
        self.assertEqual(rolled.summary, "initial summary")
        self.assertEqual(rolled.content, "initial content")

    def test_rollback_partial_local_edit_restores_full_content(self) -> None:
        # Codex P1: long page, edit only one line in the middle. With unified
        # diff (n=3), out-of-hunk lines are absent — diff-based reconstruction
        # used to drop title/summary and most of the body. Snapshot fixes this.
        long_content = "\n".join(f"line {i}" for i in range(1, 51))
        page = self.memo.create_page(
            parent_id=self.parent_id,
            title="Long page",
            summary="a long page",
            content=long_content,
        )

        # Edit only one line deep in the middle.
        new_content = long_content.replace("line 25", "line 25 EDITED")
        self.memo.update_page(page.id, content=new_content)

        history = get_page_edit_history(self.conn, page.id)
        update_edit = next(e for e in history if e.edit_type == "update")

        rolled = self.memo.rollback_page(page.id, update_edit.id)
        self.assertIsNotNone(rolled)
        self.assertEqual(rolled.title, "Long page")
        self.assertEqual(rolled.summary, "a long page")
        self.assertEqual(rolled.content, long_content)

    def test_rollback_legacy_entry_without_snapshot_returns_none(self) -> None:
        page = self.memo.create_page(
            parent_id=self.parent_id,
            title="Bob",
            summary="initial",
            content="initial",
        )
        self.memo.update_page(page.id, content="updated")

        history = get_page_edit_history(self.conn, page.id)
        update_edit = next(e for e in history if e.edit_type == "update")

        # Simulate a legacy entry by clearing the snapshot columns.
        self.conn.execute(
            "UPDATE memopedia_page_edit_history "
            "SET before_title=NULL, before_summary=NULL, before_content=NULL "
            "WHERE id = ?",
            (update_edit.id,),
        )
        self.conn.commit()

        legacy = get_edit_by_id(self.conn, update_edit.id)
        self.assertIsNone(legacy.before_title)

        result = self.memo.rollback_page(page.id, update_edit.id)
        self.assertIsNone(result)

        # Page should remain in its updated state, untouched.
        page_after = self.memo.get_page(page.id)
        self.assertEqual(page_after.content, "updated")


if __name__ == "__main__":
    unittest.main()
