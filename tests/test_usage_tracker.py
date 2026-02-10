"""Tests for usage_tracker.py — singleton, record_usage, flush."""
import unittest
from unittest.mock import MagicMock, patch

from saiverse.usage_tracker import UsageTracker, get_usage_tracker


class TestUsageTrackerSingleton(unittest.TestCase):
    def test_singleton_returns_same_instance(self):
        a = UsageTracker()
        b = UsageTracker()
        self.assertIs(a, b)

    def test_get_usage_tracker_returns_singleton(self):
        tracker = get_usage_tracker()
        self.assertIsInstance(tracker, UsageTracker)
        self.assertIs(tracker, UsageTracker())


class TestRecordUsage(unittest.TestCase):
    def setUp(self):
        self.tracker = get_usage_tracker()
        # Prevent actual DB flush during tests
        self._orig_flush = self.tracker._flush_to_db
        self.tracker._flush_to_db = MagicMock()

    def tearDown(self):
        self.tracker._flush_to_db = self._orig_flush
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def test_record_usage_adds_pending_record(self):
        initial_count = len(self.tracker._pending_records)
        self.tracker.record_usage(
            "test-model", 100, 50, persona_id="tester"
        )
        self.assertEqual(len(self.tracker._pending_records), initial_count + 1)
        record = self.tracker._pending_records[-1]
        self.assertEqual(record["model_id"], "test-model")
        self.assertEqual(record["input_tokens"], 100)
        self.assertEqual(record["output_tokens"], 50)
        self.assertEqual(record["persona_id"], "tester")

    def test_record_usage_triggers_flush_at_batch_size(self):
        self.tracker.record_usage("test-model", 10, 5)
        # batch_size is 1, so _flush_to_db should be called
        self.tracker._flush_to_db.assert_called()


class TestConfigure(unittest.TestCase):
    def test_configure_sets_session_factory(self):
        tracker = get_usage_tracker()
        mock_factory = MagicMock()
        tracker.configure(mock_factory)
        self.assertIs(tracker._session_factory, mock_factory)
        # Clean up
        tracker._session_factory = None


class TestFlush(unittest.TestCase):
    def test_flush_clears_pending(self):
        tracker = get_usage_tracker()
        mock_factory = MagicMock()
        mock_session = MagicMock()
        mock_factory.return_value = mock_session
        tracker.configure(mock_factory)

        with tracker._pending_lock:
            tracker._pending_records.append({
                "timestamp": None,
                "persona_id": None,
                "building_id": None,
                "model_id": "x",
                "input_tokens": 1,
                "output_tokens": 1,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": None,
                "node_type": None,
                "playbook_name": None,
                "category": None,
            })

        with patch("saiverse.usage_tracker.calculate_cost", return_value=0.0):
            tracker.flush()

        self.assertEqual(len(tracker._pending_records), 0)
        # Clean up
        tracker._session_factory = None


if __name__ == "__main__":
    unittest.main()
