"""Tests for Track short_id (t:N) feature.

Covers:
- TrackManager.create assigns sequential short_ids per persona
- TrackManager.resolve_track_ref resolves t:N, UUID, and rejects invalid refs
- backfill_track_short_ids assigns short_ids to existing NULL rows
"""
from __future__ import annotations

import gc
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.models import Base
from saiverse.track_manager import (
    TrackManager,
    TrackNotFoundError,
)


class TrackShortIdTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.tm = TrackManager(session_factory=self.SessionLocal)

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_create_assigns_sequential_short_ids(self):
        t1 = self.tm.create("persona_A", "autonomous", title="First")
        t2 = self.tm.create("persona_A", "autonomous", title="Second")
        t3 = self.tm.create("persona_A", "autonomous", title="Third")

        track1 = self.tm.get(t1)
        track2 = self.tm.get(t2)
        track3 = self.tm.get(t3)

        self.assertEqual(track1.short_id, 1)
        self.assertEqual(track2.short_id, 2)
        self.assertEqual(track3.short_id, 3)

    def test_short_ids_are_per_persona(self):
        t_a1 = self.tm.create("persona_A", "autonomous")
        t_b1 = self.tm.create("persona_B", "autonomous")
        t_a2 = self.tm.create("persona_A", "autonomous")

        self.assertEqual(self.tm.get(t_a1).short_id, 1)
        self.assertEqual(self.tm.get(t_b1).short_id, 1)
        self.assertEqual(self.tm.get(t_a2).short_id, 2)

    def test_resolve_short_ref(self):
        t_id = self.tm.create("persona_A", "autonomous")
        resolved = self.tm.resolve_track_ref("persona_A", "t:1")
        self.assertEqual(resolved, t_id)

    def test_resolve_short_ref_case_insensitive(self):
        t_id = self.tm.create("persona_A", "autonomous")
        resolved = self.tm.resolve_track_ref("persona_A", "T:1")
        self.assertEqual(resolved, t_id)

    def test_resolve_uuid_passthrough(self):
        t_id = self.tm.create("persona_A", "autonomous")
        resolved = self.tm.resolve_track_ref("persona_A", t_id)
        self.assertEqual(resolved, t_id)

    def test_resolve_invalid_ref_raises(self):
        with self.assertRaises(TrackNotFoundError):
            self.tm.resolve_track_ref("persona_A", "invalid_ref")

    def test_resolve_nonexistent_short_id_raises(self):
        self.tm.create("persona_A", "autonomous")
        with self.assertRaises(TrackNotFoundError):
            self.tm.resolve_track_ref("persona_A", "t:999")

    def test_resolve_empty_raises(self):
        with self.assertRaises(TrackNotFoundError):
            self.tm.resolve_track_ref("persona_A", "")

    def test_resolve_wrong_persona_raises(self):
        self.tm.create("persona_A", "autonomous")
        with self.assertRaises(TrackNotFoundError):
            self.tm.resolve_track_ref("persona_B", "t:1")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmpdir.name) / "test.db")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        gc.collect()
        try:
            self._tmpdir.cleanup()
        except PermissionError:
            pass

    def test_backfill_assigns_short_ids(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO action_track "
                "(track_id, persona_id, track_type, status, is_persistent, "
                "is_forgotten, output_target, created_at) "
                "VALUES "
                "('aaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'p1', 'autonomous', 'running', 0, 0, 'none', '2026-01-01 00:00:00'),"
                "('bbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'p1', 'autonomous', 'pending', 0, 0, 'none', '2026-01-02 00:00:00'),"
                "('ccc-cccc-cccc-cccc-cccccccccccc', 'p2', 'autonomous', 'running', 0, 0, 'none', '2026-01-01 00:00:00')"
            ))

        from database.migrate import _backfill_track_short_ids
        _backfill_track_short_ids(self.engine)

        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT track_id, short_id FROM action_track ORDER BY persona_id, created_at"
            )).fetchall()

        self.assertEqual(rows[0][1], 1)  # p1 first track
        self.assertEqual(rows[1][1], 2)  # p1 second track
        self.assertEqual(rows[2][1], 1)  # p2 first track

    def test_backfill_is_idempotent(self):
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO action_track "
                "(track_id, persona_id, short_id, track_type, status, is_persistent, "
                "is_forgotten, output_target, created_at) "
                "VALUES "
                "('aaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'p1', 1, 'autonomous', 'running', 0, 0, 'none', '2026-01-01 00:00:00')"
            ))

        from database.migrate import _backfill_track_short_ids
        _backfill_track_short_ids(self.engine)

        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT short_id FROM action_track WHERE track_id = 'aaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'"
            )).fetchone()

        self.assertEqual(row[0], 1)


if __name__ == "__main__":
    unittest.main()
