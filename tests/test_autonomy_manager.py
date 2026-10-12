"""AutonomyManager — ティックの運転と見張りのテスト (v0.4 計画 段 3)。

設計: docs/intent/autonomous_behavior_v04_plan.md §4 決定 10〜12・§5 段 3・§8、
docs/intent/autonomous_behavior_v3.md §5。

ここで固定すること:

- 見張り (watchdog): start で即時に一度走り、以後は固定間隔で張り直す。stop で
  見張りとティックの予約が両方消え、走行中の見張りが止めた後に予約を生き返らせない
- ティックの予約: ライフ開始の節目が now + T に張り、終了の節目が消す
- 発火時: 先に次回を now + T で張り直してからゲートを見る — 会話が開いていれば
  fire_tick を呼ばず予約だけ更新、ライフ外・自律 OFF・グローバル全停止なら予約を消す
- 引き当て: 期限の近い (過ぎた / 次のティックまでに来る) 一件 → システムタスク →
  自由、の順。割り当て文は確定情報の文
- T の解決: 未設定 30 / 床 10 / 均等モードは 50 上限 / 自由モードは上限なし
- 最後の標準呼び出しの押し直し (notify_standard_call): 予約があるときだけ動き、
  無いときは生やさない
- 走りきったティックだけがライフの帳簿 (used_pulses) に積まれる
- 仮想クロック + run_due で「ライフ開始 → T ごとにティック → ライフ終了で止まる」
  一日の早回しが回る (fire_tick は偽物)

DB は sqlite のメモリ。EventScheduler は start しない (run_due で同期駆動)。LLM は
呼ばない。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, PersonaSchedule, User
from saiverse import autonomy_wiring as wiring
from saiverse import clock, day_plan, task_book
from saiverse import user_conversation as uc
from saiverse.autonomy_manager import (
    WATCHDOG_INTERVAL_MINUTES,
    AutonomyManager,
    AutonomyState,
    TaskBookUnreadable,
    build_tick_assignment,
    tick_reservation_key,
    watchdog_reservation_key,
)
from saiverse.event_scheduler import EventScheduler
from saiverse.execution_ledger import ExecutionLedger
from saiverse.execution_ledger_wiring import (
    TARGET_SAIMEMORY_APPEND,
    _make_saimemory_append_handler,
)

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _reset_clock():
    yield
    clock.disable_virtual()


class FakeAdapter:
    """ライフ境界の通知の届け先 (記録のみ)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)

    def append_ledger_message(
        self, message, *, execution_id, outbox_id, building_id=None,
        thread_suffix=None,
    ):
        self.messages.append(message)
        return f"msg-{outbox_id}"


class FakeTicks:
    """manager.fire_tick の偽物。呼ばれた時刻と割り当てを記録する。

    ``standard_call=True`` なら、本物のティックと同じく本人の標準モデルの
    呼び出しが成功したことを AutonomyManager に知らせる (touch の hook 相当)。
    """

    def __init__(self, manager, outcome=None, standard_call=True):
        self.manager = manager
        self.calls: List[Dict[str, Any]] = []
        self.outcome = outcome or {
            "action": "execute", "runtime_outcome": "completed",
            "error": None, "outputs": [],
        }
        self.standard_call = standard_call

    def __call__(self, persona_id, assignment_text=None):
        self.calls.append({
            "at": clock.now(), "persona_id": persona_id,
            "assignment": assignment_text,
        })
        if self.standard_call:
            am = self.manager._autonomy_managers.get(persona_id)
            if am is not None:
                am.notify_standard_call()
        return dict(self.outcome)


@pytest.fixture
def manager(session_factory):
    db = session_factory()
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

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        autonomy_enabled=True,
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=FakeAdapter(),
        model=None,  # provider 不明 → 自由モード (均等の上限が掛からない)
    )
    ledger = ExecutionLedger(session_factory=session_factory)
    dispatched: List[str] = []
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        all_personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (run_due で同期駆動)
        buildings=[],
        sea_runtime=None,
        execution_ledger=ledger,
        _autonomy_managers={},
        pulse_dispatcher=SimpleNamespace(
            dispatch_autonomy_tick=lambda pid, context=None: dispatched.append(pid),
        ),
    )
    mgr.dispatched = dispatched
    ledger.register_outbox_handler(
        TARGET_SAIMEMORY_APPEND, _make_saimemory_append_handler(mgr),
    )
    mgr.fire_tick = FakeTicks(mgr)
    return mgr


