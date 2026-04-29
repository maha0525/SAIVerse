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


class EntryLineRoleTests(unittest.TestCase):
    """Verify entry_line_role flows: Handler defaults → track_create → metadata → TrackManager."""

    def test_handler_default_entry_line_role(self):
        from saiverse.track_handlers.autonomous_track_handler import AutonomousTrackHandler
        from saiverse.track_handlers.social_track_handler import SocialTrackHandler
        from saiverse.track_handlers.user_conversation_handler import UserConversationTrackHandler

        self.assertEqual(AutonomousTrackHandler.default_entry_line_role, "sub_line")
        self.assertEqual(UserConversationTrackHandler.default_entry_line_role, "main_line")
        self.assertEqual(SocialTrackHandler.default_entry_line_role, "main_line")

    def test_resolve_default_entry_line_role(self):
        from _track_common import resolve_default_entry_line_role
        self.assertEqual(resolve_default_entry_line_role("autonomous"), "sub_line")
        self.assertEqual(resolve_default_entry_line_role("user_conversation"), "main_line")
        self.assertEqual(resolve_default_entry_line_role("social"), "main_line")
        # Unknown type → safe default (Intent A invariant 9: other-talk = heavyweight)
        self.assertEqual(resolve_default_entry_line_role("nonexistent_type"), "main_line")

    def test_inject_entry_line_role_into_metadata(self):
        from builtin_data.tools.track_create import _inject_entry_line_role_into_metadata
        # Empty metadata → just entry_line_role
        result = _inject_entry_line_role_into_metadata(None, "sub_line")
        self.assertEqual(json.loads(result), {"entry_line_role": "sub_line"})
        # Existing metadata is preserved + entry_line_role added
        result = _inject_entry_line_role_into_metadata(
            json.dumps({"foo": "bar"}), "main_line"
        )
        self.assertEqual(json.loads(result), {"foo": "bar", "entry_line_role": "main_line"})
        # Malformed metadata is salvaged (don't lose original blob)
        result = _inject_entry_line_role_into_metadata("not_json", "main_line")
        data = json.loads(result)
        self.assertEqual(data["entry_line_role"], "main_line")
        self.assertEqual(data["_invalid_existing_metadata"], "not_json")

    def test_trackmanager_get_entry_line_role(self):
        """TrackManager.get_entry_line_role reads metadata and falls back safely."""
        import os
        import tempfile
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from database.models import Base
        from saiverse.track_manager import TrackManager

        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test.db")
            engine = create_engine(f"sqlite:///{db_path}")
            Base.metadata.create_all(engine)
            SessionLocal = sessionmaker(bind=engine)
            tm = TrackManager(session_factory=SessionLocal)

            # Track with explicit entry_line_role in metadata
            track_id = tm.create(
                persona_id="p1",
                track_type="autonomous",
                title="t",
                metadata=json.dumps({"entry_line_role": "sub_line"}),
            )
            self.assertEqual(tm.get_entry_line_role(track_id), "sub_line")

            # Track without metadata → default 'main_line'
            track_id2 = tm.create(persona_id="p1", track_type="autonomous", title="t2")
            self.assertEqual(tm.get_entry_line_role(track_id2), "main_line")

            # Unknown track_id → safe default
            self.assertEqual(tm.get_entry_line_role("nonexistent-uuid"), "main_line")

            # Invalid role value in metadata → safe default
            track_id3 = tm.create(
                persona_id="p1",
                track_type="autonomous",
                title="t3",
                metadata=json.dumps({"entry_line_role": "garbage_value"}),
            )
            self.assertEqual(tm.get_entry_line_role(track_id3), "main_line")

            engine.dispose()


if __name__ == "__main__":
    unittest.main()
