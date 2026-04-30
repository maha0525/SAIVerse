"""Tests for AutonomyManager (Phase C-2 simplified version).

旧 Decision/Execution/Stelis/PulseController-callback 連携は MetaLayer 経由に
統合されたため、このクラス自体の責務は「ペルソナごとの定期 tick タイマー」だけ。
テストもライフサイクル + 設定 + 状態取得 + tick 発火に絞る。
"""

import time
import unittest
from unittest.mock import MagicMock

from saiverse.autonomy_manager import (
    AutonomyManager,
    AutonomyState,
)


def _make_manager_mock():
    """Create a mock SAIVerseManager with a meta_layer."""
    manager = MagicMock()
    manager.meta_layer = MagicMock()
    manager.meta_layer.on_periodic_tick = MagicMock()

    persona = MagicMock()
    persona.persona_id = "test_persona"
    persona.current_building_id = "hall_1"
    manager.all_personas = {"test_persona": persona}

    return manager, persona


class TestAutonomyManagerLifecycle(unittest.TestCase):

    def test_initial_state_is_stopped(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        self.assertEqual(am.state, AutonomyState.STOPPED)
        self.assertFalse(am.is_running)

    def test_start_launches_loop(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        try:
            self.assertTrue(am.start())
            self.assertTrue(am.is_running)
            # No external dependencies (Stelis etc.) are touched
        finally:
            am.stop()

    def test_start_returns_false_if_already_running(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            self.assertFalse(am.start())
        finally:
            am.stop()

    def test_stop_terminates_loop(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        time.sleep(0.05)
        self.assertTrue(am.stop())
        self.assertEqual(am.state, AutonomyState.STOPPED)

    def test_stop_returns_false_if_not_running(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        self.assertFalse(am.stop())


class TestAutonomyManagerConfig(unittest.TestCase):

    def test_set_interval(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_interval(10)
        self.assertEqual(am.interval_minutes, 10)

    def test_set_interval_minimum(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_interval(0.1)
        self.assertEqual(am.interval_minutes, 0.5)

    def test_set_models_compat(self):
        """API 互換: 旧 set_models は値を保持するだけ (Phase C-2 で no-op)."""
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_models(decision_model="claude-opus", execution_model="gemini-flash")
        self.assertEqual(am.decision_model, "claude-opus")
        self.assertEqual(am.execution_model, "gemini-flash")

    def test_get_status(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=10)
        status = am.get_status()
        self.assertEqual(status["persona_id"], "test_persona")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["interval_minutes"], 10)
        self.assertIn("decision_model", status)
        self.assertIn("execution_model", status)
        self.assertIn("current_cycle_id", status)
        self.assertIn("last_report", status)


class TestPeriodicTick(unittest.TestCase):

    def test_loop_invokes_meta_layer_on_periodic_tick(self):
        """Tick loop calls MetaLayer.on_periodic_tick(persona_id, context=...) ."""
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            # Allow at least one tick
            time.sleep(0.1)
            self.assertGreaterEqual(
                manager.meta_layer.on_periodic_tick.call_count, 1
            )
            args, kwargs = manager.meta_layer.on_periodic_tick.call_args
            self.assertEqual(args[0], "test_persona")
            ctx = kwargs.get("context") or (args[1] if len(args) > 1 else None)
            self.assertIsInstance(ctx, dict)
            self.assertIn("cycle_id", ctx)
            self.assertIn("interval_seconds", ctx)
        finally:
            am.stop()

    def test_loop_records_completed_status_on_success(self):
        manager, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.1)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "completed")
        finally:
            am.stop()

    def test_loop_records_error_when_meta_layer_missing(self):
        manager, _ = _make_manager_mock()
        manager.meta_layer = None
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.1)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "error")
        finally:
            am.stop()

    def test_loop_records_error_when_tick_raises(self):
        manager, _ = _make_manager_mock()
        manager.meta_layer.on_periodic_tick.side_effect = RuntimeError("boom")
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            time.sleep(0.1)
            self.assertIsNotNone(am.last_report)
            self.assertEqual(am.last_report.status, "error")
            self.assertIn("boom", am.last_report.error or "")
        finally:
            am.stop()


if __name__ == "__main__":
    unittest.main()
