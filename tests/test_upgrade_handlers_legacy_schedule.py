"""Test for v0_3_0_dev1_legacy_schedule_playbook_names upgrade handler.

Verifies that schedules with deprecated meta_* / basic_chat / sub_router_user
playbook names get rewritten to track_user_conversation, while leaving valid
schedules and other personas alone.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import AI, Base, City, PersonaSchedule, User
from saiverse.upgrade_handlers import _v0_3_0_dev1_legacy_schedule_playbook_names


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    # 共通の User / City を 1 件だけ用意 (City.USERID NOT NULL 制約対応)
    sess.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
    sess.commit()
    sess.add(City(CITYID=1, CITYNAME="test_city", USERID=1, UI_PORT=3000, API_PORT=8000))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _make_ai(session: Session, persona_id: str = "test_pid") -> AI:
    ai = AI(AIID=persona_id, HOME_CITYID=1, AINAME=persona_id)
    session.add(ai)
    session.commit()
    return ai


def _make_schedule(session: Session, persona_id: str, meta_playbook: str) -> PersonaSchedule:
    schedule = PersonaSchedule(
        PERSONA_ID=persona_id,
        SCHEDULE_TYPE="oneshot",
        META_PLAYBOOK=meta_playbook,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_rewrites_meta_user(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, "meta_user")

    _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    assert sched.META_PLAYBOOK == "track_user_conversation"


def test_rewrites_all_deprecated_names(session: Session) -> None:
    ai = _make_ai(session)
    deprecated = ["meta_user", "meta_user_manual", "basic_chat", "sub_router_user"]
    schedules = [_make_schedule(session, ai.AIID, name) for name in deprecated]

    _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai)
    session.commit()

    for sched in schedules:
        session.refresh(sched)
        assert sched.META_PLAYBOOK == "track_user_conversation"


def test_does_not_touch_valid_playbook_names(session: Session) -> None:
    ai = _make_ai(session)
    sched_track = _make_schedule(session, ai.AIID, "track_user_conversation")
    sched_simple = _make_schedule(session, ai.AIID, "meta_simple_speak")

    _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai)
    session.commit()

    session.refresh(sched_track)
    session.refresh(sched_simple)
    assert sched_track.META_PLAYBOOK == "track_user_conversation"
    assert sched_simple.META_PLAYBOOK == "meta_simple_speak"


def test_idempotent(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, "meta_user")

    for _ in range(3):
        _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai)
        session.commit()

    session.refresh(sched)
    assert sched.META_PLAYBOOK == "track_user_conversation"


def test_does_not_touch_other_persona_schedules(session: Session) -> None:
    ai_a = _make_ai(session, persona_id="ai_a")
    _make_ai(session, persona_id="ai_b")
    sched_other = _make_schedule(session, "ai_b", "meta_user")

    _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai_a)
    session.commit()

    session.refresh(sched_other)
    # 別ペルソナのスケジュールは触らない (副作用の局所化)
    assert sched_other.META_PLAYBOOK == "meta_user"


def test_no_schedules_is_no_op(session: Session) -> None:
    ai = _make_ai(session)

    # 例外が出ないことを確認
    _v0_3_0_dev1_legacy_schedule_playbook_names(session=session, ai=ai)


def test_handler_registered_in_handlers_list() -> None:
    """ハンドラが HANDLERS に登録されていること (起動時に走る前提)。"""
    from saiverse.upgrade_handlers import HANDLERS

    names = {h.name for h in HANDLERS}
    assert "v0_3_0_dev1_legacy_schedule_playbook_names" in names

    h = next(h for h in HANDLERS if h.name == "v0_3_0_dev1_legacy_schedule_playbook_names")
    assert h.scope == "ai"
    assert h.from_version == "0.3.0.dev0"
    assert h.to_version == "0.3.0.dev1"
