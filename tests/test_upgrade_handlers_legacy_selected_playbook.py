"""Test for v0_3_0_dev2_legacy_schedule_selected_playbook upgrade handler.

Verifies that legacy ``PLAYBOOK_PARAMS.selected_playbook`` (orphaned by the
meta_user_manual removal in Phase 3) gets converted to the new pre_spells
format so existing schedules continue to invoke their target Spell after the
upgrade.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import AI, Base, City, PersonaSchedule, User
from saiverse.upgrade_handlers import _v0_3_0_dev2_legacy_schedule_selected_playbook


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    sess.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
    sess.commit()
    sess.add(City(CITYID=1, CITY_SLUG="test_city", USERID=1, UI_PORT=3000, API_PORT=8000))
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


def _make_schedule(
    session: Session,
    persona_id: str,
    playbook_params: dict | None,
    meta_playbook: str = "track_user_conversation",
) -> PersonaSchedule:
    schedule = PersonaSchedule(
        PERSONA_ID=persona_id,
        SCHEDULE_TYPE="oneshot",
        META_PLAYBOOK=meta_playbook,
        PLAYBOOK_PARAMS=json.dumps(playbook_params) if playbook_params is not None else None,
    )
    session.add(schedule)
    session.commit()
    return schedule


def test_converts_selected_playbook_to_pre_spells(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, {"selected_playbook": "send_email_to_user"})

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    params = json.loads(sched.PLAYBOOK_PARAMS)
    assert "selected_playbook" not in params
    assert params["pre_spells"] == ["/spell name='send_email_to_user'"]


def test_appends_to_existing_pre_spells(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(
        session,
        ai.AIID,
        {
            "selected_playbook": "send_email_to_user",
            "pre_spells": ["/spell name='other_spell'"],
        },
    )

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    params = json.loads(sched.PLAYBOOK_PARAMS)
    assert "selected_playbook" not in params
    assert params["pre_spells"] == [
        "/spell name='other_spell'",
        "/spell name='send_email_to_user'",
    ]


def test_drops_empty_selected_playbook(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(
        session,
        ai.AIID,
        {"selected_playbook": "", "other_field": "keep"},
    )

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    params = json.loads(sched.PLAYBOOK_PARAMS)
    assert "selected_playbook" not in params
    assert "pre_spells" not in params
    assert params["other_field"] == "keep"


def test_skips_schedules_without_selected_playbook(session: Session) -> None:
    ai = _make_ai(session)
    original_params = {"some_field": "value"}
    sched = _make_schedule(session, ai.AIID, original_params)

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    params = json.loads(sched.PLAYBOOK_PARAMS)
    # Unchanged
    assert params == original_params


def test_skips_null_playbook_params(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, None)

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    assert sched.PLAYBOOK_PARAMS is None


def test_idempotent(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, {"selected_playbook": "send_email_to_user"})

    for _ in range(3):
        _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
        session.commit()

    session.refresh(sched)
    params = json.loads(sched.PLAYBOOK_PARAMS)
    # 1 回目で変換、2-3 回目は selected_playbook が無いので no-op
    assert params["pre_spells"] == ["/spell name='send_email_to_user'"]


def test_empty_dict_playbook_params_after_conversion(session: Session) -> None:
    """selected_playbook が空文字で他にキーが無い場合、PLAYBOOK_PARAMS は NULL に。"""
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, {"selected_playbook": ""})

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    assert sched.PLAYBOOK_PARAMS is None


def test_does_not_touch_other_persona_schedules(session: Session) -> None:
    ai_a = _make_ai(session, persona_id="ai_a")
    _make_ai(session, persona_id="ai_b")
    sched_other = _make_schedule(session, "ai_b", {"selected_playbook": "X"})

    _v0_3_0_dev2_legacy_schedule_selected_playbook(session=session, ai=ai_a)
    session.commit()

    session.refresh(sched_other)
    params = json.loads(sched_other.PLAYBOOK_PARAMS)
    # 別ペルソナのスケジュールは触らない
    assert params == {"selected_playbook": "X"}


def test_handler_registered_in_handlers_list() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    names = {h.name for h in HANDLERS}
    assert "v0_3_0_dev2_legacy_schedule_selected_playbook" in names

    h = next(h for h in HANDLERS if h.name == "v0_3_0_dev2_legacy_schedule_selected_playbook")
    assert h.scope == "ai"
    assert h.from_version == "0.3.0.dev1"
    assert h.to_version == "0.3.0.dev2"