def _add_day_schedule(session_factory, playbook_name, time_of_day, params=None):
    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK=playbook_name, ENABLED=True, TIME_OF_DAY=time_of_day,
            PLAYBOOK_PARAMS=json.dumps(params) if params is not None else None,
        ))
        db.commit()
    finally:
        db.close()


def _set_life_window(session_factory, wake="07:00", close="22:00", params=None):
    _add_day_schedule(session_factory, wiring.LIFE_START_SCHEDULE_PLAYBOOK, wake, params)
    _add_day_schedule(session_factory, wiring.LIFE_END_SCHEDULE_PLAYBOOK, close)


def _mark_self_defined(session_factory, value='{"purpose": "live"}'):
    """旧 LIFE_PURPOSE 列を書く (v2 時代に聞き取りを済ませたペルソナ)。"""
    db = session_factory()
    try:
        db.query(AI).filter(AI.AIID == PERSONA_ID).update({AI.LIFE_PURPOSE: value})
        db.commit()
    finally:
        db.close()


def _start_am(manager) -> AutonomyManager:
    am = AutonomyManager(PERSONA_ID, manager)
    manager._autonomy_managers[PERSONA_ID] = am
    assert am.start() is True
    return am


def _start_life(manager):
    """起床の帳簿処理 (ライフ確定 + 開始の節目)。"""
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START,
    ) is True


def _end_life(manager):
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_END,
    ) is True


def _tick_at(manager) -> Optional[datetime]:
    return manager.event_scheduler.fire_time_of(tick_reservation_key(PERSONA_ID))


def _fire_tick_reservation(manager) -> None:
    """ティックの予約の時刻まで時計を進めて発火させる。"""
    at = _tick_at(manager)
    assert at is not None
    clock.advance_to(at)
    manager.event_scheduler.run_due(clock.now())


# ---------------------------------------------------------------------------
# 起動・停止と見張り
# ---------------------------------------------------------------------------


def test_start_runs_the_watchdog_at_once_and_then_every_interval(manager):
    clock.enable_virtual(BASE + timedelta(hours=3))
    am = _start_am(manager)
    assert am.state == AutonomyState.RUNNING
    assert am.start() is False  # 二重起動しない

    assert manager.event_scheduler.run_due(clock.now()) == 1
    assert manager.dispatched == [PERSONA_ID]
    # 次は固定間隔で張り直されている
    assert manager.event_scheduler.fire_time_of(
        watchdog_reservation_key(PERSONA_ID),
    ) == clock.now() + timedelta(minutes=WATCHDOG_INTERVAL_MINUTES)
    # 見張りはティックの予約を勝手に張らない (ライフの外)
    assert _tick_at(manager) is None


def test_stop_cancels_both_reservations_and_a_running_watchdog_does_not_revive(manager):
    clock.enable_virtual(BASE + timedelta(hours=3))
    am = _start_am(manager)
    manager.event_scheduler.schedule(
        fire_at=clock.now() + timedelta(minutes=30), callback=lambda: None,
        key=tick_reservation_key(PERSONA_ID),
    )
    # 見張りの最中に止められた場合: 見張りが終わっても張り直さない
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_autonomy_tick=lambda pid, context=None: am.stop(),
    )
    manager.event_scheduler.run_due(clock.now())
    assert am.state == AutonomyState.STOPPED
    assert not manager.event_scheduler.has_key(watchdog_reservation_key(PERSONA_ID))
    assert not manager.event_scheduler.has_key(tick_reservation_key(PERSONA_ID))
    assert am.stop() is False


def test_watchdog_error_is_recorded_and_the_watchdog_keeps_going(manager):
    clock.enable_virtual(BASE + timedelta(hours=3))

    def _boom(pid, context=None):
        raise RuntimeError("boom")

    manager.pulse_dispatcher = SimpleNamespace(dispatch_autonomy_tick=_boom)
    am = _start_am(manager)
    manager.event_scheduler.run_due(clock.now())
    assert am.last_report.kind == "watchdog"
    assert am.last_report.status == "error"
    assert "boom" in am.last_report.error
    assert manager.event_scheduler.has_key(watchdog_reservation_key(PERSONA_ID))


# ---------------------------------------------------------------------------
# ライフの節目がティックの予約を張る・消す
# ---------------------------------------------------------------------------


