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


class TestCacheStorageRefund(unittest.TestCase):
    """TTL 前に消したキャッシュの残りを、同じ区分の負の記録で返金する。"""

    def setUp(self):
        self.tracker = get_usage_tracker()
        self._orig_flush = self.tracker._flush_to_db
        self.tracker._flush_to_db = MagicMock()
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def tearDown(self):
        self.tracker._flush_to_db = self._orig_flush
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def test_refund_is_a_negative_cache_storage_record(self):
        with patch(
            "saiverse.model_configs.calculate_cache_storage_cost", return_value=0.25,
        ) as calc:
            self.tracker.record_cache_storage_refund(
                "gem", 4000, 120.5, persona_id="air", building_id="room",
            )
        record = self.tracker._pending_records[-1]
        calc.assert_called_once_with("gem", 4000, 120.5, at=record["timestamp"])
        self.assertEqual(record["cost_usd"], -0.25)
        self.assertEqual(record["category"], "cache_storage")
        self.assertEqual(record["model_id"], "gem")
        self.assertEqual(record["cached_tokens"], 4000)
        self.assertEqual((record["input_tokens"], record["output_tokens"]), (0, 0))
        self.assertEqual((record["persona_id"], record["building_id"]), ("air", "room"))
        # 作成時の計上 (record_cache_storage) と同じ通貨へ返金する
        self.assertEqual(record["currency"], "USD")

    def test_refund_nets_against_the_charge(self):
        with patch("saiverse.model_configs.calculate_cache_storage_cost", side_effect=[0.3, 0.2]):
            self.tracker.record_cache_storage("gem", 4000, 300, persona_id="air")
            self.tracker.record_cache_storage_refund("gem", 4000, 200, persona_id="air")
        costs = [r["cost_usd"] for r in self.tracker._pending_records]
        self.assertAlmostEqual(sum(costs), 0.1)

    def test_zero_refund_writes_nothing(self):
        with patch("saiverse.model_configs.calculate_cache_storage_cost", return_value=0.0):
            self.tracker.record_cache_storage_refund("gem", 4000, 0, persona_id="air")
        self.assertEqual(self.tracker._pending_records, [])


class TestConfigure(unittest.TestCase):
    def test_configure_sets_session_factory(self):
        tracker = get_usage_tracker()
        mock_factory = MagicMock()
        tracker.configure(mock_factory)
        self.assertIs(tracker._session_factory, mock_factory)
        # Clean up
        tracker._session_factory = None


class TestUnconfiguredNoWrite(unittest.TestCase):
    """未 configure のプロセス (pytest 等) からは一切 DB に書かないことの回帰テスト。

    かつて _flush_to_db は未 configure 時に database.session.SessionLocal
    (= 本番 DB) へ黙ってフォールバックし、テストの架空モデル使用量が本番の
    llm_usage_log に混入した (2026-08-16 発覚)。
    """

    def setUp(self):
        self.tracker = get_usage_tracker()
        self._orig_factory = self.tracker._session_factory
        self._orig_warned = self.tracker._warned_unconfigured
        self.tracker._session_factory = None
        self.tracker._warned_unconfigured = False
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def tearDown(self):
        self.tracker._session_factory = self._orig_factory
        self.tracker._warned_unconfigured = self._orig_warned
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def test_record_usage_without_configure_drops_records(self):
        with self.assertLogs("saiverse.usage_tracker", level="WARNING") as cm:
            self.tracker.record_usage("fake-model", 100, 1, persona_id="tester")
        self.assertEqual(len(self.tracker._pending_records), 0)
        self.assertTrue(any("not configured" in msg for msg in cm.output))

    def test_unconfigured_warning_fires_only_once(self):
        with self.assertLogs("saiverse.usage_tracker", level="DEBUG") as cm:
            self.tracker.record_usage("fake-model", 100, 1)
            self.tracker.record_usage("fake-model", 100, 1)
        warnings = [m for m in cm.output if m.startswith("WARNING")]
        self.assertEqual(len(warnings), 1)


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


