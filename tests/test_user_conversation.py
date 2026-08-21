"""saiverse/user_conversation.py — Track を経由しない会話経路のテスト。

芯 (track_retirement.md §2 住人 2): 会話の実体は「開いている会話の出来事 +
main_line 起動 + 沈黙タイマー」の三つで、Track はどこにも登場しない。

覆う振る舞い:
- 会話が開いていれば直接メインライン起動 + 沈黙タイマーの張り直し
- 会話が閉じていて別の活動もなければ会話開始 (出来事 open → main_line → タイマー)
- 会話が閉じていて別の活動中なら on_event 判断点へ直結 (engage_now でだけ会話開始)
- 沈黙タイマーの対象外判定 (デバッグ完全手動モード / タイムアウト 0 以下)
- タイムアウト発火で会話の出来事が閉じ、予約も解除される
- 起動時の張り直しは「会話が開いているペルソナ」だけ

2026-08-21 Codex レビューで塞いだ穴 (以下の 6 件はいずれも回帰テスト付き):
- 沈黙タイマーが仮想クロックで刻まれる (実時刻ではない)
- 取って代わられた予約の callback が新しい会話を閉じない (世代トークン)
- 同時発話で会話の出来事と Pulse が二重に作られない (ペルソナ単位のロック)
- 副作用の後の失敗を dispatcher が直接応答で肩代わりしない (二重応答の封じ)
- 新規会話の初回発話にも Pulse 起動オプションが届く
- 会話の出来事を開けなかったら応答せず送出する
"""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, Episode, User
from saiverse import clock, episodes, user_conversation as uc
from saiverse.event_scheduler import EventScheduler
from saiverse.pulse_dispatcher import PulseDispatcher

PERSONA_ID = "alice"
USER_ID = "1"


@pytest.fixture
def session_factory():
    # StaticPool + check_same_thread=False: 同時発話の再現テストが別スレッドから
    # 同じ in-memory DB を引く (既定の SingletonThreadPool だとスレッドごとに
    # 空の DB が生まれてしまう)。
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()
    yield Session
    engine.dispose()


@pytest.fixture
def manager(session_factory):
    persona = MagicMock()
    persona.persona_id = PERSONA_ID
    persona.current_building_id = "test_building"
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (発火は手で回す)
        run_sea_user=MagicMock(return_value=[]),
        _active_sse_callbacks={},
        _debug_manual_mode_personas=set(),
        _autonomy_managers={},
        user_id=1,
    )
    return mgr


def _open_conversations(mgr):
    db = mgr.SessionLocal()
    try:
        return (
            db.query(Episode)
            .filter(
                Episode.PERSONA_ID == PERSONA_ID,
                Episode.KIND == episodes.KIND_CONVERSATION,
            )
            .all()
        )
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _real_clock():
    """どのテストも実クロックで始まり、仮想モードを持ち越さない。"""
    clock.disable_virtual()
    yield
    clock.disable_virtual()


def _armed(mgr):
    entry = mgr.event_scheduler._entries_by_key.get(uc._timeout_key(PERSONA_ID))
    return entry is not None and not entry.cancelled


def _reservation(mgr):
    return mgr.event_scheduler._entries_by_key.get(uc._timeout_key(PERSONA_ID))


# ---------------------------------------------------------------------------
# on_user_utterance: 経路の分岐
# ---------------------------------------------------------------------------


def test_open_conversation_answers_directly_and_rearms_the_timeout(manager):
    """会話が開いていれば直接メインライン起動。応答後にタイマーを張り直す。"""
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ただいま"}, invoke,
    )

    invoke.assert_called_once_with()
    # 会話開始経路 (空入力の main_line) は通らない
    manager.run_sea_user.assert_not_called()
    assert _armed(manager) is True
    # 出来事は開き直されない (冪等)
    assert len(_open_conversations(manager)) == 1


def test_no_conversation_and_no_activity_starts_the_conversation(manager):
    """会話が閉じていて別の活動もなければ、判断を経ずに会話を開始する。"""
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "こんにちは"}, invoke,
    )

    # 直接応答経路は通らず、会話開始 (空入力 + auto_ingest) が走る
    invoke.assert_not_called()
    manager.run_sea_user.assert_called_once()
    args, kwargs = manager.run_sea_user.call_args
    assert args[1] == "test_building"
    assert args[2] == ""
    assert "origin_track_id" not in kwargs

    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "open"
    assert _armed(manager) is True


