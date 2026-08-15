"""Tests for the v0.3.0.dev5 legacy log import upgrade handlers.

リリース版 (v0.2.x, log.json 時代) から上がってきた環境で、世界が動き出す前に
旧ファイル形式のデータを DB へ取り込む 2 ハンドラの検証:

- city scope: cities/<slug>/buildings/<bid>/log.json → building_messages
- ai scope:   personas/<id>/conscious_log.json の pulse cursor → persona_pulse_cursor
  (生きた cursor 行がある環境では上書きしない)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    AI,
    Base,
    BuildingMessage,
    City,
    PersonaPulseCursor,
    User,
)
from saiverse.upgrade import (
    _load_default_handlers,
    _run_handlers_for_entity,
    parse_version,
    select_handlers,
)
from saiverse.upgrade_handlers import (
    _v0_3_0_dev5_building_log_import,
    _v0_3_0_dev5_conscious_log_cursor_import,
)


@pytest.fixture(autouse=True)
def _handlers_loaded() -> None:
    _load_default_handlers()


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    sess.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一時 saiverse_home。ハンドラは saiverse.data_paths.get_saiverse_home を
    呼び出し時に import するため、そちらを patch する。"""
    import saiverse.data_paths as data_paths

    monkeypatch.setattr(data_paths, "get_saiverse_home", lambda: tmp_path)
    return tmp_path


def _make_city(session: Session, slug: str, *, cityid: int = 1, version: str | None = "0.3.0.dev4") -> City:
    city = City(
        CITYID=cityid, CITY_SLUG=slug, USERID=1,
        UI_PORT=3000 + cityid, API_PORT=8000 + cityid,
    )
    session.add(city)
    session.commit()
    city.LAST_KNOWN_VERSION = version
    session.commit()
    return city


def _make_ai(session: Session, persona_id: str, *, version: str | None = "0.3.0.dev4") -> AI:
    ai = AI(AIID=persona_id, HOME_CITYID=1, AINAME=persona_id)
    session.add(ai)
    session.commit()
    ai.LAST_KNOWN_VERSION = version
    session.commit()
    return ai


def _write_building_log(home: Path, city_slug: str, building_id: str, messages: list) -> Path:
    b_dir = home / "cities" / city_slug / "buildings" / building_id
    b_dir.mkdir(parents=True, exist_ok=True)
    path = b_dir / "log.json"
    path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    return path


SAMPLE_MESSAGES = [
    {"role": "user", "content": "hello", "seq": 10, "message_id": "room1:10",
     "timestamp": "2026-05-01T10:00:00", "heard_by": []},
    {"role": "assistant", "content": "hi!", "seq": 11, "message_id": "room1:11",
     "persona_id": "p1", "timestamp": "2026-05-01T10:00:01", "heard_by": []},
]


# ---- city scope: building log import ----

def test_city_handler_imports_building_logs(session: Session, home: Path) -> None:
    city = _make_city(session, "test_city")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)

    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    rows = session.query(BuildingMessage).order_by(BuildingMessage.seq).all()
    assert [r.content for r in rows] == ["hello", "hi!"]
    assert [r.seq for r in rows] == [1, 2]
    assert [r.legacy_seq for r in rows] == [10, 11]


def test_city_handler_scopes_to_own_city(session: Session, home: Path) -> None:
    city = _make_city(session, "city_x")
    _write_building_log(home, "city_x", "room_x", SAMPLE_MESSAGES[:1])
    _write_building_log(home, "city_y", "room_y", SAMPLE_MESSAGES[1:])

    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    ids = {r.building_id for r in session.query(BuildingMessage).all()}
    assert ids == {"room_x"}


def test_city_handler_idempotent(session: Session, home: Path) -> None:
    city = _make_city(session, "test_city")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)

    for _ in range(2):
        _v0_3_0_dev5_building_log_import(session=session, city=city)
        session.commit()

    assert session.query(BuildingMessage).count() == 2


def test_city_handler_ignores_corrupted_marker(session: Session, home: Path) -> None:
    """隔離マーカーが残っていても現物が健全なら取り込む (テスタロッサの部屋の教訓)。"""
    city = _make_city(session, "test_city")
    path = _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)
    (path.parent / "log.json.corrupted_20260426_213015").write_text("junk", encoding="utf-8")

    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    assert session.query(BuildingMessage).count() == 2


def test_city_handler_survives_unreadable_file(session: Session, home: Path) -> None:
    """壊れた log.json があってもハンドラは例外にせず他の部屋を取り込む
    (起動を止めない。漏れは起動時の検算がアラートにする)。"""
    city = _make_city(session, "test_city")
    b_dir = home / "cities" / "test_city" / "buildings" / "broken"
    b_dir.mkdir(parents=True)
    (b_dir / "log.json").write_text("{broken", encoding="utf-8")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)

    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    ids = {r.building_id for r in session.query(BuildingMessage).all()}
    assert ids == {"room1"}


# ---- ai scope: conscious_log cursor import ----

