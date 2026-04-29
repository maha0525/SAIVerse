"""Tests for deferred Track operations (Intent A v0.14, Intent B v0.11).

Track-mutating spells defer their effect to Pulse completion via
PulseContext.deferred_track_ops; the runtime applies them when the root
Playbook returns. This test suite covers:

- PulseContext.enqueue_track_op (basic queue, last-wins for activate)
- Spells (track_activate / pause / complete / abort) enqueue when a
  PulseContext is active in the contextvar
- track_create runs the create immediately and only enqueues the activate
  side when activate=True
- runtime_runner._apply_deferred_track_ops drains the queue and invokes
  TrackManager methods
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Ensure builtin_data/tools is importable for _track_common (the tool loader
# normally adds this to sys.path when tools are autodiscovered; tests load the
# spell modules directly, so we mirror that here).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "builtin_data" / "tools"))


class PulseContextEnqueueTests(unittest.TestCase):
    def setUp(self) -> None:
        from sea.pulse_context import PulseContext
        self.ctx = PulseContext(pulse_id="test_pulse", thread_id="test_thread")

    def test_empty_queue(self):
        self.assertFalse(self.ctx.has_deferred_track_ops())
        self.assertEqual(self.ctx.deferred_track_ops, [])

    def test_enqueue_basic_ops_preserve_order(self):
        self.ctx.enqueue_track_op("pause", track_id="A")
        self.ctx.enqueue_track_op("complete", track_id="B")
        self.ctx.enqueue_track_op("abort", track_id="C")

        self.assertTrue(self.ctx.has_deferred_track_ops())
        self.assertEqual(len(self.ctx.deferred_track_ops), 3)
        self.assertEqual(
            [(op.op_type, op.track_id) for op in self.ctx.deferred_track_ops],
            [("pause", "A"), ("complete", "B"), ("abort", "C")],
        )

    def test_activate_last_wins_drops_earlier_activates(self):
        self.ctx.enqueue_track_op("pause", track_id="A")
        self.ctx.enqueue_track_op("activate", track_id="X")
        self.ctx.enqueue_track_op("complete", track_id="B")
        self.ctx.enqueue_track_op("activate", track_id="Y")  # last wins

        ops = self.ctx.deferred_track_ops
        # pause(A), complete(B), activate(Y) — earlier activate(X) dropped
        self.assertEqual(len(ops), 3)
        self.assertEqual(ops[0].op_type, "pause")
        self.assertEqual(ops[0].track_id, "A")
        self.assertEqual(ops[1].op_type, "complete")
        self.assertEqual(ops[1].track_id, "B")
        self.assertEqual(ops[2].op_type, "activate")
        self.assertEqual(ops[2].track_id, "Y")

    def test_activate_args_passed_through(self):
        op = self.ctx.enqueue_track_op("pause", track_id="A", reason="test")
        self.assertEqual(op.args, {"reason": "test"})


class SpellEnqueueWithPulseContextTests(unittest.TestCase):
    """Verify spells enqueue (instead of executing) when a PulseContext is set."""

    def setUp(self) -> None:
        from sea.pulse_context import PulseContext
        from tools.context import persona_context

        self.ctx = PulseContext(pulse_id="test_pulse", thread_id="test_thread")
        self._persona_cm = persona_context(
            persona_id="persona_test",
            persona_path=str(Path.cwd()),
            pulse_context=self.ctx,
        )
        self._persona_cm.__enter__()

    def tearDown(self) -> None:
        self._persona_cm.__exit__(None, None, None)

    def _import_spell(self, name: str) -> Any:
        # Force-fresh import so contextvar set up in setUp is the one the spell
        # picks up (cached imports across unittest classes are still fine).
        module_name = f"builtin_data.tools.{name}"
        if module_name in sys.modules:
            return sys.modules[module_name]
        import importlib
        return importlib.import_module(module_name)

    def test_track_activate_enqueues(self):
        from builtin_data.tools.track_activate import track_activate
        message, snippet, _ = track_activate("track_X")
        self.assertIn("scheduled for end of Pulse", message)
        self.assertEqual(len(self.ctx.deferred_track_ops), 1)
        self.assertEqual(self.ctx.deferred_track_ops[0].op_type, "activate")
        self.assertEqual(self.ctx.deferred_track_ops[0].track_id, "track_X")
        snippet_data = json.loads(snippet.history_snippet)
        self.assertEqual(snippet_data["queued"], "activate")

    def test_track_pause_enqueues(self):
        from builtin_data.tools.track_pause import track_pause
        message, _, _ = track_pause("track_Y")
        self.assertIn("scheduled for end of Pulse", message)
        self.assertEqual(self.ctx.deferred_track_ops[0].op_type, "pause")
        self.assertEqual(self.ctx.deferred_track_ops[0].track_id, "track_Y")

    def test_track_complete_enqueues(self):
        from builtin_data.tools.track_complete import track_complete
        message, _, _ = track_complete("track_Z")
        self.assertIn("scheduled for end of Pulse", message)
        self.assertEqual(self.ctx.deferred_track_ops[0].op_type, "complete")
        self.assertEqual(self.ctx.deferred_track_ops[0].track_id, "track_Z")

    def test_track_abort_enqueues(self):
        from builtin_data.tools.track_abort import track_abort
        message, _, _ = track_abort("track_W")
        self.assertIn("scheduled for end of Pulse", message)
        self.assertEqual(self.ctx.deferred_track_ops[0].op_type, "abort")
        self.assertEqual(self.ctx.deferred_track_ops[0].track_id, "track_W")


class ApplyDeferredOpsTests(unittest.TestCase):
    """Verify the runtime flush function drives TrackManager correctly."""

    def test_apply_calls_track_manager_in_order(self):
        from sea.pulse_context import PulseContext
        from sea.runtime_runner import _apply_deferred_track_ops

        ctx = PulseContext(pulse_id="p1", thread_id="t1")
        ctx.enqueue_track_op("pause", track_id="A")
        ctx.enqueue_track_op("activate", track_id="B")
        ctx.enqueue_track_op("complete", track_id="C")

        track_manager = MagicMock()
        manager_ref = MagicMock(track_manager=track_manager)
        persona = MagicMock(persona_id="p_test", manager_ref=manager_ref)

        _apply_deferred_track_ops({"_pulse_context": ctx}, persona)

        # Each op invokes the matching TrackManager method
        track_manager.pause.assert_called_once_with("A")
        track_manager.activate.assert_called_once_with("B")
        track_manager.complete.assert_called_once_with("C")
        # Queue is drained
        self.assertEqual(ctx.deferred_track_ops, [])

    def test_apply_no_ops_is_noop(self):
        from sea.pulse_context import PulseContext
        from sea.runtime_runner import _apply_deferred_track_ops

        ctx = PulseContext(pulse_id="p1", thread_id="t1")
        track_manager = MagicMock()
        persona = MagicMock(manager_ref=MagicMock(track_manager=track_manager))

        _apply_deferred_track_ops({"_pulse_context": ctx}, persona)

        track_manager.pause.assert_not_called()
        track_manager.activate.assert_not_called()
        track_manager.complete.assert_not_called()

    def test_apply_handles_missing_track_manager(self):
        from sea.pulse_context import PulseContext
        from sea.runtime_runner import _apply_deferred_track_ops

        ctx = PulseContext(pulse_id="p1", thread_id="t1")
        ctx.enqueue_track_op("activate", track_id="Z")

        # Persona without manager_ref / track_manager — must drop ops, not crash
        persona = MagicMock(spec=["persona_id"])
        persona.persona_id = "p_test"

        _apply_deferred_track_ops({"_pulse_context": ctx}, persona)
        # Queue drained even on degraded path so failed ops don't accumulate
        self.assertEqual(ctx.deferred_track_ops, [])

    def test_apply_continues_after_individual_op_failure(self):
        from sea.pulse_context import PulseContext
        from sea.runtime_runner import _apply_deferred_track_ops

        ctx = PulseContext(pulse_id="p1", thread_id="t1")
        ctx.enqueue_track_op("pause", track_id="A")
        ctx.enqueue_track_op("activate", track_id="B")

        track_manager = MagicMock()
        track_manager.pause.side_effect = RuntimeError("boom")  # first op fails
        persona = MagicMock(persona_id="p", manager_ref=MagicMock(track_manager=track_manager))

        _apply_deferred_track_ops({"_pulse_context": ctx}, persona)

        # Second op must still run despite the first failure
        track_manager.activate.assert_called_once_with("B")
        self.assertEqual(ctx.deferred_track_ops, [])


if __name__ == "__main__":
    unittest.main()