def test_life_start_arms_the_tick_at_now_plus_t_and_life_end_cancels_it(
    manager, session_factory,
):
    _set_life_window(session_factory, params={"tick_interval_minutes": 20})
    clock.enable_virtual(BASE + timedelta(hours=7))
    _start_am(manager)

    _start_life(manager)
    assert _tick_at(manager) == clock.now() + timedelta(minutes=20)

    clock.advance_to(BASE + timedelta(hours=22))
    _end_life(manager)
    assert _tick_at(manager) is None


def test_life_end_cancels_the_tick_even_without_an_autonomy_manager(
    manager, session_factory,
):
    """終了の節目は、誰が張った予約でも残さない (AutonomyManager が居なくても
    予約 key で直接消す)。"""
    _set_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    _start_life(manager)  # AutonomyManager 無し → 張らない
    assert _tick_at(manager) is None
    manager.event_scheduler.schedule(
        fire_at=clock.now() + timedelta(minutes=30), callback=lambda: None,
        key=tick_reservation_key(PERSONA_ID),
    )
    clock.advance_to(BASE + timedelta(hours=22))
    _end_life(manager)
    assert _tick_at(manager) is None


def test_watchdog_restores_a_missing_tick_for_a_running_life(manager, session_factory):
    """再起動からの回復 (段 3-7): 走っているライフに予約が無ければ見張りが張る。
    生きている予約は上書きしない。"""
    _set_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    _start_life(manager)  # まだ AutonomyManager が居ない (起動前に相当)
    assert _tick_at(manager) is None

    clock.advance_to(BASE + timedelta(hours=9))
    _start_am(manager)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none", "tick_armed": True}
    armed_at = _tick_at(manager)
    assert armed_at == clock.now() + timedelta(minutes=30)

    clock.advance_to(BASE + timedelta(hours=9, minutes=5))
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none", "tick_armed": False}
    assert _tick_at(manager) == armed_at  # 上書きしない


# ---------------------------------------------------------------------------
# 発火時のゲート
# ---------------------------------------------------------------------------


def _running_life(manager, session_factory, params=None):
    _set_life_window(session_factory, params=params)
    clock.enable_virtual(BASE + timedelta(hours=7))
    am = _start_am(manager)
    _start_life(manager)
    return am


def test_tick_fires_with_the_free_default_when_nothing_is_assigned(
    manager, session_factory,
):
    # 聞き取り済みのペルソナ (自己定義のシステムタスクが積まれない) — 引き当て
    # るものが本当に何も無い回
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)
    _fire_tick_reservation(manager)

    assert [c["assignment"] for c in manager.fire_tick.calls] == [None]
    assert am.last_report.status == "completed"
    assert am.last_report.reason == "free"
    # 走りきったティックだけが帳簿に積まれる
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["used_pulses"] == 1
    # 次回は now + T (ティック自身の標準呼び出しが押し直す)
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_tick_that_did_not_complete_is_not_counted(manager, session_factory):
    am = _running_life(manager, session_factory)
    manager.fire_tick = FakeTicks(
        manager, outcome={"action": "skipped", "runtime_outcome": None,
                          "error": None, "outputs": None},
        standard_call=False,
    )
    _fire_tick_reservation(manager)
    assert am.last_report.status == "not_completed"
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["used_pulses"] == 0
    # 連鎖は切れない (発火の頭で張り直している)
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_tick_during_an_open_conversation_only_reschedules(manager, session_factory):
    am = _running_life(manager, session_factory)
    uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room", participants=["user"],
    )
    _fire_tick_reservation(manager)

    assert manager.fire_tick.calls == []
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "in conversation"
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_tick_outside_the_life_cancels_the_reservation(manager, session_factory):
    am = _running_life(manager, session_factory)
    # 終了の節目が取りこぼされたまま、ライフの窓の外で予約が発火した
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(hours=23), callback=am._handle_tick,
        key=tick_reservation_key(PERSONA_ID),
    )
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "outside life"
    assert _tick_at(manager) is None


def test_tick_under_the_global_all_stop_cancels_the_reservation(
    manager, session_factory,
):
    am = _running_life(manager, session_factory)
    manager._autonomy_all_stop = True  # 設定の口を通さず、門だけを閉じた状態
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "autonomy all-stop"
    assert _tick_at(manager) is None


def test_tick_with_autonomy_off_cancels_the_reservation(manager, session_factory):
    am = _running_life(manager, session_factory)
    manager.personas[PERSONA_ID].autonomy_enabled = False
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "autonomy off"
    assert _tick_at(manager) is None


