"""Tests for memory note organization (plan phase)."""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock

from sai_memory.memory.note_organizer import (
    _format_memopedia_tree_for_plan,
    _format_notes_for_plan,
    _parse_plan_response,
    organize_notes,
    plan_notes,
)
from sai_memory.memory.storage import (
    MemoryNote,
    add_memory_notes,
    count_unresolved_notes,
    get_planned_group_labels,
    get_planned_notes_by_group,
    get_unplanned_notes,
    init_db,
)


def _sample_tree():
    """Sample Memopedia tree structure."""
    return {
        "people": [
            {
                "id": "root_people",
                "title": "人物",
                "summary": "",
                "content": "",
                "children": [
                    {
                        "id": "page_mahar",
                        "title": "まはー",
                        "summary": "ユーザー。誠実で好奇心旺盛。",
                        "content": "まはーは開発者で..." * 50,  # ~500 chars
                        "children": [],
                    }
                ],
            }
        ],
        "terms": [
            {
                "id": "root_terms",
                "title": "用語",
                "summary": "",
                "content": "",
                "children": [],
            }
        ],
        "plans": [],
        "events": [],
    }


class TestFormatFunctions(unittest.TestCase):

    def test_format_tree(self):
        tree = _sample_tree()
        text = _format_memopedia_tree_for_plan(tree)
        self.assertIn("page_mahar", text)
        self.assertIn("まはー", text)
        self.assertIn("字)", text)  # Should contain char count

    def test_format_tree_empty(self):
        text = _format_memopedia_tree_for_plan({})
        self.assertIn("まだページはありません", text)

    def test_format_notes(self):
        notes = [
            MemoryNote(id="n1", thread_id="t1", content="fact A",
                       source_pulse_id=None, source_time=None,
                       resolved=False, created_at=1000),
            MemoryNote(id="n2", thread_id="t1", content="fact B",
                       source_pulse_id=None, source_time=None,
                       resolved=False, created_at=1001),
        ]
        text = _format_notes_for_plan(notes)
        self.assertIn("[n1] fact A", text)
        self.assertIn("[n2] fact B", text)


class TestParsePlanResponse(unittest.TestCase):

    def test_valid_response(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "topic A",
                    "note_ids": ["n1", "n2"],
                    "action": "append_to_existing",
                    "target_page_id": "page_123",
                    "suggested_title": None,
                    "target_category": None,
                }
            ]
        })
        groups = _parse_plan_response(response, {"n1", "n2", "n3"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["group_label"], "topic A")
        self.assertEqual(groups[0]["note_ids"], ["n1", "n2"])

    def test_filters_invalid_note_ids(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["n1", "fake_id"],
                    "action": "create_new",
                    "target_category": "events",
                }
            ]
        })
        groups = _parse_plan_response(response, {"n1"})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["note_ids"], ["n1"])

    def test_skips_invalid_action(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["n1"],
                    "action": "invalid_action",
                }
            ]
        })
        groups = _parse_plan_response(response, {"n1"})
        self.assertEqual(len(groups), 0)

    def test_skips_append_without_target(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["n1"],
                    "action": "append_to_existing",
                    # missing target_page_id
                }
            ]
        })
        groups = _parse_plan_response(response, {"n1"})
        self.assertEqual(len(groups), 0)

    def test_skips_new_without_category(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["n1"],
                    "action": "create_new",
                    # missing target_category
                }
            ]
        })
        groups = _parse_plan_response(response, {"n1"})
        self.assertEqual(len(groups), 0)

    def test_markdown_code_block(self):
        inner = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["n1"],
                    "action": "create_new",
                    "target_category": "events",
                    "suggested_title": "Test",
                }
            ]
        })
        response = f"```json\n{inner}\n```"
        groups = _parse_plan_response(response, {"n1"})
        self.assertEqual(len(groups), 1)

    def test_empty_response(self):
        self.assertEqual(_parse_plan_response("", set()), [])

    def test_all_note_ids_invalid(self):
        response = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": ["fake1", "fake2"],
                    "action": "create_new",
                    "target_category": "events",
                }
            ]
        })
        groups = _parse_plan_response(response, {"real1"})
        self.assertEqual(len(groups), 0)