def test_busy_with_another_activity_fires_the_on_event_judgment(manager, monkeypatch):
    """会話が閉じていて別の活動中なら on_event 判断点へ直結する。"""
    episodes.open_episode(
        manager, PERSONA_ID, episodes.KIND_WORK_SESSION, building_id="test_building",
    )
    seen = {}

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        seen.update({"text": text, "user_id": user_id})
        return "none:judged"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )
    invoke = MagicMock()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, invoke,
    )

    assert seen == {"text": "ちょっといい？", "user_id": USER_ID}
    # 判断が engage_now を出さなければ応答しない
    invoke.assert_not_called()
    manager.run_sea_user.assert_not_called()
    assert _open_conversations(manager) == []


def test_busy_and_engage_now_starts_the_conversation(manager, monkeypatch):
    """仲裁が engage_now を選んだら、初回発火と同じ入口で会話が始まる。"""
    episodes.open_episode(
        manager, PERSONA_ID, episodes.KIND_WORK_SESSION, building_id="test_building",
    )

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        engage()
        return "judged:engage_now"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, MagicMock(),
    )

    manager.run_sea_user.assert_called_once()
    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "open"
    assert _armed(manager) is True


def test_main_line_failure_still_arms_the_timeout(manager):
    """応答が転んでも沈黙タイマーは張る (開いた会話が永久に閉じないのを防ぐ)。"""
    manager.run_sea_user.side_effect = RuntimeError("boom")

    uc.start_conversation(manager, PERSONA_ID, USER_ID)

    assert _armed(manager) is True
    assert len(_open_conversations(manager)) == 1


# ---------------------------------------------------------------------------
# 沈黙タイマー
# ---------------------------------------------------------------------------


def test_debug_manual_mode_is_excluded_from_the_timeout(manager):
    manager._debug_manual_mode_personas.add(PERSONA_ID)
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is False
    assert _armed(manager) is False


def test_zero_timeout_minutes_disables_the_timer(manager):
    db = manager.SessionLocal()
    try:
        db.query(AI).filter(AI.AIID == PERSONA_ID).update(
            {"USER_CONV_TIMEOUT_MINUTES": 0}
        )
        db.commit()
    finally:
        db.close()
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is False


def test_unloaded_persona_is_excluded_from_the_timeout(manager):
    manager.personas.pop(PERSONA_ID)
    assert uc.conversation_timeout_minutes(manager, PERSONA_ID) is None


def test_only_if_absent_keeps_the_live_reservation(manager):
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True
    first = manager.event_scheduler._entries_by_key[uc._timeout_key(PERSONA_ID)]
    assert uc.arm_conversation_timeout(manager, PERSONA_ID, only_if_absent=True) is False
    assert (
        manager.event_scheduler._entries_by_key[uc._timeout_key(PERSONA_ID)] is first
    )


def test_timeout_closes_the_conversation_and_releases_the_reservation(manager):
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    uc.arm_conversation_timeout(manager, PERSONA_ID)

    uc.handle_conversation_timeout(manager, PERSONA_ID)

    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "closed"
    assert _armed(manager) is False


def test_timeout_with_a_stale_expected_ref_closes_nothing(manager):
    """検証時に見ていた会話が既に別のものへ入れ替わっていたら閉じない。"""
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )

    uc.handle_conversation_timeout(
        manager, PERSONA_ID, expected_episode_ref="episode:999",
    )

    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "open"


# ---------------------------------------------------------------------------
# 起動時の張り直し
# ---------------------------------------------------------------------------


def test_rearm_on_load_skips_when_no_conversation_is_open(manager):
    uc.rearm_conversation_timeout_on_load(manager, PERSONA_ID)
    assert _armed(manager) is False


def test_rearm_on_load_arms_when_a_conversation_is_open(manager):
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    uc.rearm_conversation_timeout_on_load(manager, PERSONA_ID)
    assert _armed(manager) is True


