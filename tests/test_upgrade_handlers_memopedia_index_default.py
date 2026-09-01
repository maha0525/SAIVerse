"""Test for v0_3_1_memopedia_index_default_on upgrade handler.

自動想起はまだ Memopedia 索引の常時表示を置き換えられる完成度に達していない
ため、per-persona トグル ``AI.MEMOPEDIA_INDEX_ENABLED`` は v0.3.1 でデフォルト
ON へ戻した (2026-09-01 まはー裁定)。カラムデフォルトは新規行にしか効かず、
既存行は False を保持したままスキーマ差分も出ないので、本ハンドラが移行済み
ペルソナを新しい ON 既定へ揃える (dev3 の SPELL_ENABLED と同じ構図)。
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
from saiverse.upgrade_handlers import _v0_3_1_memopedia_index_default_on


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


def _make_ai(
    session: Session, persona_id: str, index_enabled: bool, version: str | None
) -> AI:
    ai = AI(
        AIID=persona_id,
        HOME_CITYID=1,
        AINAME=persona_id,
        MEMOPEDIA_INDEX_ENABLED=index_enabled,
    )
    session.add(ai)
    session.commit()
    # LAST_KNOWN_VERSION は callable column default (現行版) が INSERT 時に効くため、
    # テストで NULL / 特定バージョンを検証したい場合は INSERT 後に UPDATE で上書きする
    # (UPDATE は Python default を発火させない)。
    ai.LAST_KNOWN_VERSION = version
    session.commit()
    return ai


def test_column_default_is_on(session: Session) -> None:
    """新規ペルソナは MEMOPEDIA_INDEX_ENABLED を指定しなくても ON で作られる。"""
    ai = AI(AIID="fresh", HOME_CITYID=1, AINAME="fresh")
    session.add(ai)
    session.commit()
    session.refresh(ai)

    assert ai.MEMOPEDIA_INDEX_ENABLED is True


def test_flips_disabled_persona_on(session: Session) -> None:
    ai = _make_ai(session, "off_persona", index_enabled=False, version="0.3.0")

    _v0_3_1_memopedia_index_default_on(session=session, ai=ai)
    session.commit()

    session.refresh(ai)
    assert ai.MEMOPEDIA_INDEX_ENABLED is True


def test_keeps_enabled_persona_on(session: Session) -> None:
    ai = _make_ai(session, "on_persona", index_enabled=True, version="0.3.0")

    _v0_3_1_memopedia_index_default_on(session=session, ai=ai)
    session.commit()

    session.refresh(ai)
    assert ai.MEMOPEDIA_INDEX_ENABLED is True


def test_idempotent(session: Session) -> None:
    ai = _make_ai(session, "p", index_enabled=False, version="0.3.0")

    for _ in range(3):
        _v0_3_1_memopedia_index_default_on(session=session, ai=ai)
        session.commit()

    session.refresh(ai)
    assert ai.MEMOPEDIA_INDEX_ENABLED is True


def test_does_not_touch_other_persona(session: Session) -> None:
    ai_a = _make_ai(session, "ai_a", index_enabled=False, version="0.3.0")
    ai_b = _make_ai(session, "ai_b", index_enabled=False, version="0.3.0")

    _v0_3_1_memopedia_index_default_on(session=session, ai=ai_a)
    session.commit()

    session.refresh(ai_b)
    # 別ペルソナは touch されない (ハンドラは ai 単体を触る)
    assert ai_b.MEMOPEDIA_INDEX_ENABLED is False


def test_handler_registered_in_handlers_list() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    h = next(
        (h for h in HANDLERS if h.name == "v0_3_1_memopedia_index_default_on"),
        None,
    )
    assert h is not None
    assert h.scope == "ai"
    assert h.from_version == "0.3.0"
    assert h.to_version == "0.3.1"


def test_release_persona_is_selected_for_v0_3_1_target() -> None:
    """0.3.0 リリースのペルソナが 0.3.1 昇格で本ハンドラの対象に入る。"""
    selected = select_handlers("ai", parse_version("0.3.0"), parse_version("0.3.1"))
    assert "v0_3_1_memopedia_index_default_on" in {h.name for h in selected}


def test_v0_3_1_edges_do_not_run_on_a_v0_3_0_target() -> None:
    """0.3.1 のエッジを登録しても、アプリ版が 0.3.0 の間は選ばれない。

    VERSION はまだ 0.3.0 のまま (リリース時に上げる)。select_handlers は
    ``from < handler.to <= target`` で絞るので、target=0.3.0 では 0.3.1 の
    エッジが弾かれ、チェーン検査も 0.3.0 で閉じる。
    """
    for scope in ("city", "ai"):
        selected = select_handlers(
            scope, parse_version("0.3.0.dev6"), parse_version("0.3.0")
        )
        names = {h.name for h in selected}
        assert "v0_3_1_memopedia_index_default_on" not in names
        assert "city_noop_v0_3_1" not in names


def test_v0_3_0_persona_upgraded_end_to_end(session: Session) -> None:
    """0.3.0 の OFF 機体が 0.3.1 昇格のエンドツーエンド経路で ON になり、
    バージョンも更新される。"""
    ai = _make_ai(session, "legacy", index_enabled=False, version="0.3.0")

    ok = _run_handlers_for_entity(
        session, scope="ai", entity=ai, entity_id=ai.AIID,
        target=parse_version("0.3.1"),
    )
    assert ok is True

    session.refresh(ai)
    assert ai.MEMOPEDIA_INDEX_ENABLED is True
    assert ai.LAST_KNOWN_VERSION == "0.3.1"