class TestPlanNotes(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")
        self.client = MagicMock()

    def tearDown(self):
        self.conn.close()

    def test_plan_writes_metadata(self):
        notes = add_memory_notes(self.conn, thread_id="t1",
                                 notes=["fact A", "fact B", "fact C"])

        # Mock LLM response
        self.client.generate.return_value = json.dumps({
            "groups": [
                {
                    "group_label": "topic1",
                    "note_ids": [notes[0].id, notes[1].id],
                    "action": "append_to_existing",
                    "target_page_id": "page_xyz",
                },
                {
                    "group_label": "topic2",
                    "note_ids": [notes[2].id],
                    "action": "create_new",
                    "target_category": "events",
                    "suggested_title": "New Topic",
                },
            ]
        })

        groups = plan_notes(self.client, self.conn, _sample_tree())

        self.assertEqual(len(groups), 2)

        # Verify metadata was written
        labels = get_planned_group_labels(self.conn)
        self.assertEqual(set(labels), {"topic1", "topic2"})

        g1_notes = get_planned_notes_by_group(self.conn, "topic1")
        self.assertEqual(len(g1_notes), 2)
        self.assertEqual(g1_notes[0].action, "append_to_existing")
        self.assertEqual(g1_notes[0].target_page_id, "page_xyz")

        g2_notes = get_planned_notes_by_group(self.conn, "topic2")
        self.assertEqual(len(g2_notes), 1)
        self.assertEqual(g2_notes[0].action, "create_new")
        self.assertEqual(g2_notes[0].target_category, "events")
        self.assertEqual(g2_notes[0].suggested_title, "New Topic")

    def test_no_unplanned_notes(self):
        groups = plan_notes(self.client, self.conn, _sample_tree())
        self.assertEqual(len(groups), 0)
        self.client.generate.assert_not_called()

    def test_unplanned_empty_after_plan(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["a", "b"])
        self.client.generate.return_value = json.dumps({
            "groups": [
                {
                    "group_label": "grp",
                    "note_ids": [n.id for n in notes],
                    "action": "create_new",
                    "target_category": "terms",
                    "suggested_title": "Title",
                }
            ]
        })

        plan_notes(self.client, self.conn, _sample_tree())

        # All notes should now be planned
        unplanned = get_unplanned_notes(self.conn)
        self.assertEqual(len(unplanned), 0)

    def test_llm_failure_returns_empty(self):
        add_memory_notes(self.conn, thread_id="t1", notes=["a"])
        self.client.generate.side_effect = RuntimeError("API error")

        groups = plan_notes(self.client, self.conn, _sample_tree())
        self.assertEqual(len(groups), 0)


class TestOrganizeNotes(unittest.TestCase):
    """Test the combined organize_notes function (plan + write)."""

    def setUp(self):
        self.conn = init_db(":memory:")
        self.client = MagicMock()
        self.memopedia = MagicMock()

    def tearDown(self):
        self.conn.close()

    def test_organize_append(self):
        notes = add_memory_notes(self.conn, thread_id="t1",
                                 notes=["fact A", "fact B"])

        self.client.generate.return_value = json.dumps({
            "groups": [{
                "group_label": "topic1",
                "note_ids": [n.id for n in notes],
                "action": "append_to_existing",
                "target_page_id": "page_xyz",
            }]
        })

        # Mock memopedia
        mock_page = MagicMock()
        mock_page.title = "Test Page"
        self.memopedia.get_page.return_value = mock_page
        self.memopedia.get_tree.return_value = _sample_tree()

        results = organize_notes(self.client, self.conn, self.memopedia)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "append_to_existing")
        self.assertEqual(results[0].note_count, 2)

        # Notes should be resolved
        self.assertEqual(count_unresolved_notes(self.conn), 0)

        # Memopedia append should have been called
        self.memopedia.append_to_content.assert_called_once()
        call_args = self.memopedia.append_to_content.call_args
        self.assertEqual(call_args[0][0], "page_xyz")
        # Content should contain the note text
        self.assertIn("fact A", call_args[0][1])
        self.assertIn("fact B", call_args[0][1])

    def test_organize_create_new(self):
        notes = add_memory_notes(self.conn, thread_id="t1",
                                 notes=["new event happened"])

        self.client.generate.return_value = json.dumps({
            "groups": [{
                "group_label": "new_event",
                "note_ids": [notes[0].id],
                "action": "create_new",
                "target_category": "events",
                "suggested_title": "Big Event",
            }]
        })

        mock_root = MagicMock()
        mock_root.category = "events"
        self.memopedia.get_page.return_value = mock_root
        self.memopedia.get_tree.return_value = _sample_tree()

        mock_new_page = MagicMock()
        mock_new_page.id = "new_page_id"
        self.memopedia.create_page.return_value = mock_new_page

        results = organize_notes(self.client, self.conn, self.memopedia)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "create_new")
        self.assertEqual(count_unresolved_notes(self.conn), 0)

        # create_page should contain raw note content (no LLM rewrite)
        call_kwargs = self.memopedia.create_page.call_args[1]
        self.assertIn("new event happened", call_kwargs["content"])
        self.assertEqual(call_kwargs["title"], "Big Event")

    def test_organize_no_notes(self):
        results = organize_notes(self.client, self.conn, self.memopedia)
        self.assertEqual(results, [])
        self.client.generate.assert_not_called()

    def test_append_block_has_date_header(self):
        from sai_memory.memory.note_organizer import _format_append_block
        notes = [
            MemoryNote(id="n1", thread_id="t1", content="fact",
                       source_pulse_id=None, source_time=1711100000,
                       resolved=False, created_at=1711100000),
        ]
        block = _format_append_block(notes)
        self.assertIn("追記", block)
        self.assertIn("- fact", block)


if __name__ == "__main__":
    unittest.main()