def _write_conscious_log(home: Path, persona_id: str, payload: dict) -> None:
    p_dir = home / "personas" / persona_id
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "conscious_log.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_ai_handler_imports_cursors_after_building_import(session: Session, home: Path) -> None:
    """City ハンドラ (building log) → AI ハンドラ (cursor) の順で、seq 形式の
    旧 cursor が legacy_seq 経由で新 seq にリマップされる。"""
    city = _make_city(session, "test_city")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)
    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    ai = _make_ai(session, "p1")
    _write_conscious_log(home, "p1", {
        "pulse_cursors": {"room1": 10},
        "pulse_cursor_format": "seq",
    })

    _v0_3_0_dev5_conscious_log_cursor_import(session=session, ai=ai)
    session.commit()

    row = session.query(PersonaPulseCursor).filter_by(
        PERSONA_ID="p1", BUILDING_ID="room1"
    ).one()
    # 旧 seq=10 の行は新 seq=1 に採番されている
    assert row.CURSOR_SEQ == 1


def test_ai_handler_does_not_clobber_live_cursor_rows(session: Session, home: Path) -> None:
    """生きた cursor 行がある環境 (= DB 移行後も稼働してきた環境) では、古い
    ファイルのリマップ値で上書きしない。"""
    ai = _make_ai(session, "p1")
    session.add(PersonaPulseCursor(
        PERSONA_ID="p1", BUILDING_ID="room1", CURSOR_SEQ=42, ENTRY_MARKER_SEQ=42,
    ))
    session.commit()
    _write_conscious_log(home, "p1", {
        "pulse_cursors": {"room1": 3},
        "pulse_cursor_format": "count",
    })

    _v0_3_0_dev5_conscious_log_cursor_import(session=session, ai=ai)
    session.commit()

    row = session.query(PersonaPulseCursor).filter_by(
        PERSONA_ID="p1", BUILDING_ID="room1"
    ).one()
    assert row.CURSOR_SEQ == 42


def test_ai_handler_noop_without_persona_dir(session: Session, home: Path) -> None:
    ai = _make_ai(session, "no_dir_persona")
    _v0_3_0_dev5_conscious_log_cursor_import(session=session, ai=ai)
    session.commit()
    assert session.query(PersonaPulseCursor).count() == 0


def test_city_handler_swallows_import_failure_and_keeps_prior_state(
    session: Session, home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取り込みが例外で落ちても、ハンドラは (1) 例外を上へ返さない = 起動を
    止めない、(2) 同一トランザクション上の前段ハンドラの未コミット変更を
    巻き戻さない。"""
    import saiverse.legacy_log_import as mod

    city = _make_city(session, "test_city")
    # 前段ハンドラの未コミット変更を模す
    prior_ai = AI(AIID="prior", HOME_CITYID=1, AINAME="prior")
    session.add(prior_ai)
    session.flush()

    def _boom(*args, **kwargs):
        raise RuntimeError("import blew up")

    monkeypatch.setattr(mod, "import_building_logs", _boom)

    _v0_3_0_dev5_building_log_import(session=session, city=city)  # raise しないこと
    session.commit()

    assert session.query(AI).filter_by(AIID="prior").count() == 1


# ---- 登録とチェーン ----

def test_handlers_registered_with_dev5_edge() -> None:
    from saiverse.upgrade_handlers import HANDLERS

    city_h = next(h for h in HANDLERS if h.name == "v0_3_0_dev5_building_log_import")
    assert city_h.scope == "city"
    assert city_h.from_version == "0.3.0.dev4"
    assert city_h.to_version == "0.3.0.dev5"

    ai_h = next(h for h in HANDLERS if h.name == "v0_3_0_dev5_conscious_log_cursor_import")
    assert ai_h.scope == "ai"
    assert ai_h.from_version == "0.3.0.dev4"
    assert ai_h.to_version == "0.3.0.dev5"


@pytest.mark.parametrize("scope,name", [
    ("city", "v0_3_0_dev5_building_log_import"),
    ("ai", "v0_3_0_dev5_conscious_log_cursor_import"),
])
def test_released_user_chain_includes_dev5_import(scope: str, name: str) -> None:
    """リリース版ユーザー (LAST_KNOWN_VERSION=NULL → 0.0.0 扱い) が現行版へ
    上がるチェーンに、本取り込みハンドラが必ず入っている。"""
    selected = select_handlers(
        scope, parse_version("0.0.0"), parse_version("0.3.0.dev5")
    )
    assert name in {h.name for h in selected}


def test_current_version_is_dev5_or_later() -> None:
    """VERSION がハンドラの to_version 以上でないと、この取り込みは誰の環境でも
    走らない (select_handlers は target 以下のエッジだけを選ぶ)。"""
    from saiverse import __version__

    assert parse_version(__version__) >= parse_version("0.3.0.dev5")


def test_city_end_to_end_stamps_version(session: Session, home: Path) -> None:
    city = _make_city(session, "test_city", version="0.3.0.dev4")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)

    ok = _run_handlers_for_entity(
        session, scope="city", entity=city, entity_id=str(city.CITYID),
        target=parse_version("0.3.0.dev5"),
    )
    assert ok is True

    session.refresh(city)
    assert city.LAST_KNOWN_VERSION == "0.3.0.dev5"
    assert session.query(BuildingMessage).count() == 2