def test_set_autonomy_all_stop_persists_and_releases_without_touching_personas(
    manager, session_factory,
):
    """全停止は DB とキャッシュの両方に書き、ペルソナごとの設定は書き換えない。"""
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is True
    assert wiring.set_autonomy_all_stop(manager, True) is True
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is False
    assert manager.personas[PERSONA_ID].autonomy_enabled is True
    # 起動時の読み込み (キャッシュを捨てて DB から) でも ON のまま
    del manager._autonomy_all_stop
    assert wiring.is_autonomy_all_stopped(manager) is True

    wiring.set_autonomy_all_stop(manager, False)
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is True
    del manager._autonomy_all_stop
    assert wiring.load_autonomy_all_stop(manager) is False


def test_set_autonomy_all_stop_resyncs_every_autonomy_manager(manager):
    synced: List[str] = []
    manager.ensure_autonomy_for = synced.append
    wiring.set_autonomy_all_stop(manager, True)
    assert synced == [PERSONA_ID]


# ---------------------------------------------------------------------------
# 引き当て: 締め切り → システムタスク → 自由
# ---------------------------------------------------------------------------


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def test_assignment_takes_the_deadline_first_then_system_task_then_free(manager):
    now = BASE + timedelta(hours=10)
    horizon = now + timedelta(minutes=30)
    clock.enable_virtual(now)
    assert build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon) is None

    task_book.add_entry(
        manager, PERSONA_ID, "自己定義の聞き取りに答える", origin=task_book.ORIGIN_SYSTEM,
    )
    text = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert text == "この時間は、システムからの依頼「自己定義の聞き取りに答える」に使います。"

    # 期限が次のティックより先の一件は引かない (システムタスクのまま)
    task_book.add_entry(
        manager, PERSONA_ID, "挿絵を仕上げる", origin="user", counterpart="user",
        due_at=_epoch(now + timedelta(hours=3)),
    )
    assert "システムからの依頼" in build_tick_assignment(
        manager, PERSONA_ID, now=now, horizon=horizon,
    )

    # 次のティックまでに期限が来る一件は、システムタスクより先に引く
    task_book.add_entry(
        manager, PERSONA_ID, "日記の返事を書く", origin="user", counterpart="user",
        due_at=_epoch(now + timedelta(minutes=20)),
    )
    text = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert text == (
        "この時間は、タスク帳の一件「日記の返事を書く」に使います。"
        "期限は 2026-07-04 10:20 です。"
    )

    # 期限を過ぎた一件が最優先 (最も期限の早い一件)
    task_book.add_entry(
        manager, PERSONA_ID, "請求書をまとめる", origin="user", counterpart="user",
        due_at=_epoch(now - timedelta(hours=1)),
    )
    text = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert text == (
        "この時間は、タスク帳の一件「請求書をまとめる」に使います。"
        "期限の 2026-07-04 09:00 はもう過ぎています。"
    )


def test_assignment_reaches_fire_tick(manager, session_factory):
    # 聞き取り済み — 自己定義のシステムタスクと作成時刻が同じ秒で並ばないように
    _mark_self_defined(session_factory)
    _running_life(manager, session_factory)
    task_book.add_entry(
        manager, PERSONA_ID, "自己定義の聞き取りに答える", origin=task_book.ORIGIN_SYSTEM,
    )
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        "この時間は、システムからの依頼「自己定義の聞き取りに答える」に使います。"
    )


def test_unreadable_task_book_skips_the_tick_instead_of_running_free(
    manager, session_factory, monkeypatch,
):
    """タスク帳が読めない回は自由時間にすり替えない (締め切り優先の保証を
    黙って外さない)。予約は残る。"""
    am = _running_life(manager, session_factory)

    def _broken(mgr, pid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(task_book, "list_open_with_due", _broken)
    with pytest.raises(TaskBookUnreadable):
        build_tick_assignment(
            manager, PERSONA_ID, now=clock.now(), horizon=clock.now(),
        )
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "task book unreadable"
    assert _tick_at(manager) is not None


# ---------------------------------------------------------------------------
# システムタスク第一号: 自己定義の聞き取り (v3 §9-5、段 3-6)
# ---------------------------------------------------------------------------


def _self_definition_tasks(manager):
    key = day_plan.self_definition_task_idem_key(PERSONA_ID)
    return [
        e for e in task_book.list_open_system_tasks(manager, PERSONA_ID)
        if e["idem_key"] == key
    ]


def test_life_start_queues_the_self_definition_task_once(manager, session_factory):
    """LIFE_PURPOSE が空のペルソナは、ライフ開始で自己定義の聞き取りが一件積まれ、
    最初のティックがそれを引く。"""
    _running_life(manager, session_factory)
    entries = _self_definition_tasks(manager)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["origin"] == task_book.ORIGIN_SYSTEM
    assert entry["due_at"] is None and entry["counterpart"] is None
    assert entry["content"] == day_plan.SELF_DEFINITION_TASK_CONTENT

    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        f"この時間は、システムからの依頼「{day_plan.SELF_DEFINITION_TASK_CONTENT}」に使います。"
    )


