"""Tests for the v0.3.0.dev4 retired-autonomy-playbook upgrade handlers (P2c-3).

④オートノミー整理「時間割へ完全移行」裁定 (docs/overview/v030_release_worklist.md)
で退役した track_autonomous / meta_autonomy_decision の巻き取り:

- persona_schedule.META_PLAYBOOK → track_user_conversation (ai スコープ)
- UserSettings.SELECTED_META_PLAYBOOK → NULL (= auto、city スコープ)

流儀は tests/test_upgrade_handlers_legacy_schedule.py に従う。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import AI, Base, City, PersonaSchedule, User, UserSettings
from saiverse.upgrade_handlers import (
    _v0_3_0_dev4_retired_autonomy_schedule_names,
    _v0_3_0_dev4_retired_autonomy_selected_playbook,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    sess.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
    sess.commit()
    sess.add(City(CITYID=1, CITYNAME="test_city", USERID=1, UI_PORT=3000, API_PORT=8000))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


@pytest.fixture
def city(session: Session) -> City:
    return session.query(City).first()


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


def _make_user_settings(session: Session, selected: str) -> UserSettings:
    settings = UserSettings(USERID=1, SELECTED_META_PLAYBOOK=selected)
    session.add(settings)
    session.commit()
    return settings


# ---------------------------------------------------------------------------
# schedule 巻き取り (ai スコープ)
# ---------------------------------------------------------------------------


def test_rewrites_retired_schedule_names(session: Session) -> None:
    ai = _make_ai(session)
    scheds = [
        _make_schedule(session, ai.AIID, "track_autonomous"),
        _make_schedule(session, ai.AIID, "meta_autonomy_decision"),
    ]

    _v0_3_0_dev4_retired_autonomy_schedule_names(session=session, ai=ai)
    session.commit()

    for sched in scheds:
        session.refresh(sched)
        assert sched.META_PLAYBOOK == "track_user_conversation"


def test_schedule_rewrite_does_not_touch_valid_names(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, "track_user_conversation")

    _v0_3_0_dev4_retired_autonomy_schedule_names(session=session, ai=ai)
    session.commit()

    session.refresh(sched)
    assert sched.META_PLAYBOOK == "track_user_conversation"


def test_schedule_rewrite_is_idempotent(session: Session) -> None:
    ai = _make_ai(session)
    sched = _make_schedule(session, ai.AIID, "track_autonomous")

    for _ in range(3):
        _v0_3_0_dev4_retired_autonomy_schedule_names(session=session, ai=ai)
        session.commit()

    session.refresh(sched)
    assert sched.META_PLAYBOOK == "track_user_conversation"


def test_schedule_rewrite_scoped_to_own_persona(session: Session) -> None:
    ai_a = _make_ai(session, persona_id="ai_a")
    _make_ai(session, persona_id="ai_b")
    sched_other = _make_schedule(session, "ai_b", "track_autonomous")

    _v0_3_0_dev4_retired_autonomy_schedule_names(session=session, ai=ai_a)
    session.commit()

    session.refresh(sched_other)
    assert sched_other.META_PLAYBOOK == "track_autonomous"


# ---------------------------------------------------------------------------
# UserSettings.SELECTED_META_PLAYBOOK 巻き取り (city スコープ)
# ---------------------------------------------------------------------------


def test_selected_playbook_reset_to_null(session: Session, city: City) -> None:
    settings = _make_user_settings(session, "track_autonomous")

    _v0_3_0_dev4_retired_autonomy_selected_playbook(session=session, city=city)
    session.commit()

    session.refresh(settings)
    # NULL = 起動時ロード (saiverse_manager) がスキップされ auto 既定で動く
    assert settings.SELECTED_META_PLAYBOOK is None


def test_selected_playbook_keeps_valid_value(session: Session, city: City) -> None:
    settings = _make_user_settings(session, "track_user_conversation")

    _v0_3_0_dev4_retired_autonomy_selected_playbook(session=session, city=city)
    session.commit()

    session.refresh(settings)
    assert settings.SELECTED_META_PLAYBOOK == "track_user_conversation"


def test_selected_playbook_reset_is_idempotent(session: Session, city: City) -> None:
    settings = _make_user_settings(session, "meta_autonomy_decision")

    for _ in range(3):
        _v0_3_0_dev4_retired_autonomy_selected_playbook(session=session, city=city)
        session.commit()

    session.refresh(settings)
    assert settings.SELECTED_META_PLAYBOOK is None


def test_no_rows_is_no_op(session: Session, city: City) -> None:
    ai = _make_ai(session)
    # 例外が出ないことを確認
    _v0_3_0_dev4_retired_autonomy_schedule_names(session=session, ai=ai)
    _v0_3_0_dev4_retired_autonomy_selected_playbook(session=session, city=city)


# ---------------------------------------------------------------------------
# 登録・遷移の検証
# ---------------------------------------------------------------------------


def test_handlers_registered_with_dev4_transition() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    names = {h.name for h in HANDLERS}
    assert "v0_3_0_dev4_retired_autonomy_schedule_names" in names
    assert "v0_3_0_dev4_retired_autonomy_selected_playbook" in names

    sched_h = next(
        h for h in HANDLERS if h.name == "v0_3_0_dev4_retired_autonomy_schedule_names"
    )
    assert sched_h.scope == "ai"
    assert sched_h.from_version == "0.3.0.dev3"
    assert sched_h.to_version == "0.3.0.dev4"

    sel_h = next(
        h for h in HANDLERS
        if h.name == "v0_3_0_dev4_retired_autonomy_selected_playbook"
    )
    assert sel_h.scope == "city"
    assert sel_h.from_version == "0.3.0.dev3"
    assert sel_h.to_version == "0.3.0.dev4"


def test_dev3_to_dev4_transition_selects_handlers() -> None:
    """既存ユーザーの多くは LAST_KNOWN_VERSION=0.3.0.dev3。dev3 → dev4 の遷移で
    両ハンドラが選択されること (現行 VERSION = 0.3.0.dev4 で実際に走る保証)。"""
    from saiverse.upgrade import parse_version, select_handlers
    from saiverse.upgrade_handlers import HANDLERS as MODULE_HANDLERS
    from saiverse import upgrade

    # select_handlers はモジュールグローバル HANDLERS を見るため一時登録する
    original = list(upgrade.HANDLERS)
    upgrade.HANDLERS.clear()
    upgrade.HANDLERS.extend(MODULE_HANDLERS)
    try:
        current = parse_version("0.3.0.dev3")
        target = parse_version("0.3.0.dev4")
        ai_selected = {h.name for h in select_handlers("ai", current, target)}
        city_selected = {h.name for h in select_handlers("city", current, target)}
    finally:
        upgrade.HANDLERS.clear()
        upgrade.HANDLERS.extend(original)

    assert "v0_3_0_dev4_retired_autonomy_schedule_names" in ai_selected
    assert "v0_3_0_dev4_retired_autonomy_selected_playbook" in city_selected


def test_current_version_is_at_least_dev4() -> None:
    """VERSION ファイルが 0.3.0.dev4 以上であること — dev4 ハンドラは
    コードバージョンが dev4 に達していないと永遠に走らない。"""
    from packaging.version import Version

    import saiverse

    assert Version(saiverse.__version__) >= Version("0.3.0.dev4")
