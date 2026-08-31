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


# ---- city scope: 取り込みは毎起動の検算へ移した ----

def test_city_handler_no_longer_imports(session: Session, home: Path) -> None:
    """dev5 の City エッジは何もしない。

    取り込みは `manager/initialization.py` の毎起動の検算が引き受けた
    (`tests/test_legacy_log_startup_repair.py`)。ここに残すと、SQLite が
    最外周の SAVEPOINT の RELEASE で確定するせいで、枠組みの commit より先に
    確定してしまう。取り込みの中身の回帰は
    `tests/test_migrate_building_logs_to_db.py` が持つ。
    """
    city = _make_city(session, "test_city")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)

    _v0_3_0_dev5_building_log_import(session=session, city=city)
    session.commit()

    assert session.query(BuildingMessage).count() == 0


# ---- ai scope: conscious_log cursor import ----

def _write_conscious_log(home: Path, persona_id: str, payload: dict) -> None:
    p_dir = home / "personas" / persona_id
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "conscious_log.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_ai_handler_imports_cursors_after_building_import(session: Session, home: Path) -> None:
    """取り込み済みの部屋に対して、seq 形式の旧 cursor が新 seq にリマップされる。"""
    from saiverse.legacy_log_import import import_building_logs

    _make_city(session, "test_city")
    _write_building_log(home, "test_city", "room1", SAMPLE_MESSAGES)
    import_building_logs(session, home, city_filter="test_city")
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
    # 旧 seq=10 の行は新 seq=-2 に採番されている
    assert row.CURSOR_SEQ == -2


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
    # 取り込みはこのエッジの仕事ではない (毎起動の検算が持つ)
    assert session.query(BuildingMessage).count() == 0
