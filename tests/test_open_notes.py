"""OpenNotesSection (desire ノート→head) + purpose_seed スペルのテスト。

autonomous_desire.md §5/§7: desire ノートは候補プールの器、open_notes は
それを head に注入、purpose_seed (旧 desire_add 後継、P2c-4a で撤去) は
AUTONOMOUS が候補 Task を足すスペル。
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, ActionTrack, Base, City, User
from saiverse.note_manager import NOTE_TYPE_PROJECT, NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PARENT_TRACK, PersonaTaskManager
from saiverse.track_manager import TrackManager
from sea.head_pipeline.types import LineHeadInput
from sea.head_pipeline.sections.open_notes import (
    DesireCandidateItem,
    OpenNoteItem,
    OpenNotesSection,
    OpenNotesSnapshot,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="c", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="air", HOME_CITYID=city.CITYID, AINAME="Air"))
        db.commit()
    finally:
        db.close()
    return engine, Session


def _ctx(persona_id, manager):
    return LineHeadInput(
        persona_id=persona_id, line_id="main", line_role="main_line",
        model_key="claude-opus-4-8", current_building_id="b",
        persona=None, manager=manager,
    )


class OpenNotesSectionTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.Session = _make_db()
        self.manager = SimpleNamespace(SessionLocal=self.Session)
        self.nm = NoteManager(self.Session)
        self.ptm = PersonaTaskManager(self.Session)

    def tearDown(self):
        self.engine.dispose()

    def _add_running_track(self, persona_id="air", short_id=1):
        track_id = f"track-{persona_id}-{short_id}"
        db = self.Session()
        try:
            db.add(ActionTrack(
                track_id=track_id, persona_id=persona_id, short_id=short_id,
                track_type="autonomous", status="running",
            ))
            db.commit()
        finally:
            db.close()
        return track_id

    def test_capture_empty_when_no_desire_note(self):
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertEqual(snap.desire_note_title, "")
        self.assertEqual(snap.candidates, ())
        self.assertEqual(snap.open_notes, ())
        # render は何も出さない。
        self.assertIsNone(OpenNotesSection().render(snap))

    def test_capture_lists_candidate_tasks(self):
        note_id = self.nm.ensure_desire_note("air")
        self.ptm.create_task(
            persona_id="air", title="風景スケッチを練習したい",
            parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        )
        self.ptm.create_task(
            persona_id="air", title="あのページを調べたい",
            parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        )
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertTrue(snap.desire_note_title)
        self.assertEqual(len(snap.candidates), 2)
        rendered = OpenNotesSection().render(snap)
        self.assertIsNotNone(rendered)
        self.assertIn("やりたいこと候補", rendered.text)
        self.assertIn("風景スケッチを練習したい", rendered.text)

    def test_capture_lists_attached_open_notes_of_running_track(self):
        track_id = self._add_running_track()
        nid = self.nm.create("air", "Project N.E.K.O.", NOTE_TYPE_PROJECT,
                             description="猫型ロボの設計")
        self.nm.attach_to_track(track_id, nid)
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertEqual(len(snap.open_notes), 1)
        self.assertEqual(snap.open_notes[0].title, "Project N.E.K.O.")
        rendered = OpenNotesSection().render(snap)
        self.assertIsNotNone(rendered)
        self.assertIn("いま開いているノート", rendered.text)
        self.assertIn("Project N.E.K.O.", rendered.text)

    def test_attached_notes_ignored_without_running_track(self):
        # running Track が無ければ attach ノートは出ない (= 焼かれない)。
        track_id = "track-detached"
        nid = self.nm.create("air", "孤立ノート", NOTE_TYPE_PROJECT)
        self.nm.attach_to_track(track_id, nid)  # この track は running ではない
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertEqual(snap.open_notes, ())

    def test_completed_candidate_excluded(self):
        note_id = self.nm.ensure_desire_note("air")
        task = self.ptm.create_task(
            persona_id="air", title="済んだ候補",
            parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        )
        self.ptm.update_task_status(
            task["id"], status="completed", actor=None, persona_id="air",
        )
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertEqual(snap.candidates, ())

    def test_promoted_candidate_leaves_pool(self):
        """昇格 (note_id→track_id) した候補は候補プールから消える。"""
        note_id = self.nm.ensure_desire_note("air")
        task = self.ptm.create_task(
            persona_id="air", title="昇格させる候補",
            parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        )
        self.ptm.promote_to_track(
            task["id"], track_id="track-1", actor=None, persona_id="air",
        )
        snap = OpenNotesSection().capture(_ctx("air", self.manager))
        self.assertEqual(snap.candidates, ())

    def test_diff_notifies_on_new_candidate(self):
        old = OpenNotesSnapshot(desire_note_title="やりたいこと", open_notes=(), candidates=(
            DesireCandidateItem(task_ref="task:1", title="A", status="pending"),
        ))
        new = OpenNotesSnapshot(desire_note_title="やりたいこと", open_notes=(), candidates=(
            DesireCandidateItem(task_ref="task:1", title="A", status="pending"),
            DesireCandidateItem(task_ref="task:2", title="B", status="pending"),
        ))
        labels = OpenNotesSection().diff_to_notifications(old, new)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "desire_candidate_added")

    def test_diff_notifies_on_open_notes_change(self):
        old = OpenNotesSnapshot(desire_note_title="", candidates=(), open_notes=())
        new = OpenNotesSnapshot(desire_note_title="", candidates=(), open_notes=(
            OpenNoteItem(note_type="project", title="P", description=""),
        ))
        labels = OpenNotesSection().diff_to_notifications(old, new)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "open_notes_changed")

    def test_snapshot_roundtrip(self):
        snap = OpenNotesSnapshot(
            desire_note_title="やりたいこと",
            candidates=(DesireCandidateItem(task_ref="task:3", title="X", status="active"),),
            open_notes=(OpenNoteItem(note_type="project", title="P", description="d"),),
        )
        section = OpenNotesSection()
        restored = section.deserialize_snapshot(section.serialize_snapshot(snap))
        self.assertEqual(restored, snap)


class DesireAddSpellTest(unittest.TestCase):
    def setUp(self):
        from tool_loader import load_builtin_tool

        self.engine, self.Session = _make_db()
        self.nm = NoteManager(self.Session)
        self.ptm = PersonaTaskManager(self.Session)
        self.mod = load_builtin_tool("purpose_seed")
        # module-singleton を temp DB 版へ差し替え (動的ロード = patch.object 相当)。
        self.mod._note_manager = self.nm
        self.mod._task_manager = self.ptm

        self.tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self.tmp.name) / "air"
        self.persona_dir.mkdir(parents=True)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_desire_add_creates_note_bound_candidate(self):
        from tools.context import persona_context

        with persona_context("air", self.persona_dir):
            out = self.mod.purpose_seed(title="新しい言語を学びたい", goal="表現の幅を広げる")
        self.assertIn("task:1", out)
        # desire ノートが ensure され、候補 Task が note 紐付けで作られている。
        note_id = self.nm.ensure_desire_note("air")
        tasks = self.ptm.list_tasks("air", note_id=note_id)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["parent_kind"], PARENT_NOTE)
        self.assertEqual(tasks[0]["title"], "新しい言語を学びたい")


class TrackCreatePromoteTest(unittest.TestCase):
    """track_create(from_candidate=) で候補 Task が Track へ昇格する (§6, 案A)。"""

    def setUp(self):
        from tool_loader import load_builtin_tool

        self.engine, self.Session = _make_db()
        self.nm = NoteManager(self.Session)
        self.ptm = PersonaTaskManager(self.Session)
        self.tm = TrackManager(session_factory=self.Session)
        self.mod = load_builtin_tool("track_create")
        self.mod._track_manager = self.tm
        self.mod._task_manager = self.ptm

        self.tmp = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self.tmp.name) / "air"
        self.persona_dir.mkdir(parents=True)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_from_candidate_promotes_into_new_track(self):
        from tools.context import persona_context

        note_id = self.nm.ensure_desire_note("air")
        cand = self.ptm.create_task(
            persona_id="air", title="新言語を学ぶ",
            parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
        )
        ref = cand["task_ref"]
        with persona_context("air", self.persona_dir):
            msg, _snippet, _ = self.mod.track_create(
                track_type="autonomous", title="言語学習",
                from_candidate=ref, activate=False,
            )
        self.assertIn("昇格", msg)
        # 候補プールから消えている。
        pool = self.ptm.list_tasks("air", note_id=note_id)
        self.assertEqual(pool, [])
        # 昇格後の Task は Track 紐付けになっている。
        promoted = self.ptm.get_task(cand["id"], persona_id="air")
        self.assertEqual(promoted["parent_kind"], PARENT_TRACK)
        self.assertIsNone(promoted["note_id"])


if __name__ == "__main__":
    unittest.main()
