"""Tests for AutonomyManager."""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from saiverse.autonomy_manager import (
    AutonomyManager,
    AutonomyState,
    CycleReport,
    DEFAULT_INTERVAL_MINUTES,
)


def _make_manager_mock():
    """Create a mock SAIVerseManager with necessary attributes."""
    manager = MagicMock()
    manager.pulse_controller = MagicMock()
    manager.pulse_controller.register_on_interrupt = MagicMock()
    manager.pulse_controller.register_on_user_complete = MagicMock()

    persona = MagicMock()
    persona.persona_id = "test_persona"
    persona.current_building_id = "hall_1"

    sai_mem = MagicMock()
    sai_mem.is_ready.return_value = True
    sai_mem.get_current_thread.return_value = "test_persona:__persona__"

    stelis = MagicMock()
    stelis.thread_id = "test_persona:stelis_abc12345"
    sai_mem.start_stelis_thread.return_value = stelis

    persona.sai_memory = sai_mem
    manager.all_personas = {"test_persona": persona}

    return manager, persona, sai_mem


class TestAutonomyManagerLifecycle(unittest.TestCase):

    def test_initial_state_is_stopped(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        self.assertEqual(am.state, AutonomyState.STOPPED)
        self.assertFalse(am.is_running)

    def test_start_creates_stelis_thread(self):
        manager, persona, sai_mem = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            sai_mem.start_stelis_thread.assert_called_once()
            sai_mem.set_active_thread.assert_called_with("test_persona:stelis_abc12345")
            self.assertTrue(am.is_running)
        finally:
            am.stop()

    def test_start_returns_false_if_already_running(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        try:
            self.assertFalse(am.start())
        finally:
            am.stop()

    def test_stop_ends_stelis_thread(self):
        manager, persona, sai_mem = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=0.01)
        am.start()
        time.sleep(0.1)
        am.stop()
        sai_mem.end_stelis_thread.assert_called_once()
        sai_mem.set_active_thread.assert_called_with("test_persona:__persona__")
        self.assertEqual(am.state, AutonomyState.STOPPED)

    def test_stop_returns_false_if_not_running(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        self.assertFalse(am.stop())


class TestAutonomyManagerInterrupt(unittest.TestCase):

    def test_pause_switches_to_original_thread(self):
        manager, persona, sai_mem = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=60)
        am.start()
        try:
            time.sleep(0.1)
            result = am.pause_for_user()
            self.assertEqual(result, "test_persona:__persona__")
            self.assertEqual(am.state, AutonomyState.INTERRUPTED)
        finally:
            am.stop()

    def test_resume_switches_back_to_stelis(self):
        manager, persona, sai_mem = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=60)
        am.start()
        try:
            time.sleep(0.1)
            am.pause_for_user()
            sai_mem.reset_mock()

            result = am.resume_from_user()
            self.assertTrue(result)
            self.assertEqual(am.state, AutonomyState.RUNNING)
            sai_mem.set_active_thread.assert_called_with("test_persona:stelis_abc12345")
        finally:
            am.stop()

    def test_resume_returns_false_if_not_interrupted(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=60)
        am.start()
        try:
            self.assertFalse(am.resume_from_user())
        finally:
            am.stop()


class TestAutonomyManagerConfig(unittest.TestCase):

    def test_set_interval(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_interval(10)
        self.assertEqual(am.interval_minutes, 10)

    def test_set_interval_minimum(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_interval(0.1)
        self.assertEqual(am.interval_minutes, 0.5)

    def test_set_models(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager)
        am.set_models(decision_model="claude-opus", execution_model="gemini-flash")
        self.assertEqual(am.decision_model, "claude-opus")
        self.assertEqual(am.execution_model, "gemini-flash")

    def test_get_status(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=10)
        status = am.get_status()
        self.assertEqual(status["persona_id"], "test_persona")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["interval_minutes"], 10)


class TestPulseControllerCallbacks(unittest.TestCase):

    def test_registers_callbacks_on_start(self):
        manager, _, _ = _make_manager_mock()
        am = AutonomyManager("test_persona", manager, interval_minutes=60)
        am.start()
        try:
            manager.pulse_controller.register_on_interrupt.assert_called_once()
            manager.pulse_controller.register_on_user_complete.assert_called_once()
        finally:
            am.stop()


if __name__ == "__main__":
    unittest.main()
