"""MetaLayer (判断 Pulse の共有基盤) のテスト。

v1 メタ判断 (状況分類 → meta_judgment_* Playbook dispatch) は Track 撤廃の
順序①で退役した (track_retirement.md §7.4)。残る表面は 3 つ:

- per-persona Lock (`_get_lock`) — 判断点の直列化で共用
- 判断設定 (`_load_judgment_config`) — META_JUDGMENT_CONFIG の読み出しと型検査
- 判断ログ (`_record_judgment_log`) — meta_judgment_log への書き込み
"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, MetaJudgmentLog, User
from saiverse.meta_layer import MetaLayer

PERSONA_ID = "persona-p1"


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _seed_ai(session_factory, config_json=None) -> None:
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(
            AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice",
            META_JUDGMENT_CONFIG=config_json,
        ))
        db.commit()
    finally:
        db.close()


def _make_layer(session_factory=None):
    manager = SimpleNamespace(personas={})
    if session_factory is not None:
        manager.SessionLocal = session_factory
    return MetaLayer(manager)


# ---------------------------------------------------------------------------
# per-persona Lock
# ---------------------------------------------------------------------------


def test_locks_are_per_persona_and_stable():
    layer = _make_layer()
    lock_a1 = layer._get_lock("alice")
    lock_a2 = layer._get_lock("alice")
    lock_b = layer._get_lock("bob")
    assert lock_a1 is lock_a2  # 同一ペルソナは同一 Lock
    assert lock_a1 is not lock_b  # 別ペルソナは独立 (並行できる)


# ---------------------------------------------------------------------------
# _load_judgment_config
# ---------------------------------------------------------------------------


def test_config_defaults_when_column_null(session_factory):
    _seed_ai(session_factory, config_json=None)
    layer = _make_layer(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    config = layer._load_judgment_config(persona)
    assert config == MetaLayer._DEFAULT_JUDGMENT_CONFIG


def test_config_override_merges_and_keeps_defaults(session_factory):
    _seed_ai(session_factory, config_json=json.dumps(
        {"periodic_interval_minutes": 10, "keep_cache_alive": False}
    ))
    layer = _make_layer(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    config = layer._load_judgment_config(persona)
    assert config["periodic_interval_minutes"] == 10
    assert config["keep_cache_alive"] is False
    # 触っていないキーは既定値のまま
    assert config["autonomous_pulse_interval_seconds"] == (
        MetaLayer._DEFAULT_JUDGMENT_CONFIG["autonomous_pulse_interval_seconds"]
    )


def test_config_wrong_type_falls_back_to_default(session_factory):
    # bool は int の subclass — int キーに bool が来ても既定値へ落とす型検査
    _seed_ai(session_factory, config_json=json.dumps(
        {"periodic_interval_minutes": True, "keep_cache_alive": "yes"}
    ))
    layer = _make_layer(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    config = layer._load_judgment_config(persona)
    assert config["periodic_interval_minutes"] == (
        MetaLayer._DEFAULT_JUDGMENT_CONFIG["periodic_interval_minutes"]
    )
    assert config["keep_cache_alive"] is True


def test_config_invalid_json_falls_back(session_factory):
    _seed_ai(session_factory, config_json="{not json")
    layer = _make_layer(session_factory)
    persona = SimpleNamespace(persona_id=PERSONA_ID)
    assert layer._load_judgment_config(persona) == MetaLayer._DEFAULT_JUDGMENT_CONFIG


# ---------------------------------------------------------------------------
# _record_judgment_log
# ---------------------------------------------------------------------------


def _read_logs(session_factory):
    db = session_factory()
    try:
        return db.query(MetaJudgmentLog).all()
    finally:
        db.close()


def test_record_judgment_log_writes_row(session_factory):
    _seed_ai(session_factory)
    layer = _make_layer(session_factory)
    layer._record_judgment_log(
        persona_id=PERSONA_ID,
        trigger_type="judgment_point",
        trigger_context='{"trigger": "day_open"}',
        track_at_judgment_id=None,
        thought_parts=["朝の判断。"],
        spells=[{"name": "x", "args": {}}],
        committed_to_main_cache=True,
    )
    rows = _read_logs(session_factory)
    assert len(rows) == 1
    assert rows[0].persona_id == PERSONA_ID
    assert rows[0].judgment_thought == "朝の判断。"
    assert json.loads(rows[0].spells_emitted) == [{"name": "x", "args": {}}]
    assert rows[0].committed_to_main_cache is True


def test_record_judgment_log_skips_empty(session_factory):
    _seed_ai(session_factory)
    layer = _make_layer(session_factory)
    layer._record_judgment_log(
        persona_id=PERSONA_ID,
        trigger_type="judgment_point",
        trigger_context=None,
        track_at_judgment_id=None,
        thought_parts=["", "   "],
        spells=[],
        committed_to_main_cache=False,
    )
    assert _read_logs(session_factory) == []
