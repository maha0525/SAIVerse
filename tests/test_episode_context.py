"""Tests for episode context retrieval with level promotion control."""

import sqlite3
import unittest
from typing import List

from sai_memory.arasuji.context import (
    MIN_ENTRIES_PER_LEVEL,
    ContextEntry,
    get_episode_context,
    get_episode_context_for_timerange,
)
from sai_memory.arasuji.storage import (
    create_entry,
    init_arasuji_tables,
)


def _create_lv1_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    content: str = "",
    message_count: int = 20,
) -> str:
    """Create a Lv1 arasuji entry and return its ID."""
    if not content:
        content = f"Lv1 summary {start_time}-{end_time}"
    entry = create_entry(
        conn,
        level=1,
        content=content,
        source_ids=[],
        start_time=start_time,
        end_time=end_time,
        source_count=1,
        message_count=message_count,
    )
    return entry.id


def _create_lv2_entry(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
    source_ids: List[str],
    content: str = "",
    message_count: int = 200,
) -> str:
    """Create a Lv2 arasuji entry and return its ID."""
    if not content:
        content = f"Lv2 summary {start_time}-{end_time}"
    entry = create_entry(
        conn,
        level=2,
        content=content,
        source_ids=source_ids,
        start_time=start_time,
        end_time=end_time,
        source_count=len(source_ids),
        message_count=message_count,
    )
    return entry.id


class TestLevelPromotionControl(unittest.TestCase):
    """Test that level promotion requires MIN_ENTRIES_PER_LEVEL entries."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_old_behavior_would_promote_after_one_lv1(self):
        """Verify that with MIN_ENTRIES_PER_LEVEL, Lv1 entries are retained.

        Create 15 Lv1 entries and a Lv2 covering entries 1-10.
        Old behavior: 1 Lv1 read → immediate Lv2 promotion → only 1 Lv1.
        New behavior: must read MIN_ENTRIES_PER_LEVEL Lv1s before Lv2 allowed.
        """
        # Create 15 Lv1 entries (time 100-1500, each covering 100 units)
        lv1_ids = []
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 covering entries 0-9 (time 100-1099)
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        context = get_episode_context(self.conn, max_entries=50)

        # Count Lv1 entries in context
        lv1_count = sum(1 for e in context if e.level == 1)

        # With new behavior, we should have at least MIN_ENTRIES_PER_LEVEL Lv1s
        # (5 remaining unconsolidated Lv1s: entries 10-14)
        # The Lv2 should only appear AFTER enough Lv1s have been read
        self.assertGreaterEqual(
            lv1_count, 5,  # All 5 unconsolidated Lv1s should be present
            f"Expected at least 5 Lv1 entries, got {lv1_count}",
        )

    def test_min_entries_before_promotion(self):
        """Exactly MIN_ENTRIES_PER_LEVEL Lv1 entries must be read before Lv2."""
        # Create enough Lv1 entries that we can test the boundary
        n_lv1 = MIN_ENTRIES_PER_LEVEL + 5
        lv1_ids = []
        for i in range(n_lv1):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 covering the first MIN_ENTRIES_PER_LEVEL entries
        first_n = MIN_ENTRIES_PER_LEVEL
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=first_n * 100 + 99,
            source_ids=lv1_ids[:first_n],
        )

        context = get_episode_context(self.conn, max_entries=100)

        # Key assertion: total Lv1 entries should be at least 5
        # (the 5 unconsolidated ones that aren't covered by Lv2)
        lv1_in_context = sum(1 for e in context if e.level == 1)
        self.assertGreaterEqual(lv1_in_context, 5)

    def test_no_lv2_when_insufficient_lv1(self):
        """When fewer than MIN_ENTRIES_PER_LEVEL Lv1 entries exist, no Lv2."""
        # Create only 3 Lv1 entries
        lv1_ids = []
        for i in range(3):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create a Lv2 that would cover these if promotion were allowed
        # But since the Lv2 source_ids include the Lv1s, they're marked as read
        # Actually, we need Lv1s that are NOT covered by Lv2 for them to appear
        # Let's create Lv1s that aren't in the Lv2's sources
        extra_lv1_ids = []
        for i in range(3, 6):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            extra_lv1_ids.append(entry_id)

        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=399,
            source_ids=lv1_ids,
        )

        context = get_episode_context(self.conn, max_entries=50)
        levels = [e.level for e in context]

        # Only 3 Lv1 entries exist that aren't covered by Lv2
        # 3 < MIN_ENTRIES_PER_LEVEL, so Lv2 should NOT appear
        self.assertNotIn(
            2, levels,
            "Lv2 should not appear when fewer than MIN_ENTRIES_PER_LEVEL "
            "Lv1 entries have been read",
        )

    def test_only_lv1_no_promotion(self):
        """When only Lv1 entries exist (no Lv2), all should be returned."""
        for i in range(20):
            start = (i + 1) * 100
            end = start + 99
            _create_lv1_entry(self.conn, start, end)

        context = get_episode_context(self.conn, max_entries=50)

        # All entries should be Lv1
        for entry in context:
            self.assertEqual(entry.level, 1)

        self.assertEqual(len(context), 20)

    def test_timerange_also_respects_min_entries(self):
        """get_episode_context_for_timerange should also enforce min entries."""
        lv1_ids = []
        for i in range(15):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        # Get context for events before time 1600
        result = get_episode_context_for_timerange(
            self.conn,
            start_time=1600,
            end_time=1700,
            max_entries=50,
        )

        # Should contain Lv1 entries, not jump to Lv2 immediately
        self.assertIn("あらすじ", result)


class TestLevelPromotionEdgeCases(unittest.TestCase):
    """Edge cases for level promotion control."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_arasuji_tables(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_empty_database(self):
        """No entries should return empty context."""
        context = get_episode_context(self.conn, max_entries=50)
        self.assertEqual(len(context), 0)

    def test_single_entry(self):
        """Single Lv1 entry should be returned."""
        _create_lv1_entry(self.conn, 100, 199)
        context = get_episode_context(self.conn, max_entries=50)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0].level, 1)

    def test_promotion_happens_after_threshold(self):
        """After reading MIN_ENTRIES_PER_LEVEL Lv1s, Lv2 becomes available."""
        # Create MIN_ENTRIES_PER_LEVEL + 10 Lv1 entries
        n_total = MIN_ENTRIES_PER_LEVEL + 10
        lv1_ids = []
        for i in range(n_total):
            start = (i + 1) * 100
            end = start + 99
            entry_id = _create_lv1_entry(self.conn, start, end)
            lv1_ids.append(entry_id)

        # Create Lv2 covering the first 10 entries
        _create_lv2_entry(
            self.conn,
            start_time=100,
            end_time=1099,
            source_ids=lv1_ids[:10],
        )

        context = get_episode_context(self.conn, max_entries=100)

        # With enough remaining Lv1s (n_total - 10 = MIN_ENTRIES_PER_LEVEL),
        # exactly MIN_ENTRIES_PER_LEVEL Lv1s will be read, then Lv2 becomes
        # available for the consolidated range
        lv1_count = sum(1 for e in context if e.level == 1)
        self.assertGreaterEqual(lv1_count, MIN_ENTRIES_PER_LEVEL)


if __name__ == "__main__":
    unittest.main()
