"""SubLineScheduler unit tests (Phase C-3b).

検証:
- 起動 / 停止 (start / stop)
- _should_trigger_next_pulse の判定 (Pulse 間隔 / 連続実行回数上限)
- _trigger_pulse が manager.run_sea_auto を track_autonomous で呼び出す
- _update_pulse_metadata で last_pulse_at / consecutive_pulse_count が更新される
- 連続実行型でない Track 種別 (user_conversation 等) は対象外
"""
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.pulse_scheduler import SubLineScheduler
from saiverse.track_handlers import AutonomousTrackHandler
from saiverse.track_manager import TrackManager


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def db_persona(session_factory):
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID="alice", HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()
    return "alice"


@pytest.fixture
def tm(session_factory):
    return TrackManager(session_factory=session_factory)


@pytest.fixture
def manager_stub(tm, db_persona):
    """SubLineScheduler が必要とする最小限の SAIVerseManager スタブ。"""
    persona = SimpleNamespace(
        persona_id=db_persona,
        current_building_id="building_1",
    )
    mgr = MagicMock()
    mgr.personas = {db_persona: persona}
    mgr.track_manager = tm
    mgr.run_sea_auto = MagicMock()
    return mgr


@pytest.fixture
def scheduler(manager_stub):
    return SubLineScheduler(manager_stub)


# ---------------------------------------------------------------------------
# ライフサイクル
# ---------------------------------------------------------------------------

def test_start_stop_cleanly(scheduler):
    """起動 / 停止が例外なく完了する。"""
    scheduler.start()
    assert scheduler._thread is not None
    assert scheduler._thread.is_alive()
    scheduler.stop()
    assert scheduler._thread is None


def test_double_start_is_safe(scheduler):
    """2 回 start() しても多重起動しない。"""
    scheduler.start()
    scheduler.start()  # 警告ログだけ、例外なし
    scheduler.stop()


# ---------------------------------------------------------------------------
# _should_trigger_next_pulse
# ---------------------------------------------------------------------------

def test_should_trigger_when_no_last_pulse(scheduler, tm, db_persona):
    """last_pulse_at が無ければ即時 trigger 可。"""
    track_id = tm.create(db_persona, "autonomous")
    tm.activate(track_id)
    track = tm.get(track_id)
    assert scheduler._should_trigger_next_pulse(track, AutonomousTrackHandler) is True


def test_should_not_trigger_within_interval(scheduler, tm, db_persona):
    """interval 内であれば trigger しない。"""
    track_id = tm.create(
        db_persona, "autonomous",
        metadata=json.dumps({
            "last_pulse_at": time.time(),  # 今ちょうど
            "pulse_interval_seconds": 30,
        }),
    )
    tm.activate(track_id)
    track = tm.get(track_id)
    assert scheduler._should_trigger_next_pulse(track, AutonomousTrackHandler) is False


def test_should_trigger_after_interval(scheduler, tm, db_persona):
    """interval 経過後は trigger 可。"""
    track_id = tm.create(
        db_persona, "autonomous",
        metadata=json.dumps({
            "last_pulse_at": time.time() - 60,  # 60 秒前
            "pulse_interval_seconds": 30,
        }),
    )
    tm.activate(track_id)
    track = tm.get(track_id)
    assert scheduler._should_trigger_next_pulse(track, AutonomousTrackHandler) is True


def test_should_not_trigger_when_max_consecutive_reached(scheduler, tm, db_persona):
    """連続実行回数上限に達したら trigger しない。"""
    track_id = tm.create(
        db_persona, "autonomous",
        metadata=json.dumps({
            "consecutive_pulse_count": 5,
            "max_consecutive_pulses": 5,
        }),
    )
    tm.activate(track_id)
    track = tm.get(track_id)
    assert scheduler._should_trigger_next_pulse(track, AutonomousTrackHandler) is False


def test_unlimited_max_consecutive_always_passes(scheduler, tm, db_persona):
    """max_consecutive_pulses=-1 (無制限) なら何回でも trigger 可。"""
    track_id = tm.create(
        db_persona, "autonomous",
        metadata=json.dumps({
            "consecutive_pulse_count": 9999,
            "max_consecutive_pulses": -1,
        }),
    )
    tm.activate(track_id)
    track = tm.get(track_id)
    assert scheduler._should_trigger_next_pulse(track, AutonomousTrackHandler) is True


# ---------------------------------------------------------------------------
# _trigger_pulse + _tick_persona
# ---------------------------------------------------------------------------

