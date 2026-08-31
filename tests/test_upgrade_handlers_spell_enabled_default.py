"""Test for v0_3_0_dev3_spell_enabled_default_on upgrade handler.

Spell became a core feature, so its per-persona toggle ``AI.SPELL_ENABLED``
flips to default ON in v0.3.0.dev3. The column default only affects newly
inserted rows; existing rows already hold False and produce no schema diff, so
this handler brings migrated personas up to the new ON default.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import AI, Base, City, User
from saiverse.upgrade import (
    _load_default_handlers,
    _run_handlers_for_entity,
    parse_version,
    select_handlers,
)
from saiverse.upgrade_handlers import _v0_3_0_dev3_spell_enabled_default_on


@pytest.fixture(autouse=True)
def _handlers_loaded() -> None:
    """select_handlers / _run_handlers_for_entity は upgrade.HANDLERS を見る。
    通常は run_startup_upgrade が _load_default_handlers で populate するので、
    直呼びするテストでも同じ状態を用意する (idempotent)。"""
    _load_default_handlers()


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


def _make_ai(session: Session, persona_id: str, spell_enabled: bool, version: str | None) -> AI:
    ai = AI(
        AIID=persona_id,
        HOME_CITYID=1,
        AINAME=persona_id,
        SPELL_ENABLED=spell_enabled,
    )
    session.add(ai)
    session.commit()
    # LAST_KNOWN_VERSION は callable column default (現行版) が INSERT 時に効くため、
    # テストで NULL / 特定バージョンを検証したい場合は INSERT 後に UPDATE で上書きする
    # (UPDATE は Python default を発火させない)。
    ai.LAST_KNOWN_VERSION = version
    session.commit()
    return ai


def test_flips_disabled_persona_on(session: Session) -> None:
    ai = _make_ai(session, "off_persona", spell_enabled=False, version="0.3.0.dev2")

    _v0_3_0_dev3_spell_enabled_default_on(session=session, ai=ai)
    session.commit()

    session.refresh(ai)
    assert ai.SPELL_ENABLED is True


def test_keeps_enabled_persona_on(session: Session) -> None:
    ai = _make_ai(session, "on_persona", spell_enabled=True, version="0.3.0.dev2")

    _v0_3_0_dev3_spell_enabled_default_on(session=session, ai=ai)
    session.commit()

    session.refresh(ai)
    assert ai.SPELL_ENABLED is True


def test_idempotent(session: Session) -> None:
    ai = _make_ai(session, "p", spell_enabled=False, version="0.3.0.dev2")

    for _ in range(3):
        _v0_3_0_dev3_spell_enabled_default_on(session=session, ai=ai)
        session.commit()

    session.refresh(ai)
    assert ai.SPELL_ENABLED is True


def test_does_not_touch_other_persona(session: Session) -> None:
    ai_a = _make_ai(session, "ai_a", spell_enabled=False, version="0.3.0.dev2")
    ai_b = _make_ai(session, "ai_b", spell_enabled=False, version="0.3.0.dev2")

    _v0_3_0_dev3_spell_enabled_default_on(session=session, ai=ai_a)
    session.commit()

    session.refresh(ai_b)
    # 別ペルソナは touch されない (ハンドラは ai 単体を触る)
    assert ai_b.SPELL_ENABLED is False


def test_handler_registered_in_handlers_list() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    h = next(
        (h for h in HANDLERS if h.name == "v0_3_0_dev3_spell_enabled_default_on"),
        None,
    )
    assert h is not None
    assert h.scope == "ai"
    assert h.from_version == "0.3.0.dev2"
    assert h.to_version == "0.3.0.dev3"


def test_dev2_persona_is_selected_for_dev3_target() -> None:
    """既存の dev2 ペルソナが dev3 への昇格で本ハンドラの対象に入ることを保証。

    多くの既存ペルソナは LAST_KNOWN_VERSION=0.3.0.dev2 を持つ。dev2 から dev3 に
    上げる際、select_handlers が本ハンドラを拾わないと既存機体が ON にならない。
    """
    target = parse_version("0.3.0.dev3")
    from_v = parse_version("0.3.0.dev2")
    selected = select_handlers("ai", from_v, target)
    names = {h.name for h in selected}
    assert "v0_3_0_dev3_spell_enabled_default_on" in names


def test_new_persona_is_stamped_with_current_version(session: Session) -> None:
    """新規作成ペルソナ (LAST_KNOWN_VERSION 未指定) は作成時点で現行版が刻まれ、
    NULL のまま残らない。これにより次回起動でアップグレード通知を受けない。"""
    from saiverse import __version__

    ai = AI(AIID="fresh", HOME_CITYID=1, AINAME="fresh")
    session.add(ai)
    session.commit()
    session.refresh(ai)

    assert ai.LAST_KNOWN_VERSION == __version__


def test_stamped_new_persona_runs_no_handlers(session: Session) -> None:
    """現行版が刻まれた新規ペルソナは startup upgrade が no-op になり、
    ハンドラ (dev0 の SAIMemory 通知を含む) が一切走らない。"""
    ai = AI(AIID="fresh2", HOME_CITYID=1, AINAME="fresh2")
    session.add(ai)
    session.commit()

    from saiverse import __version__
    target = parse_version(__version__)
    current = parse_version(ai.LAST_KNOWN_VERSION)
    # 作成時点で現行版が刻まれているので昇格対象のハンドラは無い
    assert select_handlers("ai", current, target) == []


def test_null_version_persona_upgraded_end_to_end(session: Session) -> None:
    """LAST_KNOWN_VERSION=NULL (version-aware 以前) の OFF 機体が、dev3 昇格の
    エンドツーエンド経路で SPELL_ENABLED=True になり、バージョンも更新される。"""
    ai = _make_ai(session, "legacy", spell_enabled=False, version=None)
    target = parse_version("0.3.0.dev3")

    ok = _run_handlers_for_entity(
        session, scope="ai", entity=ai, entity_id=ai.AIID, target=target
    )
    assert ok is True

    session.refresh(ai)
    assert ai.SPELL_ENABLED is True
    assert ai.LAST_KNOWN_VERSION == "0.3.0.dev3"
