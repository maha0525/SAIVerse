"""Tests for memory_notes storage functions."""

import sqlite3
import unittest

from sai_memory.memory.storage import (
    MemoryNote,
    add_memory_notes,
    count_unresolved_notes,
    count_unplanned_notes,
    count_planned_groups,
    delete_resolved_notes_before,
    get_planned_group_labels,
    get_planned_notes_by_group,
    get_unplanned_notes,
    get_unresolved_notes,
    init_db,
    resolve_memory_notes,
    set_note_plan,
)


class TestMemoryNotes(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_add_and_retrieve_notes(self):
        notes = add_memory_notes(
            self.conn,
            thread_id="t1",
            notes=["fact A", "fact B", "fact C"],
            source_pulse_id="pulse_001",
            source_time=1000,
        )
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0].content, "fact A")
        self.assertFalse(notes[0].resolved)
        self.assertEqual(notes[0].source_pulse_id, "pulse_001")

        unresolved = get_unresolved_notes(self.conn)
        self.assertEqual(len(unresolved), 3)

    def test_empty_and_whitespace_notes_are_skipped(self):
        notes = add_memory_notes(
            self.conn,
            thread_id="t1",
            notes=["valid", "", "  ", "also valid"],
        )
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0].content, "valid")
        self.assertEqual(notes[1].content, "also valid")

    def test_resolve_notes(self):
        notes = add_memory_notes(
            self.conn,
            thread_id="t1",
            notes=["a", "b", "c"],
        )
        resolve_memory_notes(self.conn, [notes[0].id, notes[1].id])

        unresolved = get_unresolved_notes(self.conn)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].content, "c")

    def test_count_unresolved(self):
        add_memory_notes(self.conn, thread_id="t1", notes=["a", "b", "c"])
        self.assertEqual(count_unresolved_notes(self.conn), 3)

        notes = get_unresolved_notes(self.conn)
        resolve_memory_notes(self.conn, [notes[0].id])
        self.assertEqual(count_unresolved_notes(self.conn), 2)

    def test_filter_by_thread(self):
        add_memory_notes(self.conn, thread_id="t1", notes=["t1 note"])
        add_memory_notes(self.conn, thread_id="t2", notes=["t2 note"])

        t1_notes = get_unresolved_notes(self.conn, thread_id="t1")
        self.assertEqual(len(t1_notes), 1)
        self.assertEqual(t1_notes[0].content, "t1 note")

        all_notes = get_unresolved_notes(self.conn)
        self.assertEqual(len(all_notes), 2)

    def test_delete_resolved_before(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["old", "new"])
        resolve_memory_notes(self.conn, [n.id for n in notes])

        # Delete resolved notes older than far-future timestamp
        deleted = delete_resolved_notes_before(self.conn, 9999999999)
        self.assertEqual(deleted, 2)

        # Nothing left
        self.assertEqual(count_unresolved_notes(self.conn), 0)

    def test_resolve_empty_list(self):
        result = resolve_memory_notes(self.conn, [])
        self.assertEqual(result, 0)

    def test_limit(self):
        add_memory_notes(self.conn, thread_id="t1", notes=[f"n{i}" for i in range(20)])
        limited = get_unresolved_notes(self.conn, limit=5)
        self.assertEqual(len(limited), 5)


class TestNotePlanMetadata(unittest.TestCase):

    def setUp(self):
        self.conn = init_db(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_new_notes_are_unplanned(self):
        add_memory_notes(self.conn, thread_id="t1", notes=["a", "b"])
        self.assertEqual(count_unplanned_notes(self.conn), 2)
        unplanned = get_unplanned_notes(self.conn)
        self.assertEqual(len(unplanned), 2)
        self.assertIsNone(unplanned[0].group_label)
        self.assertIsNone(unplanned[0].action)

    def test_set_note_plan(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["a", "b", "c"])

        # Plan first two notes
        updated = set_note_plan(
            self.conn,
            [notes[0].id, notes[1].id],
            group_label="topic A",
            action="append_to_existing",
            target_page_id="page_123",
        )
        self.assertEqual(updated, 2)

        # First two should be planned, third still unplanned
        self.assertEqual(count_unplanned_notes(self.conn), 1)
        unplanned = get_unplanned_notes(self.conn)
        self.assertEqual(len(unplanned), 1)
        self.assertEqual(unplanned[0].content, "c")

    def test_get_planned_group_labels(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["a", "b", "c", "d"])
        set_note_plan(self.conn, [notes[0].id, notes[1].id],
                      group_label="group1", action="create_new",
                      suggested_title="Title1", target_category="events")
        set_note_plan(self.conn, [notes[2].id],
                      group_label="group2", action="append_to_existing",
                      target_page_id="page_456")

        labels = get_planned_group_labels(self.conn)
        self.assertEqual(len(labels), 2)
        self.assertIn("group1", labels)
        self.assertIn("group2", labels)
        self.assertEqual(count_planned_groups(self.conn), 2)

    def test_get_planned_notes_by_group(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["a", "b", "c"])
        set_note_plan(self.conn, [notes[0].id, notes[1].id],
                      group_label="grp", action="create_new",
                      suggested_title="New Page", target_category="terms")

        group_notes = get_planned_notes_by_group(self.conn, "grp")
        self.assertEqual(len(group_notes), 2)
        self.assertEqual(group_notes[0].group_label, "grp")
        self.assertEqual(group_notes[0].action, "create_new")
        self.assertEqual(group_notes[0].suggested_title, "New Page")
        self.assertEqual(group_notes[0].target_category, "terms")

    def test_metadata_preserved_in_unresolved(self):
        """Metadata should be visible in get_unresolved_notes too."""
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["fact"])
        set_note_plan(self.conn, [notes[0].id],
                      group_label="test", action="create_child",
                      target_page_id="parent_page")

        unresolved = get_unresolved_notes(self.conn)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].group_label, "test")
        self.assertEqual(unresolved[0].action, "create_child")
        self.assertEqual(unresolved[0].target_page_id, "parent_page")

    def test_resolve_clears_planned(self):
        notes = add_memory_notes(self.conn, thread_id="t1", notes=["a", "b"])
        set_note_plan(self.conn, [notes[0].id, notes[1].id],
                      group_label="grp", action="create_new")

        resolve_memory_notes(self.conn, [notes[0].id, notes[1].id])
        self.assertEqual(count_planned_groups(self.conn), 0)
        self.assertEqual(get_planned_group_labels(self.conn), [])

    def test_set_plan_empty_list(self):
        result = set_note_plan(self.conn, [], group_label="x", action="y")
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