def test_self_definition_task_is_not_queued_again_on_the_next_life_start(
    manager, session_factory,
):
    """二度目の節目 (翌日のライフ開始) でも、完了後でも、積み直されない。"""
    _running_life(manager, session_factory)
    assert len(_self_definition_tasks(manager)) == 1
    _end_life(manager)

    # 翌日のライフ開始 — まだ open のまま: 一件のまま
    clock.advance_to(BASE + timedelta(days=1, hours=7))
    _start_life(manager)
    assert len(_self_definition_tasks(manager)) == 1

    # 本人が済ませた後のライフ開始 — 閉じた行は積み直さない
    task_id = _self_definition_tasks(manager)[0]["task_id"]
    task_book.complete_entry(manager, PERSONA_ID, task_id, outcome="書いた")
    _end_life(manager)
    clock.advance_to(BASE + timedelta(days=2, hours=7))
    _start_life(manager)
    assert _self_definition_tasks(manager) == []
    db = session_factory()
    try:
        from database.models import TaskBookEntry

        assert db.query(TaskBookEntry).filter(
            TaskBookEntry.IDEM_KEY == day_plan.self_definition_task_idem_key(PERSONA_ID),
        ).count() == 1
    finally:
        db.close()


def test_self_definition_task_skips_a_persona_with_life_purpose(
    manager, session_factory,
):
    """v2 時代に聞き取りを済ませたペルソナ (LIFE_PURPOSE あり) には積まない。"""
    _mark_self_defined(session_factory)
    _running_life(manager, session_factory)
    assert task_book.list_open_system_tasks(manager, PERSONA_ID) == []


def test_self_definition_failure_does_not_fail_the_life_start(
    manager, session_factory, monkeypatch, caplog,
):
    """積み込みの失敗は節目を失敗にしない (WARN で握る)。"""
    def _boom(*args, **kwargs):
        raise RuntimeError("task book down")

    monkeypatch.setattr(task_book, "add_system_task", _boom)
    _set_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    _start_am(manager)
    with caplog.at_level("WARNING"):
        _start_life(manager)  # True を返す (内部で assert)
    assert any("self-definition" in r.message for r in caplog.records)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True
    assert _tick_at(manager) is not None


# ---------------------------------------------------------------------------
# T の解決
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested, mode, expected",
    [
        (None, "free", 30),       # 未設定 = 既定 30
        ("30", "free", 30),       # 不正な型 = 既定
        (True, "free", 30),
        (0, "free", 30),
        (5, "free", 10),          # 床 10 へ切り上げ
        (10, "even", 10),
        (45, "even", 45),
        (90, "even", 50),         # 均等モードは 50 へ切り下げ
        (90, "free", 90),         # 自由モードは上限なし
        (None, "even", 30),
    ],
)
def test_effective_tick_interval(requested, mode, expected):
    assert day_plan.effective_tick_interval_minutes(requested, mode) == expected


def test_resolve_tick_interval_reads_the_current_setting_each_time(
    manager, session_factory,
):
    """T はライフに焼き込まず、張るたびに設定から読む。均等モードの上限は
    走っているライフのモードで掛かる。"""
    _set_life_window(session_factory, params={"tick_interval_minutes": 90})
    clock.enable_virtual(BASE + timedelta(hours=7))
    assert day_plan.resolve_tick_interval_minutes(manager, PERSONA_ID) == 90  # 自由

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モードのライフ
    _start_life(manager)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["mode"] == "even"
    assert day_plan.resolve_tick_interval_minutes(manager, PERSONA_ID) == 50
    assert day_plan.resolve_tick_interval_minutes(
        manager, PERSONA_ID, life_mode="free",
    ) == 90


