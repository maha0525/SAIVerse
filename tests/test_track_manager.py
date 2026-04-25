"""TrackManager unit tests (Phase B-1).

In-memory SQLite で完結する純粋ロジックテスト。実機環境を必要としない。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.track_manager import (
    InvalidTrackStateError,
    PersistentTrackError,
    STATUS_ABORTED,
    STATUS_ALERT,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNSTARTED,
    STATUS_WAITING,
    TrackManager,
    TrackNotFoundError,
)


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona(session_factory):
    """Create minimal user/city/AI for FK satisfaction."""
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
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_returns_track_id_with_unstarted_status(tm, persona):
    track_id = tm.create(persona, "autonomous")
    track = tm.get(track_id)
    assert track.track_id == track_id
    assert track.persona_id == persona
    assert track.status == STATUS_UNSTARTED
    assert track.is_persistent is False
    assert track.output_target == "none"


def test_create_persistent_track(tm, persona):
    track_id = tm.create(
        persona, "social",
        title="交流",
        is_persistent=True,
        output_target="building:current",
    )
    track = tm.get(track_id)
    assert track.is_persistent is True
    assert track.output_target == "building:current"


def test_create_requires_persona_and_type(tm):
    with pytest.raises(ValueError):
        tm.create("", "autonomous")
    with pytest.raises(ValueError):
        tm.create("alice", "")


def test_get_raises_when_not_found(tm):
    with pytest.raises(TrackNotFoundError):
        tm.get("nonexistent")


def test_list_filters_by_status(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.activate(t1)

    running = tm.list_for_persona(persona, statuses=[STATUS_RUNNING])
    unstarted = tm.list_for_persona(persona, statuses=[STATUS_UNSTARTED])

    assert {t.track_id for t in running} == {t1}
    assert {t.track_id for t in unstarted} == {t2}


def test_list_excludes_forgotten_by_default(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.forget(t2)

    visible = tm.list_for_persona(persona)
    full = tm.list_for_persona(persona, include_forgotten=True)

    assert {t.track_id for t in visible} == {t1}
    assert {t.track_id for t in full} == {t1, t2}


def test_get_running_returns_none_when_no_active(tm, persona):
    assert tm.get_running(persona) is None
    track_id = tm.create(persona, "autonomous")
    assert tm.get_running(persona) is None
    tm.activate(track_id)
    running = tm.get_running(persona)
    assert running is not None
    assert running.track_id == track_id


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_activate_pushes_existing_running_to_pending(tm, persona):
    t1 = tm.create(persona, "autonomous")
    t2 = tm.create(persona, "autonomous")
    tm.activate(t1)
    assert tm.get(t1).status == STATUS_RUNNING

    tm.activate(t2)

    assert tm.get(t1).status == STATUS_PENDING
    assert tm.get(t2).status == STATUS_RUNNING
    # Only one running per persona
    running = tm.list_for_persona(persona, statuses=[STATUS_RUNNING])
    assert len(running) == 1


def test_activate_does_not_affect_other_personas(tm, persona, session_factory):
    t_alice = tm.create("alice", "autonomous")
    t_bob = tm.create("bob", "autonomous")
    tm.activate(t_alice)
    tm.activate(t_bob)

    assert tm.get(t_alice).status == STATUS_RUNNING
    assert tm.get(t_bob).status == STATUS_RUNNING


def test_activate_rejects_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.activate(t)


def test_pause(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    assert tm.get(t).status == STATUS_PENDING


def test_pause_rejects_unstarted(tm, persona):
    t = tm.create(persona, "autonomous")
    with pytest.raises(InvalidTrackStateError):
        tm.pause(t)


def test_wait_sets_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"type":"user_response"}', timeout_seconds=600)
    track = tm.get(t)
    assert track.status == STATUS_WAITING
    assert track.waiting_for == '{"type":"user_response"}'
    assert track.waiting_timeout_at is not None


def test_wait_without_timeout_keeps_null(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"type":"user_response"}')
    assert tm.get(t).waiting_timeout_at is None


def test_resume_from_wait_activate(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    tm.resume_from_wait(t, "activate")
    assert tm.get(t).status == STATUS_RUNNING


def test_resume_from_wait_pause_clears_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}', timeout_seconds=60)
    tm.resume_from_wait(t, "pause")
    track = tm.get(t)
    assert track.status == STATUS_PENDING
    assert track.waiting_for is None
    assert track.waiting_timeout_at is None


def test_resume_from_wait_abort(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    tm.resume_from_wait(t, "abort")
    track = tm.get(t)
    assert track.status == STATUS_ABORTED
    assert track.aborted_at is not None


def test_resume_from_wait_invalid_mode(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    with pytest.raises(ValueError):
        tm.resume_from_wait(t, "explode")


def test_resume_from_wait_requires_waiting_status(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)  # running, not waiting
    with pytest.raises(InvalidTrackStateError):
        tm.resume_from_wait(t, "activate")


def test_complete_sets_timestamp(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    track = tm.get(t)
    assert track.status == STATUS_COMPLETED
    assert track.completed_at is not None


def test_complete_rejects_non_running(tm, persona):
    t = tm.create(persona, "autonomous")
    with pytest.raises(InvalidTrackStateError):
        tm.complete(t)


def test_abort_from_pending(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.abort(t)
    assert tm.get(t).status == STATUS_ABORTED


def test_abort_clears_waiting_fields(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}', timeout_seconds=60)
    tm.abort(t)
    track = tm.get(t)
    assert track.status == STATUS_ABORTED
    assert track.waiting_for is None
    assert track.waiting_timeout_at is None


def test_abort_rejects_already_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.abort(t)


def test_set_alert_from_pending(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.pause(t)
    tm.set_alert(t)
    assert tm.get(t).status == STATUS_ALERT


def test_set_alert_no_op_when_running(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.set_alert(t)
    # running stays running (no-op)
    assert tm.get(t).status == STATUS_RUNNING


def test_set_alert_rejects_terminal(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.complete(t)
    with pytest.raises(InvalidTrackStateError):
        tm.set_alert(t)


# ---------------------------------------------------------------------------
# Persistent track constraints
# ---------------------------------------------------------------------------

def test_persistent_track_cannot_complete(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    with pytest.raises(PersistentTrackError):
        tm.complete(t)


def test_persistent_track_cannot_abort(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    with pytest.raises(PersistentTrackError):
        tm.abort(t)


def test_persistent_track_cannot_abort_from_wait(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    with pytest.raises(PersistentTrackError):
        tm.resume_from_wait(t, "abort")


def test_persistent_track_can_pause(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.pause(t)
    assert tm.get(t).status == STATUS_PENDING


def test_persistent_track_can_wait(tm, persona):
    t = tm.create(persona, "social", is_persistent=True)
    tm.activate(t)
    tm.wait(t, waiting_for='{"x":1}')
    assert tm.get(t).status == STATUS_WAITING


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------

def test_forget_and_recall(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.forget(t)
    assert tm.get(t).is_forgotten is True
    tm.recall(t)
    assert tm.get(t).is_forgotten is False


def test_forget_does_not_affect_status(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    tm.forget(t)
    track = tm.get(t)
    assert track.status == STATUS_RUNNING
    assert track.is_forgotten is True


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_operations_on_unknown_track_raise(tm):
    for op_name, args in [
        ("activate", ("nope",)),
        ("pause", ("nope",)),
        ("wait", ("nope", '{"x":1}')),
        ("complete", ("nope",)),
        ("abort", ("nope",)),
        ("set_alert", ("nope",)),
        ("forget", ("nope",)),
        ("recall", ("nope",)),
    ]:
        op = getattr(tm, op_name)
        with pytest.raises(TrackNotFoundError):
            op(*args)


def test_wait_requires_waiting_for(tm, persona):
    t = tm.create(persona, "autonomous")
    tm.activate(t)
    with pytest.raises(ValueError):
        tm.wait(t, waiting_for="")
