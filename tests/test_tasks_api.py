"""タスク一覧 API (所属ラベル付き全タスク) のテスト。

api/routes/people/tasks.py の list_tasks: 候補(note) / Track 小目標(track) / 単独
を parent_kind + task_ref + parent_label 付きで返す (unified_task_model.md)。
"""
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.people.tasks import list_tasks
from database.models import AI, ActionTrack, Base, City, User
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PARENT_TRACK, PersonaTaskManager


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="t"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="air", HOME_CITYID=city.CITYID, AINAME="Air"))
        db.add(ActionTrack(
            track_id="trk1", persona_id="air", short_id=2,
            track_type="autonomous", status="running", title="言語学習",
        ))
        db.commit()
    finally:
        db.close()
    return engine, Session


class TasksListApiTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _make_db()
        self.manager = SimpleNamespace(SessionLocal=self.Session)
        self.nm = NoteManager(self.Session)
        self.ptm = PersonaTaskManager(self.Session)

    def tearDown(self):
        self.engine.dispose()

    def test_list_returns_all_kinds_with_parent_labels(self):
        desire = self.nm.ensure_desire_note("air")
        self.ptm.create_task(
            persona_id="air", title="候補タスク",
            parent_kind=PARENT_NOTE, note_id=desire, auto_activate=False,
        )
        self.ptm.create_task(
            persona_id="air", title="Track小目標",
            parent_kind=PARENT_TRACK, track_id="trk1", auto_activate=False,
        )
        self.ptm.create_task(persona_id="air", title="単独タスク", auto_activate=False)

        result = list_tasks("air", manager=self.manager)
        by_title = {r.title: r for r in result}
        self.assertEqual(len(result), 3)

        self.assertEqual(by_title["候補タスク"].parent_kind, "note")
        self.assertEqual(by_title["候補タスク"].parent_label, "候補")

        track_task = by_title["Track小目標"]
        self.assertEqual(track_task.parent_kind, "track")
        self.assertEqual(track_task.parent_label, "track:2 言語学習")

        solo = by_title["単独タスク"]
        self.assertIsNone(solo.parent_kind)
        self.assertEqual(solo.parent_label, "単独")
        # task_ref が全件に付く。
        for r in result:
            self.assertTrue(r.task_ref and r.task_ref.startswith("task:"))


if __name__ == "__main__":
    unittest.main()