# ---------------------------------------------------------------------------
# 最後の標準呼び出しの押し直し
# ---------------------------------------------------------------------------


def test_notify_standard_call_pushes_an_armed_tick_and_never_creates_one(
    manager, session_factory,
):
    _set_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=6))
    am = _start_am(manager)
    # ライフ外 (予約なし): 標準呼び出しがあっても生やさない
    assert am.notify_standard_call() is False
    assert _tick_at(manager) is None

    clock.advance_to(BASE + timedelta(hours=7))
    _start_life(manager)
    clock.advance_to(BASE + timedelta(hours=7, minutes=12))
    assert am.notify_standard_call() is True
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)

    am.stop()
    assert am.notify_standard_call() is False


def test_get_status_reports_the_resolved_interval_and_eta(manager, session_factory):
    am = _running_life(manager, session_factory, params={"tick_interval_minutes": 15})
    status = am.get_status()
    assert status["state"] == "running"
    assert status["interval_minutes"] == 15
    assert status["tick_armed"] is True
    assert status["next_tick_eta_seconds"] == 15 * 60
    assert am.get_next_tick_eta_seconds() == 15 * 60


# ---------------------------------------------------------------------------
# 一日の早回し (仮想クロック + run_due)
# ---------------------------------------------------------------------------


def test_a_day_fast_forward_ticks_every_t_and_stops_at_life_end(
    manager, session_factory,
):
    """07:00 起床 → T=30 分ごとにティック → 10:00〜11:00 は会話 (空のティックは
    打たない) → 会話の最後の標準呼び出しから T 分で再開 → 13:00 就寝で止まる。

    起床・就寝は本番の ScheduleManager の代わりに、同じ帳簿処理を予約で撃つ。
    """
    _set_life_window(session_factory, wake="07:00", close="13:00")
    scheduler = manager.event_scheduler
    clock.enable_virtual(BASE + timedelta(hours=6, minutes=59))
    _start_am(manager)
    scheduler.schedule(
        fire_at=BASE + timedelta(hours=7), callback=lambda: _start_life(manager),
        key="test:wake",
    )
    scheduler.schedule(
        fire_at=BASE + timedelta(hours=13), callback=lambda: _end_life(manager),
        key="test:close",
    )

    def _open_conversation():
        uc._set_open_conversation(
            manager, PERSONA_ID, building_id="alice_room", participants=["user"],
        )

    def _talk():
        # 会話の応答 = 本人の標準モデルの呼び出し (ティックの予約を押し直す)
        manager._autonomy_managers[PERSONA_ID].notify_standard_call()

    def _close_conversation():
        uc.clear_open_conversation(manager, PERSONA_ID)

    scheduler.schedule(fire_at=BASE + timedelta(hours=10), callback=_open_conversation,
                       key="test:conv-open")
    for i, minute in enumerate((5, 25, 45, 58)):
        scheduler.schedule(fire_at=BASE + timedelta(hours=10, minutes=minute),
                           callback=_talk, key=f"test:talk-{i}")
    scheduler.schedule(fire_at=BASE + timedelta(hours=11), callback=_close_conversation,
                       key="test:conv-close")

    end = BASE + timedelta(hours=16)
    steps = 0
    while True:
        nxt = scheduler.next_fire_time()
        if nxt is None or nxt > end:
            break
        clock.advance_to(max(nxt, clock.now()))
        scheduler.run_due(clock.now())
        steps += 1
        assert steps < 500

    fired = [c["at"].strftime("%H:%M") for c in manager.fire_tick.calls]
    # 07:30〜09:30 は 30 分ごと。10:00 は会話中 (空のティックは打たない)。
    # 会話の最後の標準呼び出し (10:58) から 30 分 = 11:28 で再開し、以後 30 分ごと。
    # 13:00 の就寝で止まる (12:58 が最後)。
    assert fired == [
        "07:30", "08:00", "08:30", "09:00", "09:30",
        "11:28", "11:58", "12:28", "12:58",
    ]
    assert _tick_at(manager) is None
    life = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]
    assert life["used_pulses"] == len(fired)
    assert life["ended"] is True


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 1 巡目の消し込み
# ---------------------------------------------------------------------------


