"""Tests for memory note organization (exec phase)."""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from sai_memory.memory.note_executor import (
    ExecResult,
    _parse_json_response,
    execute_group,
)
from sai_memory.memory.storage import (
    add_memory_notes,
    count_unplanned_notes,
    count_unresolved_notes,
    get_unplanned_notes,
    init_db,
    resolve_memory_notes,
    set_note_plan,
)


class TestParseJsonResponse(unittest.TestCase):

    def test_clean_json(self):
        result = _parse_json_response('{"key": "value"}')
        self.assertEqual(result, {"key": "value"})

    def test_markdown_wrapped(self):
        result = _parse_json_response('```json\n{"key": "value"}\n```')
        self.assertEqual(result, {"key": "value"})

    def test_empty(self):
        self.assertIsNone(_parse_json_response(""))

    def test_invalid_json(self):
        self.assertIsNone(_parse_json_response("not json"))


class TestExecuteGroup(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.client = MagicMock()
        self.memopedia = MagicMock()

    def tearDown(self):
        self.conn.close()

    def _setup_planned_notes(self, action, target_page_id=None,
                              suggested_title=None, target_category=None):
        """Create notes and set plan metadata."""
        notes = add_memory_notes(self.conn, thread_id="t1",
                                 notes=["fact A", "fact B", "fact C"])
        set_note_plan(
            self.conn, [n.id for n in notes],
            group_label="test_group",
            action=action,
            target_page_id=target_page_id,
            suggested_title=suggested_title,
            target_category=target_category,
        )
        return notes

    def test_append_to_existing(self):
        self._setup_planned_notes(
            "append_to_existing", target_page_id="page_123",
        )

        # Mock page
        mock_page = MagicMock()
        mock_page.title = "Test Page"
        mock_page.content = "Existing content."
        mock_page.summary = "A test page"
        self.memopedia.get_page.return_value = mock_page

        # Mock LLM response
        self.client.generate.return_value = json.dumps({
            "updated_content": "Existing content.\n\nNew facts added.",
            "updated_summary": "Updated summary",
            "excluded_note_ids": [],
        })

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "append_to_existing")
        self.assertEqual(result.resolved_count, 3)
        self.assertEqual(result.excluded_count, 0)
        self.assertEqual(count_unresolved_notes(self.conn), 0)

        # Verify memopedia was updated
        self.memopedia.update_page.assert_called_once()

    def test_append_with_exclusion(self):
        notes = self._setup_planned_notes(
            "append_to_existing", target_page_id="page_123",
        )

        mock_page = MagicMock()
        mock_page.title = "Test"
        mock_page.content = "Content"
        mock_page.summary = "Summary"
        self.memopedia.get_page.return_value = mock_page

        # Exclude the third note
        self.client.generate.return_value = json.dumps({
            "updated_content": "Updated content.",
            "updated_summary": "Summary",
            "excluded_note_ids": [notes[2].id],
            "exclude_reasons": "Unrelated topic",
        })

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )

        self.assertEqual(result.resolved_count, 2)
        self.assertEqual(result.excluded_count, 1)

        # Excluded note should be back to unplanned
        unplanned = get_unplanned_notes(self.conn)
        self.assertEqual(len(unplanned), 1)
        self.assertEqual(unplanned[0].id, notes[2].id)

    def test_create_new(self):
        self._setup_planned_notes(
            "create_new", target_category="events",
            suggested_title="New Event",
        )

        # Mock root page
        mock_root = MagicMock()
        mock_root.category = "events"
        self.memopedia.get_page.return_value = mock_root

        # Mock new page creation
        mock_new_page = MagicMock()
        mock_new_page.id = "new_page_id"
        self.memopedia.create_page.return_value = mock_new_page

        self.client.generate.return_value = json.dumps({
            "title": "New Event",
            "summary": "A new event happened",
            "content": "Details about the event.",
            "keywords": ["event", "news"],
            "excluded_note_ids": [],
        })

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "create_new")
        self.assertEqual(result.page_id, "new_page_id")
        self.assertEqual(result.resolved_count, 3)
        self.memopedia.create_page.assert_called_once()

    def test_create_child(self):
        self._setup_planned_notes(
            "create_child", target_page_id="parent_page",
            suggested_title="Child Topic",
        )

        mock_parent = MagicMock()
        mock_parent.title = "Parent"
        mock_parent.summary = "Parent page"
        self.memopedia.get_page.return_value = mock_parent

        mock_child = MagicMock()
        mock_child.id = "child_page_id"
        self.memopedia.create_page.return_value = mock_child

        self.client.generate.return_value = json.dumps({
            "title": "Child Topic",
            "summary": "Details about child",
            "content": "Child page content.",
            "keywords": ["child"],
            "excluded_note_ids": [],
        })

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "create_child")
        self.memopedia.create_page.assert_called_once()
        call_kwargs = self.memopedia.create_page.call_args[1]
        self.assertEqual(call_kwargs["parent_id"], "parent_page")

    def test_missing_target_page_reverts(self):
        self._setup_planned_notes(
            "append_to_existing", target_page_id="nonexistent",
        )
        self.memopedia.get_page.return_value = None

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )

        self.assertIsNone(result)
        # Notes should be reverted to unplanned
        self.assertEqual(count_unplanned_notes(self.conn), 3)

    def test_empty_group(self):
        result = execute_group(
            self.client, self.conn, "nonexistent_group", self.memopedia,
        )
        self.assertIsNone(result)

    def test_llm_failure(self):
        self._setup_planned_notes(
            "append_to_existing", target_page_id="page_123",
        )

        mock_page = MagicMock()
        mock_page.title = "Test"
        mock_page.content = "Content"
        mock_page.summary = "Summary"
        self.memopedia.get_page.return_value = mock_page

        self.client.generate.side_effect = RuntimeError("API error")

        result = execute_group(
            self.client, self.conn, "test_group", self.memopedia,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
