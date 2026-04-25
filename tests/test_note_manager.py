"""NoteManager unit tests (Phase B-2)."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.note_manager import (
    InvalidNoteTypeError,
    NOTE_TYPE_PERSON,
    NOTE_TYPE_PROJECT,
    NOTE_TYPE_VOCATION,
    NoteManager,
    NoteNotFoundError,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.add(AI(AIID="bob", HOME_CITYID=city.CITYID, AINAME="Bob"))
        db.commit()
    finally:
        db.close()
    return "alice"


@pytest.fixture
def nm(session_factory):
    return NoteManager(session_factory=session_factory)


# ---------------------------------------------------------------------------
# Note CRUD
# ---------------------------------------------------------------------------

def test_create_returns_note_id(nm, persona):
    note_id = nm.create(persona, "対 mahomu", NOTE_TYPE_PERSON)
    note = nm.get(note_id)
    assert note.note_id == note_id
    assert note.persona_id == persona
    assert note.title == "対 mahomu"
    assert note.note_type == NOTE_TYPE_PERSON
    assert note.is_active is True
    assert note.closed_at is None


def test_create_each_type_succeeds(nm, persona):
    for ntype in (NOTE_TYPE_PERSON, NOTE_TYPE_PROJECT, NOTE_TYPE_VOCATION):
        nid = nm.create(persona, f"note-{ntype}", ntype)
        assert nm.get(nid).note_type == ntype


def test_create_rejects_invalid_type(nm, persona):
    with pytest.raises(InvalidNoteTypeError):
        nm.create(persona, "invalid", "topic")
    with pytest.raises(InvalidNoteTypeError):
        nm.create(persona, "invalid", "")


def test_create_requires_persona_and_title(nm):
    with pytest.raises(ValueError):
        nm.create("", "title", NOTE_TYPE_PERSON)
    with pytest.raises(ValueError):
        nm.create("alice", "", NOTE_TYPE_PERSON)


def test_get_raises_when_missing(nm):
    with pytest.raises(NoteNotFoundError):
        nm.get("nope")


def test_list_filters_by_type(nm, persona):
    p1 = nm.create(persona, "対 mahomu", NOTE_TYPE_PERSON)
    p2 = nm.create(persona, "Project N.E.K.O.", NOTE_TYPE_PROJECT)
    nm.create(persona, "engineer", NOTE_TYPE_VOCATION)

    persons = nm.list_for_persona(persona, note_type=NOTE_TYPE_PERSON)
    projects = nm.list_for_persona(persona, note_type=NOTE_TYPE_PROJECT)

    assert {n.note_id for n in persons} == {p1}
    assert {n.note_id for n in projects} == {p2}


def test_list_excludes_inactive_by_default(nm, persona):
    n1 = nm.create(persona, "active", NOTE_TYPE_PERSON)
    n2 = nm.create(persona, "archived", NOTE_TYPE_PERSON)
    nm.archive(n2)

    visible = nm.list_for_persona(persona)
    full = nm.list_for_persona(persona, include_inactive=True)

    assert {n.note_id for n in visible} == {n1}
    assert {n.note_id for n in full} == {n1, n2}


def test_list_invalid_type_raises(nm, persona):
    with pytest.raises(InvalidNoteTypeError):
        nm.list_for_persona(persona, note_type="bogus")


def test_archive_and_unarchive(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.archive(nid)
    assert nm.get(nid).is_active is False
    nm.unarchive(nid)
    assert nm.get(nid).is_active is True


def test_close_project_sets_closed_at(nm, persona):
    nid = nm.create(persona, "Project N", NOTE_TYPE_PROJECT)
    nm.close_project(nid)
    note = nm.get(nid)
    assert note.closed_at is not None
    # is_active is preserved (still readable as a memory)
    assert note.is_active is True


def test_close_project_rejects_non_project_types(nm, persona):
    pid = nm.create(persona, "対 mahomu", NOTE_TYPE_PERSON)
    vid = nm.create(persona, "engineer", NOTE_TYPE_VOCATION)
    with pytest.raises(ValueError):
        nm.close_project(pid)
    with pytest.raises(ValueError):
        nm.close_project(vid)


def test_touch_opened_updates_timestamp(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    assert nm.get(nid).last_opened_at is None
    nm.touch_opened(nid)
    assert nm.get(nid).last_opened_at is not None


# ---------------------------------------------------------------------------
# Note ↔ Memopedia page
# ---------------------------------------------------------------------------

def test_add_and_list_pages(nm, persona):
    nid = nm.create(persona, "対 eid", NOTE_TYPE_PERSON)
    nm.add_page(nid, "page-eid")
    nm.add_page(nid, "page-eid-style")
    pages = nm.list_pages(nid)
    assert set(pages) == {"page-eid", "page-eid-style"}


def test_add_page_is_idempotent(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.add_page(nid, "page-1")
    nm.add_page(nid, "page-1")
    assert nm.list_pages(nid) == ["page-1"]


def test_remove_page(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.add_page(nid, "page-1")
    nm.remove_page(nid, "page-1")
    assert nm.list_pages(nid) == []


def test_remove_page_missing_is_silent(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.remove_page(nid, "ghost-page")  # no exception


def test_add_page_unknown_note_raises(nm):
    with pytest.raises(NoteNotFoundError):
        nm.add_page("ghost-note", "page-1")


# ---------------------------------------------------------------------------
# Note ↔ message
# ---------------------------------------------------------------------------

def test_add_and_list_messages(nm, persona):
    nid = nm.create(persona, "対 mahomu", NOTE_TYPE_PERSON)
    nm.add_message(nid, "msg-1", auto_added=True)
    nm.add_message(nid, "msg-2", auto_added=False)
    msgs = nm.list_messages(nid)
    assert set(msgs) == {"msg-1", "msg-2"}


def test_message_can_belong_to_multiple_notes(nm, persona):
    """3 人会話の核: 同一メッセージが複数 Note に属せる。"""
    n_a = nm.create(persona, "対 A", NOTE_TYPE_PERSON)
    n_b = nm.create(persona, "対 B", NOTE_TYPE_PERSON)
    nm.add_message(n_a, "shared-msg", auto_added=True)
    nm.add_message(n_b, "shared-msg", auto_added=True)

    notes = nm.get_notes_for_message("shared-msg")
    assert set(notes) == {n_a, n_b}


def test_add_message_idempotent(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.add_message(nid, "msg-1")
    nm.add_message(nid, "msg-1")  # no duplicate
    assert nm.list_messages(nid) == ["msg-1"]


def test_remove_message(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.add_message(nid, "msg-1")
    nm.remove_message(nid, "msg-1")
    assert nm.list_messages(nid) == []


# ---------------------------------------------------------------------------
# Track ↔ open Note
# ---------------------------------------------------------------------------

def test_attach_and_list_open_notes(nm, persona):
    n1 = nm.create(persona, "Project N", NOTE_TYPE_PROJECT)
    n2 = nm.create(persona, "engineer", NOTE_TYPE_VOCATION)
    nm.attach_to_track("track-X", n1)
    nm.attach_to_track("track-X", n2)
    opens = nm.list_open_notes("track-X")
    assert {n.note_id for n in opens} == {n1, n2}


def test_attach_idempotent(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.attach_to_track("track-X", nid)
    nm.attach_to_track("track-X", nid)
    opens = nm.list_open_notes("track-X")
    assert len(opens) == 1


def test_detach(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.attach_to_track("track-X", nid)
    nm.detach_from_track("track-X", nid)
    assert nm.list_open_notes("track-X") == []


def test_detach_missing_silent(nm, persona):
    nid = nm.create(persona, "n", NOTE_TYPE_PERSON)
    nm.detach_from_track("track-X", nid)  # no exception even when not attached


def test_attach_unknown_note_raises(nm):
    with pytest.raises(NoteNotFoundError):
        nm.attach_to_track("track-X", "ghost-note")


def test_list_tracks_with_note(nm, persona):
    """同一 Note が複数 Track で開かれているケース。"""
    n = nm.create(persona, "Project N", NOTE_TYPE_PROJECT)
    nm.attach_to_track("track-A", n)
    nm.attach_to_track("track-B", n)
    tracks = nm.list_tracks_with_note(n)
    assert set(tracks) == {"track-A", "track-B"}