def test_all_stop_read_failure_is_treated_as_stopped_and_not_cached(
    manager, session_factory,
):
    """① 全停止の読み失敗は停止扱い (fail-closed)。キャッシュせず、DB が回復
    したら読み直して本当の値 (ここでは解除) に戻る。"""
    def _broken_session():
        raise RuntimeError("db is down")

    manager.SessionLocal = _broken_session
    assert wiring.load_autonomy_all_stop(manager) is None
    assert not hasattr(manager, "_autonomy_all_stop")
    assert wiring.is_autonomy_all_stopped(manager) is True
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is False
    assert not hasattr(manager, "_autonomy_all_stop")  # 読み失敗は載せない

    manager.SessionLocal = session_factory  # 障害が直った
    assert wiring.is_autonomy_all_stopped(manager) is False
    assert manager._autonomy_all_stop is False
    assert wiring.is_autonomy_on(manager, PERSONA_ID) is True


def test_all_stop_read_failure_keeps_a_saved_stop_once_the_db_recovers(
    manager, session_factory,
):
    """① 保存済みの停止は、起動時の読み失敗の後も DB の回復で効く (解除として
    永久にキャッシュされない)。"""
    wiring.set_autonomy_all_stop(manager, True)
    del manager._autonomy_all_stop  # 再起動相当
    original = manager.SessionLocal

    def _broken_session():
        raise RuntimeError("db is down")

    manager.SessionLocal = _broken_session
    assert wiring.is_autonomy_all_stopped(manager) is True
    manager.SessionLocal = original
    assert wiring.is_autonomy_all_stopped(manager) is True
    assert manager._autonomy_all_stop is True


def test_setting_updates_publish_in_commit_order(manager, session_factory):
    """② 全停止の更新は commit → 公開 → 再同期を一つのロック区間で行う —
    先に始めた更新の区間が終わるまで、後の更新の commit・公開は始まらない。"""
    import threading

    order: List[str] = []
    first_in_resync = threading.Event()
    release_first = threading.Event()

    def _ensure(persona_id):
        value = manager._autonomy_all_stop
        order.append(f"resync:{value}")
        if value is False and not first_in_resync.is_set():
            first_in_resync.set()
            release_first.wait(timeout=5)

    manager.ensure_autonomy_for = _ensure
    t1 = threading.Thread(target=wiring.set_autonomy_all_stop, args=(manager, False))
    t1.start()
    assert first_in_resync.wait(timeout=5)
    t2 = threading.Thread(target=wiring.set_autonomy_all_stop, args=(manager, True))
    t2.start()
    # 解除の区間が終わるまで、停止の commit・公開は始まらない
    t2.join(timeout=0.3)
    assert t2.is_alive()
    assert manager._autonomy_all_stop is False
    release_first.set()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["resync:False", "resync:True"]
    # DB と実効ゲートが一致している
    assert manager._autonomy_all_stop is True
    del manager._autonomy_all_stop
    assert wiring.load_autonomy_all_stop(manager) is True


def test_all_stop_after_the_gate_does_not_start_the_tick(
    manager, session_factory, monkeypatch,
):
    """② 生成開始の直前の再検査: 引き当て (タスク帳の読み) の最中に全停止が
    入ったら fire_tick を呼ばない。予約は消える。"""
    am = _running_life(manager, session_factory)
    from saiverse import autonomy_manager as am_module

    def _assign_then_stop(mgr, persona_id, *, now, horizon):
        mgr._autonomy_all_stop = True  # 引き当ての最中に全停止された
        return None

    monkeypatch.setattr(am_module, "build_tick_assignment", _assign_then_stop)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "autonomy all-stop before start"
    assert _tick_at(manager) is None