# ---------------------------------------------------------------------------
# 指摘 1: 沈黙タイマーは仮想クロックで刻む
# ---------------------------------------------------------------------------


def test_timeout_is_armed_and_fires_on_the_virtual_clock(manager):
    """仮想日付のシミュレーション中でも、期限は仮想時刻基準で来て会話が閉じる。

    実時刻 (``datetime.now()``) で刻むと、期限がシミュレーション終了後へ飛んで
    開いた会話の出来事が最後まで閉じない。
    """
    start = datetime(2026, 8, 21, 9, 0, 0)
    clock.enable_virtual(start)

    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True

    expected = start + timedelta(minutes=uc.DEFAULT_CONVERSATION_TIMEOUT_MINUTES)
    assert _reservation(manager).fire_at_ts == pytest.approx(expected.timestamp())

    # まだ期限前: 何も起きない
    clock.advance_to(start + timedelta(minutes=5))
    assert manager.event_scheduler.run_due(clock.now()) == 0
    assert _open_conversations(manager)[0].STATUS == "open"

    # 仮想時刻を期限の先へ進めると発火して会話が閉じる
    clock.advance_to(expected + timedelta(minutes=1))
    assert manager.event_scheduler.run_due(clock.now()) == 1

    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "closed"
    assert _armed(manager) is False


# ---------------------------------------------------------------------------
# 指摘 2: 取って代わられた予約の callback は新しい会話を閉じない
# ---------------------------------------------------------------------------


def _detach_reservation(mgr):
    """EventScheduler が「期限到来で取り出した」状態を再現する。

    dispatch は heap と ``_entries_by_key`` の両方から外してから、ロックの外で
    callback を呼ぶ。その瞬間に居合わせた再予約は、古い callback を止められない。
    """
    key = uc._timeout_key(PERSONA_ID)
    entry = mgr.event_scheduler._entries_by_key.pop(key)
    mgr.event_scheduler._heap.remove(entry)
    return entry


def test_a_superseded_timeout_callback_does_not_close_the_new_conversation(manager):
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)

    # 走り出した古い callback の隣で、ユーザー発話が同じ key へ再予約する
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True
    fresh = _reservation(manager)

    stale.callback()

    # 古い世代は降りる: 会話は開いたまま、新しい予約も生きている
    assert _open_conversations(manager)[0].STATUS == "open"
    assert _reservation(manager) is fresh
    assert fresh.cancelled is False

    # 現行世代なら閉じる
    fresh.callback()
    assert _open_conversations(manager)[0].STATUS == "closed"


def test_rearming_the_same_conversation_also_supersedes_the_old_callback(manager):
    """同じ出来事のままタイマーを延長した場合も、古い世代は無効になる。

    出来事の参照だけを照合値にすると、この並び (会話は同じ / 予約だけ新しい) を
    区別できず、延長したはずの会話が古い callback に閉じられる。
    """
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)
    uc.arm_conversation_timeout(manager, PERSONA_ID)  # 同じ会話のまま延長

    stale.callback()

    assert _open_conversations(manager)[0].STATUS == "open"


def test_a_cancelled_reservation_cannot_fire_after_it_started(manager):
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    uc.arm_conversation_timeout(manager, PERSONA_ID)
    stale = _detach_reservation(manager)

    uc.cancel_conversation_timeout(manager, PERSONA_ID)
    stale.callback()

    assert _open_conversations(manager)[0].STATUS == "open"


def test_each_reservation_gets_a_distinct_random_token(manager):
    """世代の識別は乱数 nonce — カウンタや時刻のような再到達可能な点を持たない。"""
    seen = set()
    for _ in range(5):
        uc.arm_conversation_timeout(manager, PERSONA_ID)
        seen.add(manager._conversation_timeout_tokens[PERSONA_ID])
    assert len(seen) == 5


# ---------------------------------------------------------------------------
# 指摘 3: 同時発話でも会話の出来事と Pulse は一つだけ
# ---------------------------------------------------------------------------