class TestDirectCostAndClientUsage(unittest.TestCase):
    def setUp(self):
        self.tracker = get_usage_tracker()
        self._orig_flush = self.tracker._flush_to_db
        self.tracker._flush_to_db = MagicMock()

    def tearDown(self):
        self.tracker._flush_to_db = self._orig_flush
        with self.tracker._pending_lock:
            self.tracker._pending_records.clear()

    def test_record_direct_cost_adds_record(self):
        self.tracker.record_direct_cost(
            "grok-imagine-image-2.0",
            0.08,
            input_tokens=0,
            output_tokens=0,
            persona_id="p1",
            node_type="tool",
            category="image_generation",
        )
        self.assertEqual(len(self.tracker._pending_records), 1)
        rec = self.tracker._pending_records[0]
        self.assertEqual(rec["model_id"], "grok-imagine-image-2.0")
        self.assertAlmostEqual(rec["cost_usd"], 0.08)
        self.assertEqual(rec["category"], "image_generation")
        self.assertEqual(rec["persona_id"], "p1")

    def test_record_client_usage_consumes_and_handles_errors(self):
        from llm_clients.base import UsageInfo

        client = MagicMock()
        client.consume_usage.return_value = UsageInfo(
            model="gemini-3.8-flash",
            input_tokens=120,
            output_tokens=30,
            timestamp=1700000000.0,
            cached_tokens=20,
        )
        with patch("saiverse.usage_tracker.calculate_cost", return_value=0.001):
            self.tracker.record_client_usage(
                client,
                persona_id="p1",
                node_type="media_summary",
                category="media_summary",
            )
        self.assertEqual(len(self.tracker._pending_records), 1)
        rec = self.tracker._pending_records[0]
        self.assertEqual(rec["model_id"], "gemini-3.8-flash")
        self.assertEqual(rec["input_tokens"], 120)
        self.assertEqual(rec["output_tokens"], 30)
        self.assertEqual(rec["cached_tokens"], 20)
        self.assertEqual(rec["category"], "media_summary")

        # Empty or raising consume_usage must not raise
        client.consume_usage.return_value = None
        self.tracker.record_client_usage(client, category="media_summary")
        client.consume_usage.side_effect = RuntimeError("boom")
        self.tracker.record_client_usage(client, category="media_summary")
        self.assertEqual(len(self.tracker._pending_records), 1)

    def test_standalone_callers_record_usage_even_on_exception(self):
        from pathlib import Path
        from sai_memory.memory.entity_extractor import ExtractionFailed, extract_entities_and_involvement
        from saiverse import media_summary

        # 1. media_summary (_generate_document_summary)
        fake_client = MagicMock()
        fake_client.generate.side_effect = RuntimeError("api error")
        fake_path = MagicMock(spec=Path)
        fake_path.read_text.return_value = "doc"
        with (
            patch.object(media_summary, "_get_summary_client", return_value=fake_client),
            patch.object(self.tracker, "record_client_usage") as mock_rec,
        ):
            self.assertIsNone(media_summary._generate_document_summary(fake_path))
            mock_rec.assert_called_once_with(
                fake_client, node_type="media_summary", category="media_summary"
            )

        # 2. entity_extractor (extract_entities_and_involvement)
        msg = MagicMock()
        msg.role = "user"
        msg.speaker_name = "User"
        msg.content = "hello"
        msg.timestamp = None
        ee_client = MagicMock()
        ee_client.generate.side_effect = RuntimeError("timeout")
        with (
            patch.object(self.tracker, "record_client_usage") as mock_rec,
            self.assertRaises(ExtractionFailed),
        ):
            extract_entities_and_involvement(ee_client, [msg], persona_id="p_ee")
        mock_rec.assert_called_once_with(
            ee_client,
            persona_id="p_ee",
            node_type="entity_extractor",
            category="memory_weave_generate",
        )


if __name__ == "__main__":
    unittest.main()
