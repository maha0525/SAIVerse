"""候補補充 Track (autonomous_desire.md §11) の ensure テスト。"""
import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import ActionTrack, Base
from saiverse.track_manager import DESIRE_REFILL_ROLE, TrackManager


class DesireRefillTrackTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.tm = TrackManager(session_factory=self.Session)

    def tearDown(self):
        self.engine.dispose()

    def _get(self, track_id):
        db = self.Session()
        try:
            return db.query(ActionTrack).filter_by(track_id=track_id).first()
        finally:
            db.close()

    def test_ensure_creates_persistent_autonomous_track(self):
        tid = self.tm.ensure_desire_refill_track("air")
        row = self._get(tid)
        self.assertEqual(row.track_type, "autonomous")
        self.assertTrue(row.is_persistent)
        self.assertEqual(row.status, "unstarted")  # idle_with_pending で拾われる
        md = json.loads(row.track_metadata)
        self.assertEqual(md["role"], DESIRE_REFILL_ROLE)
        self.assertEqual(md["entry_line_role"], "sub_line")

    def test_ensure_is_idempotent(self):
        a = self.tm.ensure_desire_refill_track("air")
        b = self.tm.ensure_desire_refill_track("air")
        self.assertEqual(a, b)
        # 重複作成されていない (補充 Track は 1 本だけ)。
        db = self.Session()
        try:
            refill = [
                t for t in db.query(ActionTrack).filter_by(persona_id="air").all()
                if t.track_metadata and json.loads(t.track_metadata).get("role") == DESIRE_REFILL_ROLE
            ]
            self.assertEqual(len(refill), 1)
        finally:
            db.close()

    def test_per_persona(self):
        self.assertNotEqual(
            self.tm.ensure_desire_refill_track("air"),
            self.tm.ensure_desire_refill_track("bob"),
        )


if __name__ == "__main__":
    unittest.main()
