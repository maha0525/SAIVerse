"""Phase A schema tests for cognitive model (Intent A v0.9 / Intent B v0.6).

Verifies that the new tables (action_track) and the new AI columns
(ACTIVITY_STATE, SLEEP_ON_CACHE_EXPIRE) exist with the expected defaults and
constraints.

Note / NotePage / NoteMessage / TrackOpenNote の ORM クラスは P3c①
(concept_consolidation.md「Note → テーマノード移行」) で models.py から削除
された (Note はテーマノードページへ物理統合済み)。それらを検証していた
test_note_defaults / test_note_message_multiple_membership /
test_note_page_link / test_track_open_note_link は同時に削除している。
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
    expected = {"action_track"}
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


def test_action_track_indexes(session):
    """All indexes on action_track are created."""
    inspector = inspect(session.bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("action_track")}
    expected = {
        "idx_action_track_persona_status",
        "idx_action_track_last_active",
        "idx_action_track_persistent",
    }
    assert expected.issubset(indexes)
