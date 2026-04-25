"""Phase A schema tests for cognitive model (Intent A v0.9 / Intent B v0.6).

Verifies that the new tables (action_track, note, note_page, note_message,
track_open_note) and the new AI columns (ACTIVITY_STATE, SLEEP_ON_CACHE_EXPIRE)
exist with the expected defaults and constraints.
"""
import uuid

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from database.models import (
    AI,
    ActionTrack,
    Base,
    City,
    Note,
    NoteMessage,
    NotePage,
    TrackOpenNote,
    User,
)


@pytest.fixture
def session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def persona(session):
    """Create a minimal user/city/AI for FK satisfaction."""
    user = User(USERID=1, PASSWORD="x", USERNAME="tester")
    session.add(user)
    session.flush()
    city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
    session.add(city)
    session.flush()
    ai = AI(AIID="test_persona", HOME_CITYID=city.CITYID, AINAME="Test")
    session.add(ai)
    session.commit()
    return ai


def test_new_tables_exist(session):
    """Phase A schema: all new cognitive-model tables are created."""
    inspector = inspect(session.bind)
    expected = {"action_track", "note", "note_page", "note_message", "track_open_note"}
    assert expected.issubset(set(inspector.get_table_names()))


def test_ai_has_new_activity_columns(session):
    """AI gains ACTIVITY_STATE and SLEEP_ON_CACHE_EXPIRE columns."""
    inspector = inspect(session.bind)
    cols = {c["name"] for c in inspector.get_columns("ai")}
    assert "ACTIVITY_STATE" in cols
    assert "SLEEP_ON_CACHE_EXPIRE" in cols


def test_ai_activity_state_defaults_to_idle(session, persona):
    """New personas default to ACTIVITY_STATE='Idle' and SLEEP_ON_CACHE_EXPIRE=True."""
    refreshed = session.query(AI).filter_by(AIID="test_persona").first()
    assert refreshed.ACTIVITY_STATE == "Idle"
    assert refreshed.SLEEP_ON_CACHE_EXPIRE is True


def test_action_track_defaults(session, persona):
    """ActionTrack defaults: status='unstarted', is_persistent=False, output_target='none'."""
    track_id = str(uuid.uuid4())
    track = ActionTrack(
        track_id=track_id,
        persona_id=persona.AIID,
        track_type="autonomous",
    )
    session.add(track)
    session.commit()

    found = session.query(ActionTrack).filter_by(track_id=track_id).first()
    assert found.status == "unstarted"
    assert found.is_persistent is False
    assert found.is_forgotten is False
    assert found.output_target == "none"
    assert found.completed_at is None
    assert found.aborted_at is None


def test_persistent_track_creation(session, persona):
    """Persistent tracks (user_conversation, social) can be marked is_persistent=True."""
    track = ActionTrack(
        track_id=str(uuid.uuid4()),
        persona_id=persona.AIID,
        track_type="social",
        title="交流",
        is_persistent=True,
        output_target="building:current",
    )
    session.add(track)
    session.commit()

    found = session.query(ActionTrack).filter_by(persona_id=persona.AIID, track_type="social").first()
    assert found.is_persistent is True
    assert found.output_target == "building:current"


def test_note_defaults(session, persona):
    """Note defaults: is_active=True, type is required."""
    note_id = str(uuid.uuid4())
    note = Note(
        note_id=note_id,
        persona_id=persona.AIID,
        title="対 mahomu",
        note_type="person",
    )
    session.add(note)
    session.commit()

    found = session.query(Note).filter_by(note_id=note_id).first()
    assert found.is_active is True
    assert found.note_type == "person"
    assert found.closed_at is None


def test_note_message_multiple_membership(session, persona):
    """A single message can belong to multiple notes (3-way conversation case)."""
    note_a_id = str(uuid.uuid4())
    note_b_id = str(uuid.uuid4())
    session.add_all([
        Note(note_id=note_a_id, persona_id=persona.AIID, title="対 A", note_type="person"),
        Note(note_id=note_b_id, persona_id=persona.AIID, title="対 B", note_type="person"),
    ])
    session.flush()

    message_id = "msg-shared-1"
    session.add_all([
        NoteMessage(note_id=note_a_id, message_id=message_id, auto_added=True),
        NoteMessage(note_id=note_b_id, message_id=message_id, auto_added=True),
    ])
    session.commit()

    rows = session.query(NoteMessage).filter_by(message_id=message_id).all()
    assert len(rows) == 2
    note_ids = {row.note_id for row in rows}
    assert note_ids == {note_a_id, note_b_id}


def test_note_page_link(session, persona):
    """Note-Memopedia page links work as a many-to-many table."""
    note_id = str(uuid.uuid4())
    session.add(Note(note_id=note_id, persona_id=persona.AIID, title="エイド", note_type="person"))
    session.flush()
    session.add(NotePage(note_id=note_id, page_id="memopedia-page-1"))
    session.commit()

    found = session.query(NotePage).filter_by(note_id=note_id).first()
    assert found.page_id == "memopedia-page-1"


def test_track_open_note_link(session, persona):
    """A track can have multiple open notes."""
    track_id = str(uuid.uuid4())
    note1_id = str(uuid.uuid4())
    note2_id = str(uuid.uuid4())

    session.add(ActionTrack(track_id=track_id, persona_id=persona.AIID, track_type="autonomous"))
    session.add_all([
        Note(note_id=note1_id, persona_id=persona.AIID, title="Project N", note_type="project"),
        Note(note_id=note2_id, persona_id=persona.AIID, title="Engineer", note_type="vocation"),
    ])
    session.flush()

    session.add_all([
        TrackOpenNote(track_id=track_id, note_id=note1_id),
        TrackOpenNote(track_id=track_id, note_id=note2_id),
    ])
    session.commit()

    rows = session.query(TrackOpenNote).filter_by(track_id=track_id).all()
    assert len(rows) == 2
    assert {r.note_id for r in rows} == {note1_id, note2_id}


def test_action_track_indexes(session):
    """All four indexes on action_track are created."""
    inspector = inspect(session.bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("action_track")}
    expected = {
        "idx_action_track_persona_status",
        "idx_action_track_last_active",
        "idx_action_track_waiting_timeout",
        "idx_action_track_persistent",
    }
    assert expected.issubset(indexes)