def test_concurrent_start_opens_one_episode_and_one_pulse(manager):
    barrier = threading.Barrier(2)
    started = threading.Event()

    def _slow_pulse(*_args, **_kwargs):
        started.set()
        # 勝った側がロックを握ったまま応答している間に、負けた側を再検査へ回す
        threading.Event().wait(0.2)
        return []

    manager.run_sea_user = MagicMock(side_effect=_slow_pulse)
    results = {}

    def _worker(name):
        barrier.wait()
        results[name] = uc.start_conversation(manager, PERSONA_ID, USER_ID)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)

    assert started.is_set()
    convs = _open_conversations(manager)
    assert len(convs) == 1 and convs[0].STATUS == "open"
    assert manager.run_sea_user.call_count == 1
    # 勝ちが 1、相乗りが 1
    assert sorted(results.values()) == [False, True]
    assert _armed(manager) is True


def test_start_conversation_rides_along_an_already_open_conversation(manager):
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )

    assert uc.start_conversation(manager, PERSONA_ID, USER_ID) is False

    manager.run_sea_user.assert_not_called()
    assert len(_open_conversations(manager)) == 1
    # 相乗りでも会話は動いているので沈黙タイマーは張り直す
    assert _armed(manager) is True


# ---------------------------------------------------------------------------
# 指摘 5: 新規会話の初回発話にも Pulse 起動オプションが届く
# ---------------------------------------------------------------------------


def _pulse_options():
    return {
        "metadata": {"source": "chat"},
        "meta_playbook": "meta_user_custom",
        "args": {"topic": "weather"},
        "pre_spells": ["look_around"],
        "event_callback": MagicMock(name="sse_callback"),
    }


def _assert_options_forwarded(manager, options):
    args, kwargs = manager.run_sea_user.call_args
    assert args[1] == "test_building"
    assert args[2] == ""  # user_input は空 (auto_ingest が取り込む)
    assert kwargs["metadata"] == options["metadata"]
    assert kwargs["meta_playbook"] == options["meta_playbook"]
    assert kwargs["args"] == options["args"]
    assert kwargs["pre_spells"] == options["pre_spells"]
    assert kwargs["event_callback"] is options["event_callback"]


def test_first_utterance_of_a_new_conversation_keeps_the_pulse_options(manager):
    options = _pulse_options()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "はじめまして"}, MagicMock(),
        pulse_options=options,
    )

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)


def test_the_arbitration_engage_closure_keeps_the_pulse_options(manager, monkeypatch):
    """仲裁が engage_now を選んだ経路でも、初回と同じオプションが届く。"""
    episodes.open_episode(
        manager, PERSONA_ID, episodes.KIND_WORK_SESSION, building_id="test_building",
    )

    def _fake_conflict(mgr, persona_id, text, *, engage, user_id):
        engage()
        return "judged:engage_now"

    monkeypatch.setattr(
        "saiverse.autonomy_wiring.handle_user_utterance_conflict", _fake_conflict,
    )
    options = _pulse_options()

    uc.on_user_utterance(
        manager, PERSONA_ID, USER_ID, {"content": "ちょっといい？"}, MagicMock(),
        pulse_options=options,
    )

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)


def test_without_options_the_active_sse_callback_is_still_picked_up(manager):
    sse = MagicMock(name="active_sse")
    manager._active_sse_callbacks["test_building"] = sse

    uc.start_conversation(manager, PERSONA_ID, USER_ID)

    _, kwargs = manager.run_sea_user.call_args
    assert kwargs["event_callback"] is sse


def test_unknown_pulse_options_are_dropped(manager):
    uc.start_conversation(
        manager, PERSONA_ID, USER_ID,
        pulse_options={"origin_track_id": "track:1", "metadata": {"a": 1}},
    )

    _, kwargs = manager.run_sea_user.call_args
    assert "origin_track_id" not in kwargs
    assert kwargs["metadata"] == {"a": 1}


# ---------------------------------------------------------------------------
# 指摘 6: 会話の出来事を開けなかったら応答しない
# ---------------------------------------------------------------------------


