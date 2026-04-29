"""AutonomousTrackHandler unit tests (Phase C-3a).

Handler の責務:
- 自律 Track の取得 / 一覧
- v0.10 拡張属性 (post_complete_behavior=meta_judge, default_pulse_interval 等)
- on_pulse_complete (Pulse 完了時の最小処理、ログのみ)
- build_track_context (Track 切替時の SAIMemory 注入用テキスト)
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.track_handlers import AutonomousTrackHandler
from saiverse.track_handlers.autonomous_track_handler import AUTONOMOUS_TRACK_TYPE
from saiverse.track_manager import (
    STATUS_RUNNING,
    TrackManager,
)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


@pytest.fixture
def persona(session_factory):
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
def handler(tm):
    return AutonomousTrackHandler(track_manager=tm)


# ---------------------------------------------------------------------------
# v0.10 拡張属性
# ---------------------------------------------------------------------------

def test_post_complete_behavior_is_meta_judge():
    """自律 Track は連続実行型 (meta_judge)。"""
    assert AutonomousTrackHandler.post_complete_behavior == "meta_judge"


def test_default_pulse_interval_is_set():
    """default_pulse_interval が設定されている。"""
    assert AutonomousTrackHandler.default_pulse_interval > 0


def test_default_max_consecutive_pulses_is_unlimited():
    """default_max_consecutive_pulses は -1 (無制限)。"""
    assert AutonomousTrackHandler.default_max_consecutive_pulses == -1


def test_pulse_completion_notice_mentions_meta_judge():
    """完了後挙動の notice に「メタレイヤーが判断」が含まれる。"""
    assert "メタレイヤー" in AutonomousTrackHandler.pulse_completion_notice


def test_available_spells_doc_includes_track_complete():
    """自律 Track 用のスペル一覧に track_complete が含まれる (応答待ち型と違う点)。"""
    assert "track_complete" in AutonomousTrackHandler.available_spells_doc


# ---------------------------------------------------------------------------
# list_active_autonomous_tracks
# ---------------------------------------------------------------------------

def test_list_active_autonomous_tracks_empty(handler, persona):
    """自律 Track がなければ空リスト。"""
    result = handler.list_active_autonomous_tracks(persona)
    assert result == []


def test_list_active_autonomous_tracks_returns_only_running_autonomous(
    handler, tm, persona
):
    """running な自律 Track のみ返す (他種別 / 他状態は除外)。"""
    # 自律 Track 2 つ作る、1 つだけ activate
    t_active = tm.create(persona, "autonomous", title="active autonomous")
    tm.activate(t_active)
    tm.create(persona, "autonomous", title="unstarted autonomous")  # unstarted

    # 別種別の running Track (混入しないか確認)
    t_user = tm.create(persona, "user_conversation", is_persistent=True)
    tm.activate(t_user)  # これで t_active が pending に押し出される

    # 改めて t_active を再 activate (t_user が pending に)
    tm.activate(t_active)

    result = handler.list_active_autonomous_tracks(persona)
    assert len(result) == 1
    assert result[0].track_id == t_active
    assert result[0].track_type == AUTONOMOUS_TRACK_TYPE
    assert result[0].status == STATUS_RUNNING


# ---------------------------------------------------------------------------
# build_track_context
# ---------------------------------------------------------------------------

def test_build_track_context_includes_required_sections(handler, tm, persona):
    track_id = tm.create(persona, "autonomous", title="記憶整理", intent="過去の会話を整理する")
    tm.activate(track_id)
    track = tm.get(track_id)
    text = handler.build_track_context(track)
    assert "Track 切替通知" in text
    assert "autonomous" in text
    assert "記憶整理" in text
    assert "過去の会話を整理する" in text
    # 完了後挙動 notice
    assert "メタレイヤー" in text
    # スペル一覧
    assert "track_complete" in text


def test_build_track_context_handles_missing_intent(handler, tm, persona):
    """intent 未設定でも例外にならない。"""
    track_id = tm.create(persona, "autonomous", title="test")
    tm.activate(track_id)
    track = tm.get(track_id)
    text = handler.build_track_context(track)
    assert "意図未設定" in text or "intent: " in text


# ---------------------------------------------------------------------------
# on_pulse_complete (最小実装)
# ---------------------------------------------------------------------------

def test_on_pulse_complete_does_not_raise(handler, tm, persona):
    """on_pulse_complete が例外にならない (ログ記録のみの最小実装)。"""
    track_id = tm.create(persona, "autonomous")
    tm.activate(track_id)
    track = tm.get(track_id)
    # 例外が出なければ OK
    handler.on_pulse_complete(persona, track, ["some output"])
