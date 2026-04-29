"""UserConversationTrackHandler unit tests.

Handler の責務:
- 対ユーザー Track の取得 / 自動作成
- Track が running なら invoke_main_line を直接呼ぶ
- Track が running 以外なら set_alert を発火 → invoke_main_line
- Track が running に**遷移したタイミング**で Track コンテキストを SAIMemory に注入
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import AI, Base, City, User
from saiverse.track_handlers import UserConversationTrackHandler
from saiverse.track_manager import (
    STATUS_ALERT,
    STATUS_PENDING,
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
def manager_stub(persona):
    """history_manager.add_to_persona_only を mock した最小限の manager スタブ。"""
    history_manager = MagicMock()
    persona_obj = MagicMock()
    persona_obj.history_manager = history_manager
    mgr = MagicMock()
    mgr.personas = {persona: persona_obj}
    return mgr, history_manager


@pytest.fixture
def handler(tm, manager_stub):
    mgr, _hm = manager_stub
    return UserConversationTrackHandler(track_manager=tm, manager=mgr)


# ---------------------------------------------------------------------------
# get_or_create_track: tuple (track, was_newly_created) を返す
# ---------------------------------------------------------------------------

def test_first_call_creates_returns_was_newly_created_true(handler, tm, persona):
    track, was_new = handler.get_or_create_track(persona, "1")
    assert was_new is True
    assert track.status == STATUS_RUNNING
    assert track.is_persistent is True
    assert track.track_type == "user_conversation"
    assert track.output_target == "building:current"


def test_second_call_returns_was_newly_created_false(handler, persona):
    track1, was_new_1 = handler.get_or_create_track(persona, "1")
    track2, was_new_2 = handler.get_or_create_track(persona, "1")
    assert was_new_1 is True
    assert was_new_2 is False
    assert track1.track_id == track2.track_id


def test_different_user_ids_get_separate_tracks(handler, persona):
    t1, was_new_1 = handler.get_or_create_track(persona, "1")
    t2, was_new_2 = handler.get_or_create_track(persona, "2")
    assert was_new_1 is True
    assert was_new_2 is True
    assert t1.track_id != t2.track_id


# ---------------------------------------------------------------------------
# build_track_context: Track コンテキスト本文の組み立て
# ---------------------------------------------------------------------------

def test_build_track_context_includes_required_sections(handler, tm, persona):
    track, _ = handler.get_or_create_track(persona, "1")
    text = handler.build_track_context(track)
    # 切替通知
    assert "Track 切替通知" in text
    # Track の identity
    assert "user_conversation" in text
    # 完了後挙動 (pulse_completion_notice 由来)
    assert "ユーザーの返答を待つ" in text
    # 利用可能スペル (available_spells_doc 由来)
    assert "track_pause" in text
    assert "track_activate" in text


# ---------------------------------------------------------------------------
# on_user_utterance: running 経路
# ---------------------------------------------------------------------------

def test_running_track_invokes_main_line_without_alert(handler, tm, persona, manager_stub):
    """初回会話: Track 作成 → running → main line 直接 + Track コンテキスト初回注入。"""
    _mgr, history_manager = manager_stub

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "おはよう"},
        invoke_main_line=lambda: invoked.append(True),
    )
    assert invoked == [True]
    # alert observer は呼ばれない (Track が新規 running なので)
    assert alert_observer_calls == []
    # 新規作成時は Track コンテキスト注入が行われる
    history_manager.add_to_persona_only.assert_called_once()
    args, _kwargs = history_manager.add_to_persona_only.call_args
    assert args[0]["role"] == "user"
    assert "Track 切替通知" in args[0]["content"]
    assert "<system>" in args[0]["content"]


def test_subsequent_utterance_on_running_track_no_inject_no_alert(handler, tm, persona, manager_stub):
    """既存 running Track への発話: Track コンテキスト注入なし、alert なし。"""
    _mgr, history_manager = manager_stub
    handler.get_or_create_track(persona, "1")  # 1 回目で running になる
    history_manager.reset_mock()  # 1 回目の注入呼び出しをクリア

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "二回目"},
        invoke_main_line=lambda: invoked.append(True),
    )
    assert invoked == [True]
    assert alert_observer_calls == []
    # 既存 running セッション継続なので注入なし
    history_manager.add_to_persona_only.assert_not_called()


# ---------------------------------------------------------------------------
# on_user_utterance: alert 経路
# ---------------------------------------------------------------------------

def test_pending_track_triggers_alert_then_main_line(handler, tm, persona, manager_stub):
    """Track が pending → alert 遷移 + main line 起動 (MetaLayer は activate しない想定)。"""
    _mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)  # running -> pending
    history_manager.reset_mock()  # 初回注入をクリア

    alert_observer_calls = []
    tm.add_alert_observer(
        lambda pid, tid, ctx: alert_observer_calls.append((pid, tid, ctx))
    )

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda: invoked.append(True),
    )
    assert len(alert_observer_calls) == 1
    assert invoked == [True]
    # MetaLayer (= alert observer) が activate しないので Track は alert のまま
    # → running への遷移なし → コンテキスト注入なし
    assert tm.get(track.track_id).status == STATUS_ALERT
    history_manager.add_to_persona_only.assert_not_called()


def test_pending_track_with_metalayer_activating_injects_track_context(
    handler, tm, persona, manager_stub
):
    """pending → MetaLayer が activate して running になれば Track コンテキスト注入される。"""
    _mgr, history_manager = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    history_manager.reset_mock()

    # MetaLayer の代わりに、alert observer で activate を行う
    def mock_metalayer(pid, tid, ctx):
        tm.activate(tid)

    tm.add_alert_observer(mock_metalayer)

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "話しかけた"},
        invoke_main_line=lambda: None,
    )
    # MetaLayer が activate したので running になっている
    assert tm.get(track.track_id).status == STATUS_RUNNING
    # → Track コンテキスト注入が行われる
    history_manager.add_to_persona_only.assert_called_once()


def test_main_line_invoked_even_if_alert_observer_raises(handler, tm, persona, manager_stub):
    """alert observer が例外を出しても main line は起動される。"""
    _mgr, _hm = manager_stub
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)

    def bad_observer(*args):
        raise RuntimeError("boom")

    tm.add_alert_observer(bad_observer)

    invoked = []
    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "x"},
        invoke_main_line=lambda: invoked.append(True),
    )
    assert invoked == [True]


def test_alert_status_after_handler_pending_path(handler, tm, persona, manager_stub):
    """pending 経路を通った後、Track の status は alert になっている (MetaLayer 未起動時)。"""
    track, _ = handler.get_or_create_track(persona, "1")
    tm.pause(track.track_id)
    assert tm.get(track.track_id).status == STATUS_PENDING

    handler.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "x"},
        invoke_main_line=lambda: None,
    )
    assert tm.get(track.track_id).status == STATUS_ALERT


# ---------------------------------------------------------------------------
# manager 未指定でも動く (テスト容易性 / 後方互換性のため)
# ---------------------------------------------------------------------------

def test_handler_works_without_manager_just_skips_inject(tm, persona):
    """manager=None でもエラーにならず、注入だけスキップされる。"""
    h = UserConversationTrackHandler(track_manager=tm, manager=None)
    invoked = []
    h.on_user_utterance(
        persona_id=persona,
        user_id="1",
        event={"role": "user", "content": "hi"},
        invoke_main_line=lambda: invoked.append(True),
    )
    assert invoked == [True]