def _break_episode_open(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("episode INSERT failed")

    monkeypatch.setattr("saiverse.episodes.open_conversation_episode", _boom)


def test_a_failed_episode_open_stops_the_response(manager, monkeypatch):
    _break_episode_open(monkeypatch)

    with pytest.raises(uc.UserUtteranceError) as excinfo:
        uc.start_conversation(manager, PERSONA_ID, USER_ID)

    assert excinfo.value.stage == "open_episode"
    assert excinfo.value.side_effects_done is False
    assert excinfo.value.fallback_safe is False
    manager.run_sea_user.assert_not_called()
    assert _armed(manager) is False


def test_a_failed_episode_open_propagates_through_the_utterance_entry(
    manager, monkeypatch
):
    _break_episode_open(monkeypatch)
    invoke = MagicMock()

    with pytest.raises(uc.UserUtteranceError):
        uc.on_user_utterance(
            manager, PERSONA_ID, USER_ID, {"content": "こんにちは"}, invoke,
        )

    invoke.assert_not_called()


# ---------------------------------------------------------------------------
# 指摘 4: PulseDispatcher のフォールバック境界
# ---------------------------------------------------------------------------


def _dispatcher(manager):
    return PulseDispatcher(manager)


def _dispatch(manager, invoke, **kwargs):
    _dispatcher(manager).dispatch_user_utterance(
        persona_id=PERSONA_ID,
        user_id=USER_ID,
        event={"content": "やあ"},
        invoke_main_line=invoke,
        **kwargs,
    )


def test_dispatcher_falls_back_when_the_failure_precedes_any_side_effect(
    manager, monkeypatch
):
    def _boom(*_args, **_kwargs):
        raise uc.UserUtteranceError(
            "routing blew up",
            stage="lookup_activity",
            side_effects_done=False,
            fallback_safe=True,
        )

    monkeypatch.setattr(uc, "on_user_utterance", _boom)
    invoke = MagicMock()

    _dispatch(manager, invoke)

    invoke.assert_called_once_with()


def test_dispatcher_does_not_retry_after_side_effects(manager, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise uc.UserUtteranceError(
            "the response already ran",
            stage="direct_response",
            side_effects_done=True,
            fallback_safe=False,
        )

    monkeypatch.setattr(uc, "on_user_utterance", _boom)
    invoke = MagicMock()

    with pytest.raises(uc.UserUtteranceError):
        _dispatch(manager, invoke)

    # 呼び直さない (二重応答の封じ)。失敗そのものは上へ通す。
    invoke.assert_not_called()


def test_dispatcher_surfaces_the_original_error_after_side_effects(manager):
    """応答中の失敗は、元の例外のまま上へ通す。

    握り潰すと ``handle_user_input_stream`` の LLMError 変換に届かず、画面には
    何も出ないまま応答だけが消える。
    """
    boom = RuntimeError("the model refused")
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="test_building",
    )
    # 会話が開いているので直接応答経路 — 応答本体 (invoke_main_line) が転ぶ
    invoke = MagicMock(side_effect=boom)

    with pytest.raises(RuntimeError) as excinfo:
        _dispatch(manager, invoke)

    assert excinfo.value is boom
    invoke.assert_called_once_with()  # 1 回だけ (フォールバックで 2 回目を呼ばない)


def test_dispatcher_raises_when_the_conversation_record_is_missing(
    manager, monkeypatch
):
    _break_episode_open(monkeypatch)
    invoke = MagicMock()

    with pytest.raises(uc.UserUtteranceError):
        _dispatch(manager, invoke)

    invoke.assert_not_called()


def test_dispatcher_does_not_double_answer_when_arming_fails_after_the_pulse(
    manager, monkeypatch
):
    """指摘 4 の現物: 装填だけ転んでも、同じ発話をもう一度処理しない。"""
    def _boom_arm(*_args, **_kwargs):
        raise RuntimeError("scheduler is down")

    monkeypatch.setattr(uc, "arm_conversation_timeout", _boom_arm)
    invoke = MagicMock()

    _dispatch(manager, invoke)

    # 会話開始経路 (空入力の main_line) が 1 回だけ走り、直接応答は呼ばれない
    assert manager.run_sea_user.call_count == 1
    invoke.assert_not_called()
    assert len(_open_conversations(manager)) == 1


def test_dispatcher_forwards_the_pulse_options(manager):
    options = _pulse_options()

    _dispatch(manager, MagicMock(), pulse_options=options)

    manager.run_sea_user.assert_called_once()
    _assert_options_forwarded(manager, options)