def test_conversation_opened_after_the_gate_does_not_start_the_tick(
    manager, session_factory, monkeypatch,
):
    """② 引き当ての最中に会話が開いたら fire_tick を呼ばない (予約は残る)。"""
    am = _running_life(manager, session_factory)
    from saiverse import autonomy_manager as am_module

    def _assign_then_talk(mgr, persona_id, *, now, horizon):
        uc._set_open_conversation(
            mgr, persona_id, building_id="alice_room", participants=["user"],
        )
        return None

    monkeypatch.setattr(am_module, "build_tick_assignment", _assign_then_talk)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "in conversation before start"
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_a_late_tick_does_not_roll_back_a_reservation_pushed_by_a_standard_call(
    manager, session_factory,
):
    """③ 07:30 の発火が手渡された後、07:20 の標準呼び出しが予約を 07:50 へ
    押した。遅れて走る 07:30 の発火は LLM へ進まず、07:50 のまま (巻き戻さない)。"""
    am = _running_life(manager, session_factory)
    assert _tick_at(manager) == BASE + timedelta(hours=7, minutes=30)
    clock.advance_to(BASE + timedelta(hours=7, minutes=20))
    assert am.notify_standard_call() is True
    assert _tick_at(manager) == BASE + timedelta(hours=7, minutes=50)

    # 押し直しの前に手渡されていた古い発火が、07:30 に走る
    clock.advance_to(BASE + timedelta(hours=7, minutes=30))
    am._handle_tick()
    assert manager.fire_tick.calls == []
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "superseded by a standard call"
    assert _tick_at(manager) == BASE + timedelta(hours=7, minutes=50)

    # 正しい期限の発火は普通に走る
    _fire_tick_reservation(manager)
    assert [c["at"] for c in manager.fire_tick.calls] == [
        BASE + timedelta(hours=7, minutes=50),
    ]


def test_a_standard_call_during_the_hand_off_is_not_lost(manager, session_factory):
    """④ EventScheduler が予約を辞書から外してワーカーが走るまでの間に成功した
    標準呼び出しは、予約を押せない (予約が無い) が時刻は記録され、発火した
    ワーカーが自分で見送って「その呼び出し + T」に張り直す。"""
    am = _running_life(manager, session_factory)
    key = tick_reservation_key(PERSONA_ID)
    # 07:30 の発火が手渡された (予約は辞書から消えている)
    manager.event_scheduler.cancel(key)
    clock.advance_to(BASE + timedelta(hours=7, minutes=29, seconds=50))
    assert am.notify_standard_call() is False  # 押せる予約は無い
    assert _tick_at(manager) is None

    clock.advance_to(BASE + timedelta(hours=7, minutes=30))
    am._handle_tick()
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "superseded by a standard call"
    assert _tick_at(manager) == BASE + timedelta(hours=7, minutes=59, seconds=50)


def test_a_standard_call_just_before_the_due_time_does_not_skip_the_tick(
    manager, session_factory,
):
    """③ 見送りの猶予 (5 秒): 期限が発火時刻の 5 秒以内 (時計の揺れ) なら
    見送らずに走る。次回はこの発火から T。"""
    am = _running_life(manager, session_factory)
    manager.fire_tick = FakeTicks(manager, standard_call=False)
    clock.advance_to(BASE + timedelta(hours=7, minutes=0, seconds=3))
    am.notify_standard_call()  # 07:00:03 → 期限 07:30:03 (発火の 3 秒後)
    clock.advance_to(BASE + timedelta(hours=7, minutes=30))
    am._handle_tick()
    assert len(manager.fire_tick.calls) == 1
    assert am.last_report.status == "completed"
    assert _tick_at(manager) == BASE + timedelta(hours=8)


class _RecordingLifecycle:
    def __init__(self):
        self.reconciled: List[str] = []

    def reconcile_keepalive_after_tick(self, persona_id):
        self.reconciled.append(persona_id)
        return True


def test_a_tick_that_did_not_warm_reconciles_the_keepalive(manager, session_factory):
    """⑤ 温めずに終わった回 (skip・not_completed) の後は keep-alive の見張りを
    再照合する。走りきって標準呼び出しが起きた回は再照合しない。"""
    lifecycle = _RecordingLifecycle()
    manager.sea_runtime = SimpleNamespace(session_lifecycle=lifecycle)
    am = _running_life(manager, session_factory)

    _fire_tick_reservation(manager)  # 走りきって標準呼び出しが起きた
    assert am.last_report.status == "completed"
    assert lifecycle.reconciled == []

    uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room", participants=["user"],
    )
    _fire_tick_reservation(manager)  # 会話中で見送り
    assert am.last_report.reason == "in conversation"
    assert lifecycle.reconciled == [PERSONA_ID]
    uc.clear_open_conversation(manager, PERSONA_ID)

    manager.fire_tick = FakeTicks(
        manager, outcome={"action": "execute", "runtime_outcome": "error",
                          "error": "boom", "outputs": None},
        standard_call=False,
    )
    _fire_tick_reservation(manager)  # 走ったが標準呼び出しまで届かなかった
    assert am.last_report.status == "not_completed"
    assert lifecycle.reconciled == [PERSONA_ID, PERSONA_ID]