def test_tick_triggers_run_sea_auto_for_autonomous_track(
    scheduler, manager_stub, tm, db_persona
):
    """tick で running な autonomous Track があれば run_sea_auto が呼ばれる。"""
    track_id = tm.create(db_persona, "autonomous", title="記憶整理")
    tm.activate(track_id)

    scheduler._tick()

    manager_stub.run_sea_auto.assert_called_once()
    call_kwargs = manager_stub.run_sea_auto.call_args.kwargs
    assert call_kwargs["meta_playbook"] == "track_autonomous"
    assert call_kwargs["args"]["track_id"] == track_id


def test_tick_does_not_trigger_for_user_conversation_track(
    scheduler, manager_stub, tm, db_persona
):
    """user_conversation (応答待ち型) Track は対象外。"""
    track_id = tm.create(
        db_persona, "user_conversation", is_persistent=True,
    )
    tm.activate(track_id)

    scheduler._tick()

    manager_stub.run_sea_auto.assert_not_called()


def test_tick_does_not_trigger_for_unstarted_track(
    scheduler, manager_stub, tm, db_persona
):
    """unstarted な Track は対象外 (running のみ)。"""
    tm.create(db_persona, "autonomous")  # unstarted のまま

    scheduler._tick()

    manager_stub.run_sea_auto.assert_not_called()


def test_tick_updates_metadata_after_trigger(
    scheduler, manager_stub, tm, db_persona
):
    """trigger 後、Track metadata の last_pulse_at と consecutive_pulse_count が更新される。"""
    track_id = tm.create(db_persona, "autonomous")
    tm.activate(track_id)

    before = time.time()
    scheduler._tick()
    after = time.time()

    track = tm.get(track_id)
    meta = json.loads(track.track_metadata) if track.track_metadata else {}
    assert "last_pulse_at" in meta
    assert before <= float(meta["last_pulse_at"]) <= after
    assert meta["consecutive_pulse_count"] == 1


def test_tick_increments_consecutive_count(
    scheduler, manager_stub, tm, db_persona
):
    """連続 tick で consecutive_pulse_count が増えていく。"""
    track_id = tm.create(
        db_persona, "autonomous",
        metadata=json.dumps({"pulse_interval_seconds": 0}),  # 連続即時 OK
    )
    tm.activate(track_id)

    scheduler._tick()
    track = tm.get(track_id)
    meta = json.loads(track.track_metadata)
    assert meta["consecutive_pulse_count"] == 1

    # 次の tick は前回の last_pulse_at から 0 秒経過 = OK
    scheduler._tick()
    track = tm.get(track_id)
    meta = json.loads(track.track_metadata)
    assert meta["consecutive_pulse_count"] == 2


def test_tick_skips_persona_without_building(scheduler, manager_stub, tm, db_persona):
    """current_building_id が無いペルソナはスキップ (Pulse 起動しない)。"""
    manager_stub.personas[db_persona].current_building_id = None
    track_id = tm.create(db_persona, "autonomous")
    tm.activate(track_id)

    scheduler._tick()

    manager_stub.run_sea_auto.assert_not_called()


# ---------------------------------------------------------------------------
# 環境変数による起動制御 (緊急停止手段)
# ---------------------------------------------------------------------------

def test_is_subline_scheduler_enabled_default_true(monkeypatch):
    """環境変数未設定時はデフォルト true (起動する)。"""
    from saiverse.pulse_scheduler import is_subline_scheduler_enabled
    monkeypatch.delenv("SAIVERSE_SUBLINE_SCHEDULER_ENABLED", raising=False)
    assert is_subline_scheduler_enabled() is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "off", ""])
def test_is_subline_scheduler_enabled_disabled_values(monkeypatch, value):
    """false/0/no/off/空文字 はすべて無効化扱い。"""
    from saiverse.pulse_scheduler import is_subline_scheduler_enabled
    monkeypatch.setenv("SAIVERSE_SUBLINE_SCHEDULER_ENABLED", value)
    assert is_subline_scheduler_enabled() is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "anything"])
def test_is_subline_scheduler_enabled_enabled_values(monkeypatch, value):
    """false 系以外はすべて有効化扱い。"""
    from saiverse.pulse_scheduler import is_subline_scheduler_enabled
    monkeypatch.setenv("SAIVERSE_SUBLINE_SCHEDULER_ENABLED", value)
    assert is_subline_scheduler_enabled() is True
