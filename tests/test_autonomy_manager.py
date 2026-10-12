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
    ASSIGNMENT_DEADLINE,
    ASSIGNMENT_FREE,
    ASSIGNMENT_SYSTEM,
    WATCHDOG_INTERVAL_MINUTES,
    AutonomyManager,
    AutonomyState,
    TaskBookUnreadable,
    TickAssignment,
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

    本物の ``SAIVerseManager.fire_tick`` と同じく、``pre_generation_check``
    (Beat ロックの内側の再検査) を生成の前に呼び、拒否されたら生成せずに
    ``runtime_outcome="refused"`` を返す (``calls`` には積まない)。
    ``beat_wait`` は Beat ロックを待っている間に起きる出来事 (全停止・ライフ
    終了・標準呼び出しなど) を差し込むフック — 再検査の直前に一度呼ぶ。
    """

    def __init__(self, manager, outcome=None, standard_call=True, beat_wait=None):
        self.manager = manager
        self.calls: List[Dict[str, Any]] = []
        self.refused: List[str] = []
        self.outcome = outcome or {
            "action": "execute", "runtime_outcome": "completed",
            "error": None, "outputs": [],
        }
        self.standard_call = standard_call
        self.beat_wait = beat_wait

    def __call__(self, persona_id, assignment_text=None, *, pre_generation_check=None):
        if self.beat_wait is not None:
            self.beat_wait()
        if pre_generation_check is not None:
            reason = pre_generation_check()
            if reason is not None:
                self.refused.append(reason)
                return {
                    "action": "execute", "runtime_outcome": "refused",
                    "error": None, "outputs": [], "refusal": reason,
                }
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
    assert out == {"action": "none", "ttl_restored": False, "tick_armed": True}
    armed_at = _tick_at(manager)
    assert armed_at == clock.now() + timedelta(minutes=30)

    clock.advance_to(BASE + timedelta(hours=9, minutes=5))
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none", "ttl_restored": False, "tick_armed": False}
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
    free = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert free == TickAssignment(text=None, kind=ASSIGNMENT_FREE)

    system_entry = task_book.add_entry(
        manager, PERSONA_ID, "自己定義の聞き取りに答える", origin=task_book.ORIGIN_SYSTEM,
    )
    got = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert got.text == "この時間は、システムからの依頼「自己定義の聞き取りに答える」に使います。"
    assert got.kind == ASSIGNMENT_SYSTEM
    assert got.task_id == system_entry["task_id"]
    assert got.resolves_on_completion is True
    assert got.is_deadline is False

    # 期限が次のティックより先の一件は引かない (システムタスクのまま)
    task_book.add_entry(
        manager, PERSONA_ID, "挿絵を仕上げる", origin="user", counterpart="user",
        due_at=_epoch(now + timedelta(hours=3)),
    )
    assert "システムからの依頼" in build_tick_assignment(
        manager, PERSONA_ID, now=now, horizon=horizon,
    ).text

    # 次のティックまでに期限が来る一件は、システムタスクより先に引く
    diary = task_book.add_entry(
        manager, PERSONA_ID, "日記の返事を書く", origin="user", counterpart="user",
        due_at=_epoch(now + timedelta(minutes=20)),
    )
    got = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert got.text == (
        "この時間は、タスク帳の一件「日記の返事を書く」に使います。"
        "期限は 2026-07-04 10:20 です。"
    )
    assert got.kind == ASSIGNMENT_DEADLINE and got.is_deadline
    assert got.task_id == diary["task_id"]
    assert got.resolves_on_completion is False

    # 期限を過ぎた一件が最優先 (最も期限の早い一件)
    task_book.add_entry(
        manager, PERSONA_ID, "請求書をまとめる", origin="user", counterpart="user",
        due_at=_epoch(now - timedelta(hours=1)),
    )
    got = build_tick_assignment(manager, PERSONA_ID, now=now, horizon=horizon)
    assert got.text == (
        "この時間は、タスク帳の一件「請求書をまとめる」に使います。"
        "期限の 2026-07-04 09:00 はもう過ぎています。"
    )
    assert got.is_deadline


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
        return TickAssignment(text=None, kind=ASSIGNMENT_FREE)

    monkeypatch.setattr(am_module, "build_tick_assignment", _assign_then_stop)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "autonomy all-stop before start"
    assert _tick_at(manager) is None


def test_conversation_opened_after_the_gate_does_not_start_the_tick(
    manager, session_factory, monkeypatch,
):
    """② 引き当ての最中に会話が開いたら fire_tick を呼ばない (予約は残る)。
    会話の検査は引き当ての後に行う (3 巡目 — 締め切りの回は会話中でも打つため)。"""
    am = _running_life(manager, session_factory)
    from saiverse import autonomy_manager as am_module

    def _assign_then_talk(mgr, persona_id, *, now, horizon):
        uc._set_open_conversation(
            mgr, persona_id, building_id="alice_room", participants=["user"],
        )
        return TickAssignment(text=None, kind=ASSIGNMENT_FREE)

    monkeypatch.setattr(am_module, "build_tick_assignment", _assign_then_talk)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "in conversation"
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_conversation_opened_while_waiting_for_the_beat_does_not_start_the_tick(
    manager, session_factory,
):
    """② Beat ロックを待つ間に会話が開いたら、Beat 内の再検査が生成を止める
    (締め切りの無い回)。予約は残る。"""
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)

    def _talk_while_waiting():
        uc._set_open_conversation(
            manager, PERSONA_ID, building_id="alice_room", participants=["user"],
        )

    manager.fire_tick = FakeTicks(manager, beat_wait=_talk_while_waiting)
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
    # ライフ開始の節目がティックの予約を張った回も再照合する (2 巡目 ⑤)
    assert lifecycle.reconciled == [PERSONA_ID]
    lifecycle.reconciled.clear()

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


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 2 巡目: 生成開始のフェンスを Beat ロックの
# 内側へ (①②③)、ティックの予約の張り直しで保温を再照合 (⑤)
# ---------------------------------------------------------------------------


def _wire_real_beat(manager):
    """本物の fire_tick → PulseController → SEARuntime.run_meta_user (Beat ロック
    + pre_generation_check) を通す。生成の本体 (_run_meta_user_locked) だけ記録係。

    Returns:
        (gate, generated, reached_beat) — reached_beat はティックのスレッドが
        Beat ロックの取得に入ったことを知らせる Event。
    """
    import threading

    from saiverse.saiverse_manager import SAIVerseManager
    from sea.beat_gate import BeatGate
    from sea.pulse_controller import PulseController
    from sea.runtime import SEARuntime

    gate = BeatGate(manager)
    manager.beat_gate = gate
    generated: List[str] = []
    runtime = SEARuntime.__new__(SEARuntime)
    runtime.manager = manager
    runtime._run_meta_user_locked = (
        lambda *a, **k: generated.append(k.get("pulse_type")) or []
    )
    manager.pulse_controller = PulseController(runtime)
    manager._submit_tick = SAIVerseManager._submit_tick.__get__(manager)
    manager.fire_tick = SAIVerseManager.fire_tick.__get__(manager)

    reached_beat = threading.Event()
    holder = threading.current_thread()
    original_get_lock = gate._get_lock

    def _get_lock(persona_id):
        if threading.current_thread() is not holder:
            reached_beat.set()
        return original_get_lock(persona_id)

    gate._get_lock = _get_lock
    return gate, generated, reached_beat


def test_all_stop_while_waiting_for_the_beat_lock_does_not_start_the_tick(
    manager, session_factory,
):
    """① 本物の Beat ロック: 会話が Beat を握っている間にティックが Beat 待ちに
    入り、その間に全停止された。Beat を取った後の再検査で生成を始めない —
    席 (PulseController の _current) は空き、予約は消える。"""
    import threading

    am = _running_life(manager, session_factory)
    gate, generated, reached_beat = _wire_real_beat(manager)
    clock.advance_to(_tick_at(manager))

    with gate.hold(PERSONA_ID, purpose="conversation", check_gate=False):
        worker = threading.Thread(target=am._handle_tick)
        worker.start()
        assert reached_beat.wait(timeout=5)
        manager._autonomy_all_stop = True  # Beat 待ちの間に全停止された
    worker.join(timeout=5)
    assert not worker.is_alive()

    assert generated == []
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "autonomy all-stop before start"
    assert _tick_at(manager) is None
    assert manager.pulse_controller._current == {}


def test_the_real_beat_path_still_generates_when_nothing_changed(
    manager, session_factory,
):
    """① の対照: 何も変わらなければ同じ配線で生成まで進み、走りきった扱いになる。"""
    am = _running_life(manager, session_factory)
    _gate, generated, _reached = _wire_real_beat(manager)
    _fire_tick_reservation(manager)
    assert generated == ["auto"]
    assert am.last_report.status == "completed"


def test_a_standard_call_while_waiting_for_the_beat_lock_skips_the_tick(
    manager, session_factory,
):
    """② 07:30 の発火が Beat を待っている間 (07:31) に会話の標準呼び出しが
    成功した。予約は 08:01 へ押し直され、この発火は生成せずに見送る
    (予約は維持)。"""
    am = _running_life(manager, session_factory)

    def _conversation_call_while_waiting():
        clock.advance_to(BASE + timedelta(hours=7, minutes=31))
        am.notify_standard_call()

    manager.fire_tick = FakeTicks(manager, beat_wait=_conversation_call_while_waiting)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert manager.fire_tick.refused == ["superseded by a standard call"]
    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "superseded by a standard call before start"
    assert _tick_at(manager) == BASE + timedelta(hours=8, minutes=1)


def test_life_end_while_waiting_for_the_beat_lock_cancels_the_tick(
    manager, session_factory,
):
    """③ Beat を待っている間に終了の節目が走った (ended を保存した)。終了の節目の
    取り消しがこの発火の張り直しより先に済んでいて、発火が張り直した自分の予約が
    生き残っている並び (手渡し中の張り直しとの交差) でも、Beat 内の再検査が生成を
    止めて自分の予約 (札の一致する予約) を消す。"""
    am = _running_life(manager, session_factory)
    scheduler = manager.event_scheduler

    def _life_ends_while_waiting():
        # 終了の節目の取り消しは、この発火の張り直しより前に済んでいた — ここでの
        # 取り消しは空振りさせ、発火が張り直した自分の予約を残す。
        real_cancel = scheduler.cancel
        scheduler.cancel = lambda key: False
        try:
            _end_life(manager)
        finally:
            scheduler.cancel = real_cancel
        assert _tick_at(manager) is not None  # 自分の予約が生き残っている

    manager.fire_tick = FakeTicks(manager, beat_wait=_life_ends_while_waiting)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "outside life before start"
    assert _tick_at(manager) is None


def test_a_reservation_rearmed_at_the_same_time_survives_the_old_cleanup(
    manager, session_factory,
):
    """④ ABA (段 3 4 巡目 medium): 発火したワーカーが全停止を観測してから自分の
    予約を消すまでの間に、全停止が解除されて再同期がティックを張り直した。
    仮想時計が止まっているので新しい予約は古い予約と**同じ時刻**になる —
    古いワーカーの後始末は、札の違う新しい予約を消さない。"""
    am = _running_life(manager, session_factory)
    fire_at = _tick_at(manager)
    clock.advance_to(fire_at)
    manager._autonomy_all_stop = True
    rearmed: list = []
    original_gate = am._check_gate

    def _gate_then_resume(now, **kwargs):
        verdict = original_gate(now, **kwargs)
        if verdict[0] is not None and not rearmed:
            # 判断の後・取り消しの前に、全停止の解除と再同期が割り込む
            manager._autonomy_all_stop = False
            assert am.arm_tick() is True
            rearmed.append(_tick_at(manager))
        return verdict

    am._check_gate = _gate_then_resume
    manager.event_scheduler.run_due(clock.now())

    assert am.last_report.status == "skipped"
    assert am.last_report.reason == "autonomy all-stop"
    # 再同期の予約は古いワーカーの張り直しと同じ時刻 (= ABA の並び)
    assert rearmed == [fire_at + timedelta(minutes=30)]
    assert _tick_at(manager) == rearmed[0]  # 消えずに残る


def test_rearming_the_tick_after_a_resume_reconciles_the_keepalive(
    manager, session_factory,
):
    """⑤ 全停止の解除・自律 OFF→ON・再起動の回復で、見張りがティックの予約を
    張り直した回は keep-alive を再照合する (予約が now + T へ後ろ倒しになって
    キャッシュの失効に間に合わないことがあるため)。生きている予約がある回
    (張らなかった回) は再照合しない。"""
    lifecycle = _RecordingLifecycle()
    manager.sea_runtime = SimpleNamespace(session_lifecycle=lifecycle)
    am = _running_life(manager, session_factory)
    assert lifecycle.reconciled == [PERSONA_ID]  # ライフ開始の arm_tick
    lifecycle.reconciled.clear()

    # 停止 (予約が消える) → 再開 → 見張りが走っているライフに予約を張り直す
    am.stop()
    assert _tick_at(manager) is None
    clock.advance_to(BASE + timedelta(hours=7, minutes=25))
    assert am.start() is True
    result = wiring.watchdog_tick(manager, PERSONA_ID)
    assert _tick_at(manager) == BASE + timedelta(hours=7, minutes=55)
    assert lifecycle.reconciled == [PERSONA_ID], result

    # 予約が生きている間の見張りは張らない = 再照合もしない
    wiring.watchdog_tick(manager, PERSONA_ID)
    assert lifecycle.reconciled == [PERSONA_ID]


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 3 巡目: システムタスクの機械的な解消 (high 2)、
# 会話ゲートは締め切りの無い回だけ (medium 1)、予約の取り消しを CAS に (medium 3)
# ---------------------------------------------------------------------------


def _entry(manager, task_id):
    return task_book.get_entry(manager, PERSONA_ID, task_id)


def test_a_completed_tick_resolves_the_self_definition_task_and_frees_the_next(
    manager, session_factory,
):
    """high 2: 自己定義の聞き取り (システムタスク第一号) は、割り当てられた
    ティックが走りきったら機械が済にする。次のティックは自由時間になる (旧実装は
    同じ依頼を永久に割り当て続けた)。"""
    _running_life(manager, session_factory)
    (entry,) = _self_definition_tasks(manager)

    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        f"この時間は、システムからの依頼「{day_plan.SELF_DEFINITION_TASK_CONTENT}」に使います。"
    )
    closed = _entry(manager, entry["task_id"])
    assert closed["status"] == task_book.STATUS_DONE
    assert closed["outcome"] == AutonomyManager.SYSTEM_TASK_TICK_OUTCOME

    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[1]["assignment"] is None
    assert manager._autonomy_managers[PERSONA_ID].last_report.reason == "free"


def test_a_tick_that_did_not_run_through_keeps_the_system_task(
    manager, session_factory,
):
    """high 2: 走りきらなかった回 (not_completed・生成前の拒否) はシステムタスクを
    閉じない — 次のティックが同じ件をまた引く。"""
    am = _running_life(manager, session_factory)
    (entry,) = _self_definition_tasks(manager)

    manager.fire_tick = FakeTicks(
        manager, outcome={"action": "execute", "runtime_outcome": "error",
                          "error": "boom", "outputs": None},
        standard_call=False,
    )
    _fire_tick_reservation(manager)
    assert am.last_report.status == "not_completed"
    assert _entry(manager, entry["task_id"])["status"] == task_book.STATUS_OPEN

    def _conversation_call_while_waiting():
        am.notify_standard_call()  # Beat 待ちの間に標準呼び出し = 見送り

    manager.fire_tick = FakeTicks(manager, beat_wait=_conversation_call_while_waiting)
    _fire_tick_reservation(manager)
    assert am.last_report.reason == "superseded by a standard call before start"
    assert _entry(manager, entry["task_id"])["status"] == task_book.STATUS_OPEN

    manager.fire_tick = FakeTicks(manager)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        f"この時間は、システムからの依頼「{day_plan.SELF_DEFINITION_TASK_CONTENT}」に使います。"
    )
    assert _entry(manager, entry["task_id"])["status"] == task_book.STATUS_DONE


def test_an_on_event_system_task_is_resolved_the_same_way(manager, session_factory):
    """high 2: on_event 由来のシステムタスク (判断の結果として機械が積んだ依頼) も
    同じく、割り当てたティックが走りきったら済になる。"""
    _mark_self_defined(session_factory)
    _running_life(manager, session_factory)
    entry = task_book.add_system_task(
        manager, PERSONA_ID, "届いた手紙に目を通す", idem_key="on_event:test-1",
    )
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        "この時間は、システムからの依頼「届いた手紙に目を通す」に使います。"
    )
    assert _entry(manager, entry["task_id"])["status"] == task_book.STATUS_DONE


def test_a_system_task_edited_after_it_was_drawn_stays_open(manager, session_factory):
    """① 段 3 4 巡目 high: 引き当ての後 (生成の最中) に中身を書き換えられた
    システムタスクは、ティックが走りきっても済にしない — このティックが実行した
    のは古い内容。次のティックが新しい内容で引き直し、それが走りきったら済になる。"""
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)
    entry = task_book.add_system_task(
        manager, PERSONA_ID, "届いた手紙に目を通す", idem_key="on_event:edit-1",
    )

    def _edited_while_running():
        task_book.update_entry(
            manager, PERSONA_ID, entry["task_id"], content="届いた手紙に返事を書く",
        )

    manager.fire_tick = FakeTicks(manager, beat_wait=_edited_while_running)
    _fire_tick_reservation(manager)
    assert am.last_report.status == "completed"
    assert manager.fire_tick.calls[0]["assignment"] == (
        "この時間は、システムからの依頼「届いた手紙に目を通す」に使います。"
    )
    edited = _entry(manager, entry["task_id"])
    assert edited["status"] == task_book.STATUS_OPEN
    assert edited["content"] == "届いた手紙に返事を書く"

    manager.fire_tick = FakeTicks(manager)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls[0]["assignment"] == (
        "この時間は、システムからの依頼「届いた手紙に返事を書く」に使います。"
    )
    assert _entry(manager, entry["task_id"])["status"] == task_book.STATUS_DONE


def test_a_system_task_given_a_deadline_after_it_was_drawn_stays_open(
    manager, session_factory,
):
    """① 段 3 4 巡目 high の同族: 引き当て後に期限が付いた一件も済にしない
    (済にしてよいのは「期限の無い」一件だけで、その前提が引き当て後に崩れた)。"""
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)
    entry = task_book.add_system_task(
        manager, PERSONA_ID, "棚の本を並べ直す", idem_key="on_event:due-1",
    )
    due = _epoch(BASE + timedelta(days=2))

    def _given_a_deadline_while_running():
        task_book.update_entry(manager, PERSONA_ID, entry["task_id"], due_at=due)

    manager.fire_tick = FakeTicks(manager, beat_wait=_given_a_deadline_while_running)
    _fire_tick_reservation(manager)
    assert am.last_report.status == "completed"
    after = _entry(manager, entry["task_id"])
    assert after["status"] == task_book.STATUS_OPEN
    assert after["due_at"] == due


def test_deadline_tasks_are_not_resolved_by_the_tick(manager, session_factory):
    """締め切りのある件は済にしない — 未完なら台帳に残り、次の空きティックが
    自然に同じ件を引く (v3 §5)。期限つきのシステムタスクも同じ。"""
    _mark_self_defined(session_factory)
    _running_life(manager, session_factory)  # 07:00、ティックは 07:30
    due_soon = task_book.add_entry(
        manager, PERSONA_ID, "日記の返事を書く", origin="user", counterpart="user",
        due_at=_epoch(BASE + timedelta(hours=7, minutes=45)),
    )
    dated_system = task_book.add_entry(
        manager, PERSONA_ID, "月末の整理", origin=task_book.ORIGIN_SYSTEM,
        due_at=_epoch(BASE + timedelta(days=3)),
    )
    _fire_tick_reservation(manager)
    assert "日記の返事を書く" in manager.fire_tick.calls[0]["assignment"]
    assert _entry(manager, due_soon["task_id"])["status"] == task_book.STATUS_OPEN

    task_book.withdraw_entry(manager, PERSONA_ID, due_soon["task_id"])
    _fire_tick_reservation(manager)
    assert "月末の整理" in manager.fire_tick.calls[1]["assignment"]
    assert _entry(manager, dated_system["task_id"])["status"] == task_book.STATUS_OPEN


def test_a_deadline_tick_runs_during_an_open_conversation(manager, session_factory):
    """medium 1: 会話中に打たないのは締め切りの無い回だけ (計画 §5 段 3-5)。
    期限の迫った一件を引いた回は、会話が開いていても打つ。Beat 待ちの間に会話が
    開いても止めない。"""
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)  # ティックは 07:30
    task_book.add_entry(
        manager, PERSONA_ID, "日記の返事を書く", origin="user", counterpart="user",
        due_at=_epoch(BASE + timedelta(hours=7, minutes=45)),
    )
    uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room", participants=["user"],
    )
    _fire_tick_reservation(manager)
    assert am.last_report.status == "completed"
    assert "日記の返事を書く" in manager.fire_tick.calls[0]["assignment"]

    # Beat 待ちの間に会話が開いても、締め切りの回は止めない
    uc.clear_open_conversation(manager, PERSONA_ID)

    def _talk_while_waiting():
        uc._set_open_conversation(
            manager, PERSONA_ID, building_id="alice_room", participants=["user"],
        )

    manager.fire_tick = FakeTicks(manager, beat_wait=_talk_while_waiting)
    _fire_tick_reservation(manager)  # 08:00 — 期限 07:45 を過ぎた一件
    assert manager.fire_tick.refused == []
    assert "もう過ぎています" in manager.fire_tick.calls[0]["assignment"]
    assert am.last_report.status == "completed"


def test_a_free_tick_during_an_open_conversation_is_still_skipped(
    manager, session_factory,
):
    """medium 1 の対照: 締め切りの無い回 (自由時間) は会話中は見送る。期限が
    次のティックより先の一件は締め切りの回にならない。"""
    _mark_self_defined(session_factory)
    am = _running_life(manager, session_factory)
    task_book.add_entry(
        manager, PERSONA_ID, "挿絵を仕上げる", origin="user", counterpart="user",
        due_at=_epoch(BASE + timedelta(hours=12)),
    )
    uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room", participants=["user"],
    )
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert am.last_report.reason == "in conversation"
    assert _tick_at(manager) == clock.now() + timedelta(minutes=30)


def test_an_old_refusal_does_not_cancel_the_reservation_armed_by_a_resume(
    manager, session_factory,
):
    """medium 3 (レビューの再現): ゲートが自律 OFF を観測して返った後、取り消しの
    前に別スレッドが OFF→ON を保存し、再同期 (stop → start) と見張りが新しい
    予約を張った。古い発火の後始末は自分が張った予約だけを消すので、新しい
    予約は生き残る (旧実装は同じ key を無条件に消し、再開の時刻が後退した)。"""
    am = _running_life(manager, session_factory)  # ティックは 07:30
    persona = manager.personas[PERSONA_ID]
    persona.autonomy_enabled = False
    original_gate = am._check_gate
    resumed: Dict[str, Any] = {}

    def _gate_then_resume(now, **kwargs):
        result = original_gate(now, **kwargs)
        if result[0] == "autonomy off" and not resumed:
            persona.autonomy_enabled = True
            am.stop()
            assert am.start() is True
            clock.advance_to(clock.now() + timedelta(minutes=1))
            assert am.arm_tick_if_absent() is True
            resumed["at"] = _tick_at(manager)
        return result

    am._check_gate = _gate_then_resume
    # 07:30 の発火がワーカーへ手渡された (予約は辞書から外れている)。run_due で
    # 回すと再開で即時に張られた見張りまで同じ回で走るので、ワーカーを直接呼ぶ。
    clock.advance_to(_tick_at(manager))
    manager.event_scheduler.cancel(tick_reservation_key(PERSONA_ID))
    am._handle_tick()
    assert am.last_report.reason == "autonomy off"
    assert resumed["at"] == BASE + timedelta(hours=8, minutes=1)
    assert _tick_at(manager) == resumed["at"]


def test_a_refusal_still_cancels_its_own_reservation(manager, session_factory):
    """medium 3 の対照: 割り込みが無ければ、拒否した発火は自分が張った予約を
    従来どおり消す。"""
    am = _running_life(manager, session_factory)
    manager.personas[PERSONA_ID].autonomy_enabled = False
    _fire_tick_reservation(manager)
    assert am.last_report.reason == "autonomy off"
    assert _tick_at(manager) is None


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 6 巡目: 停止・再開の世代 (medium 1)、
# 再起動の回復が均等モードの 1h を建て直す (medium 3)
# ---------------------------------------------------------------------------


def _hand_off_tick(manager):
    """ティックの予約を EventScheduler がワーカーへ手渡した状態にして callback を返す
    (辞書と heap から外れ、stop() の取り消しが届かない)。"""
    entry = manager.event_scheduler._entries_by_key.pop(tick_reservation_key(PERSONA_ID))
    entry.cancelled = True  # heap からも外れた扱い (run_due が二度拾わない)
    return entry.callback


def test_a_tick_handed_off_before_stop_does_not_run_after_restart(
    manager, session_factory,
):
    """medium 1 の再現順序: 07:30 の発火を手渡しで保留 → 停止 → 07:31 に再開 →
    保留していた発火が再開。状態は RUNNING に戻っているが、停止を跨いだ古い
    世代の発火なので何もしない (生成しない・再開後の予約を上書きしない)。
    再開後に張った新しい予約は正常に発火する。"""
    am = _running_life(manager, session_factory)
    fire_at = _tick_at(manager)
    clock.advance_to(fire_at)
    stale_fire = _hand_off_tick(manager)

    assert am.stop() is True
    clock.advance_to(fire_at + timedelta(minutes=1))
    assert am.start() is True
    wiring.watchdog_tick(manager, PERSONA_ID)  # 再開の見張りが新しい予約を張る
    new_at = _tick_at(manager)
    assert new_at == clock.now() + timedelta(minutes=30)

    stale_fire()
    assert manager.fire_tick.calls == []
    assert _tick_at(manager) == new_at

    _fire_tick_reservation(manager)
    assert len(manager.fire_tick.calls) == 1
    assert am.last_report.status == "completed"


def test_a_stop_and_restart_while_waiting_for_the_beat_lock_does_not_start_the_tick(
    manager, session_factory,
):
    """medium 1 (生成直前のフェンス): 発火の入口を通った後、Beat を待っている間に
    停止→再開が挟まった。いまの状態は RUNNING でも、生成の直前の世代の照合で
    見送る。"""
    am = _running_life(manager, session_factory)

    def _stop_and_restart_while_waiting():
        am.stop()
        am.start()

    manager.fire_tick = FakeTicks(manager, beat_wait=_stop_and_restart_while_waiting)
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert manager.fire_tick.refused == ["stopped"]


def _wire_cache_overrides(manager):
    """本物の SAIVerseManager の cache override の読み書きを生やす。"""
    from saiverse.saiverse_manager import SAIVerseManager

    manager._persona_cache_overrides = {}
    for name in (
        "get_persona_cache_override", "set_persona_cache_override",
        "clear_persona_cache_override",
    ):
        setattr(manager, name, getattr(SAIVerseManager, name).__get__(manager))


EVEN_LIFE = {"life_mode_override": "even"}
LIFE_TTL = {"enabled": True, "ttl": "1h"}


def _running_even_life(manager, session_factory):
    """均等モードのライフを走らせる (起床の帳簿処理に設定の params を渡す)。"""
    _set_life_window(session_factory, params=EVEN_LIFE)
    clock.enable_virtual(BASE + timedelta(hours=7))
    am = _start_am(manager)
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START, params=EVEN_LIFE,
    ) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["mode"] == "even"
    return am


def _restart_in_memory(manager, am):
    """再起動: in-memory の override・持ち主の記録と、予約が消える。"""
    am.stop()
    manager._persona_cache_overrides.clear()
    manager._life_ttl_owners.clear()


def test_watchdog_restores_the_even_life_ttl_after_a_restart(manager, session_factory):
    """medium 3 の再現順序: 均等モードのライフの途中で再起動 (override と持ち主が
    消える。開始の節目は started で決着済みなので撃ち直されない) → 見張りが
    ティックと一緒に 1h と持ち主を建て直す。二度目の見張りは何もしない。"""
    _wire_cache_overrides(manager)
    am = _running_even_life(manager, session_factory)
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_TTL

    _restart_in_memory(manager, am)
    clock.advance_to(BASE + timedelta(hours=7, minutes=10))
    assert am.start() is True
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none", "ttl_restored": True, "tick_armed": True}
    assert manager.get_persona_cache_override(PERSONA_ID) == LIFE_TTL
    assert manager._life_ttl_owners[PERSONA_ID] == (PLAN_DATE, 0)

    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none", "ttl_restored": False, "tick_armed": False}

    # 建て直した持ち主で、ライフ終了の遅延解除が普通に外せる
    clock.advance_to(BASE + timedelta(hours=22))
    _end_life(manager)
    key = f"life_ttl_clear:{PERSONA_ID}"
    entry = manager.event_scheduler._entries_by_key.pop(key)
    entry.callback()
    assert manager.get_persona_cache_override(PERSONA_ID) is None


def test_watchdog_restore_leaves_a_user_cache_setting_alone(manager, session_factory):
    """medium 3 の対照: 再起動の後、見張りより先に利用者が人設定タブで明示した
    設定は触らない (持ち主の無い override は利用者のもの)。"""
    _wire_cache_overrides(manager)
    am = _running_even_life(manager, session_factory)
    _restart_in_memory(manager, am)
    day_plan.apply_user_cache_override(manager, PERSONA_ID, enabled=False, ttl="5m")

    clock.advance_to(BASE + timedelta(hours=7, minutes=10))
    assert am.start() is True
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["ttl_restored"] is False
    assert manager.get_persona_cache_override(PERSONA_ID) == {"enabled": False, "ttl": "5m"}
    assert PERSONA_ID not in manager._life_ttl_owners


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 7 巡目: 最終ゲートの後の世代の再照合
# (medium 1)、ライフの切り替わりでティックの世代を進める (medium 2)
# ---------------------------------------------------------------------------


def test_a_stop_and_restart_during_the_final_gate_read_does_not_start_the_tick(
    manager, session_factory,
):
    """medium 1 の再現順序: 生成直前のフェンスが冒頭の世代の照合を通った後、
    ゲートのライフの読み (DB) の最中に停止→再開が完了した。いまの状態は
    RUNNING でライフも走っているが、許可を返す直前の最後の照合で見送る。"""
    am = _running_life(manager, session_factory)
    original_gate = am._check_gate

    def _gate_with_stop_and_restart_inside(now, *, check_conversation=True):
        verdict = original_gate(now, check_conversation=check_conversation)
        if check_conversation:  # 生成直前のフェンスの読み (冒頭のゲートは会話を見ない)
            assert am.stop() is True
            assert am.start() is True
        return verdict

    am._check_gate = _gate_with_stop_and_restart_inside
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert manager.fire_tick.refused == ["stopped"]


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 8 巡目 medium: 生成直前のフェンスの最後の
# 照合は、公開済みの全停止の値と自律 OFF の値を読み直す
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "publish, expected",
    [
        (lambda manager: setattr(manager, "_autonomy_all_stop", True), "autonomy all-stop"),
        (
            lambda manager: setattr(manager.personas[PERSONA_ID], "autonomy_enabled", False),
            "autonomy off",
        ),
    ],
    ids=["all-stop", "autonomy-off"],
)
def test_a_stop_published_during_the_final_gate_read_does_not_start_the_tick(
    manager, session_factory, publish, expected,
):
    """再現順序: 生成直前のフェンスのゲートが全停止・自律 OFF の検査を通った後、
    ライフの読み (DB) の最中に全停止の値 (または自律 OFF) が公開された。この
    ペルソナの stop() はまだ届いていない (全停止は「公開 → 全ペルソナを順に
    停止同期」の順) ので、状態と世代の照合は通る — 最後の照合が公開値を読み直して
    見送り、自分の予約も消す。"""
    am = _running_life(manager, session_factory)
    original_gate = am._check_gate

    def _gate_with_a_stop_published_inside(now, *, check_conversation=True):
        verdict = original_gate(now, check_conversation=check_conversation)
        if check_conversation:  # 生成直前のフェンスの読み (冒頭のゲートは会話を見ない)
            publish(manager)
        return verdict

    am._check_gate = _gate_with_a_stop_published_inside
    _fire_tick_reservation(manager)
    assert manager.fire_tick.calls == []
    assert manager.fire_tick.refused == [expected]
    assert am.state != AutonomyState.STOPPED  # stop() はまだ届いていない
    assert am.last_report.reason == f"{expected} before start"
    assert _tick_at(manager) is None


def _set_overnight_life_window(session_factory):
    """07:00〜翌 06:59 のライフ (終了の直後に次のライフが始まる並び)。"""
    _add_day_schedule(session_factory, wiring.LIFE_START_SCHEDULE_PLAYBOOK, "07:00")
    _add_day_schedule(session_factory, wiring.LIFE_END_SCHEDULE_PLAYBOOK, "06:59")


def test_a_tick_handed_off_in_the_previous_life_does_not_run_in_the_next(
    manager, session_factory,
):
    """medium 2 の再現順序: 前のライフのティックの発火を手渡しで保留 → ライフ
    終了 → 次のライフの開始 (新しい予約は 07:30) → 07:02 に保留していた発火が
    再開。停止は挟まっていない (運転の世代は同じ) が、ライフ終了を跨いだ古い
    発火なので何もしない — 次のライフの予約を上書きせず、生成もしない。次の
    ライフの予約は正常に発火する。"""
    _set_overnight_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    am = _start_am(manager)
    _start_life(manager)
    stale_fire = _hand_off_tick(manager)  # 前のライフの 07:30 の発火

    clock.advance_to(BASE + timedelta(days=1, hours=6, minutes=59))
    _end_life(manager)
    assert _tick_at(manager) is None
    clock.advance_to(BASE + timedelta(days=1, hours=7))
    _start_life(manager)
    new_at = _tick_at(manager)
    assert new_at == BASE + timedelta(days=1, hours=7, minutes=30)

    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=2))
    stale_fire()
    assert manager.fire_tick.calls == []
    assert _tick_at(manager) == new_at

    _fire_tick_reservation(manager)
    assert len(manager.fire_tick.calls) == 1
    assert am.last_report.status == "completed"


def test_a_life_switch_while_waiting_for_the_beat_lock_does_not_start_the_tick(
    manager, session_factory,
):
    """medium 2 (生成直前のフェンス): 前のライフの発火が Beat を待っている間に、
    ライフ終了と次のライフの開始が済んだ。次のライフは走っているのでゲートは
    通るが、ライフの世代の照合で見送る。次のライフの予約は残る。"""
    _set_overnight_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    _start_am(manager)
    _start_life(manager)  # 前日 (07-04) 07:00〜翌 06:59 のライフ
    switched: Dict[str, Any] = {}

    def _life_switch_while_waiting():
        clock.advance_to(BASE + timedelta(days=1, hours=6, minutes=59))
        _end_life(manager)
        clock.advance_to(BASE + timedelta(days=1, hours=7))
        _start_life(manager)
        switched["at"] = _tick_at(manager)

    manager.fire_tick = FakeTicks(manager, beat_wait=_life_switch_while_waiting)
    # ライフの尻尾 (翌 06:58) で前のライフの予約が発火し、ゲートを通って Beat
    # 待ちに入る。
    stale_fire = _hand_off_tick(manager)
    clock.advance_to(BASE + timedelta(days=1, hours=6, minutes=58))
    stale_fire()
    assert manager.fire_tick.calls == []
    assert manager.fire_tick.refused == ["outside life"]
    assert switched["at"] == BASE + timedelta(days=1, hours=7, minutes=30)
    assert _tick_at(manager) == switched["at"]


# ---------------------------------------------------------------------------
# 2026-10-12 Codex 敵対レビュー 段 3 9 巡目 medium: ライフ終了の取り消しは
# ティックの持ち主 (ライフ) のときだけ効く — 終了処理が遅れている間に次の
# ライフが開始していたら、次のライフの予約も世代も触らない
# ---------------------------------------------------------------------------


def _start_the_next_life_inside_the_end(monkeypatch, manager, *, hand_off=False):
    """前のライフの終了処理の「最初の取り消し」と「ended の commit」の間 (TTL の
    遅延解除の予約の段) に、翌 07:00 の次のライフを開始させる。

    ``hand_off=True`` なら、開始した次のライフの予約を、終了処理が終わる前に
    ワーカーへ手渡した状態にする (取り消しの届かない発火)。
    """
    seen: Dict[str, Any] = {}
    original = day_plan._sync_cache_ttl_for_life_end

    def _sync_with_the_next_life_started(mgr, persona_id, life, *, life_key):
        if not seen:
            clock.advance_to(BASE + timedelta(days=1, hours=7))
            _start_life(manager)
            seen["next_at"] = _tick_at(manager)
            if hand_off:
                seen["handed_off"] = _hand_off_tick(manager)
        return original(mgr, persona_id, life, life_key=life_key)

    monkeypatch.setattr(
        day_plan, "_sync_cache_ttl_for_life_end", _sync_with_the_next_life_started,
    )
    return seen


def test_a_late_life_end_does_not_cancel_the_next_lifes_tick(
    manager, session_factory, monkeypatch,
):
    """再現順序: 翌 06:59 の就寝の終了処理が、最初の取り消しの後の DB の区間で
    遅れ、その間に 07:00 起床の次のライフが開始した (07:30 の予約)。ended の
    commit の後の二度目の取り消しは、持ち主が次のライフに替わっているので何も
    しない — 07:30 の予約が残り、見張りの回復 (07:39) を待たずに 07:30 で打つ。"""
    _set_overnight_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    am = _start_am(manager)
    _start_life(manager)  # 07-04 07:00〜翌 06:59
    seen = _start_the_next_life_inside_the_end(monkeypatch, manager)

    clock.advance_to(BASE + timedelta(days=1, hours=6, minutes=59))
    generation_before = am.life_generation
    _end_life(manager)

    next_at = BASE + timedelta(days=1, hours=7, minutes=30)
    assert seen["next_at"] == next_at
    assert _tick_at(manager) == next_at
    # 最初の取り消し (前のライフが持ち主) の一回だけ進む
    assert am.life_generation == generation_before + 1
    assert am._tick_life_owner == ("2026-07-05", 0)

    _fire_tick_reservation(manager)
    assert clock.now() == next_at
    assert len(manager.fire_tick.calls) == 1
    assert am.last_report.status == "completed"


def test_a_late_life_end_does_not_invalidate_the_next_lifes_handed_off_tick(
    manager, session_factory, monkeypatch,
):
    """同じ順序で、次のライフの予約が終了処理の終わる前にワーカーへ手渡されて
    いた。二度目の取り消しは世代を進めないので、手渡し済みの発火は次のライフの
    ものとして走る。"""
    _set_overnight_life_window(session_factory)
    clock.enable_virtual(BASE + timedelta(hours=7))
    am = _start_am(manager)
    _start_life(manager)
    seen = _start_the_next_life_inside_the_end(monkeypatch, manager, hand_off=True)

    clock.advance_to(BASE + timedelta(days=1, hours=6, minutes=59))
    _end_life(manager)

    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=30))
    seen["handed_off"]()
    assert len(manager.fire_tick.calls) == 1
    assert am.last_report.status == "completed"


def test_a_normal_life_end_still_advances_the_generation_after_the_commit(
    manager, session_factory,
):
    """対照: 次のライフが始まっていない普通の終了では、ended の commit の後の
    二度目の取り消しも世代を進める (8 巡目 — 取り消しと commit の間に走り始めた
    保温の最後の照合を外す)。予約は残らず、持ち主の記録も消える。"""
    am = _running_life(manager, session_factory)
    stale_fire = _hand_off_tick(manager)
    generation_before = am.life_generation

    clock.advance_to(BASE + timedelta(hours=22))
    _end_life(manager)
    assert am.life_generation == generation_before + 2
    assert _tick_at(manager) is None
    assert am._tick_life_owner is None

    stale_fire()
    assert manager.fire_tick.calls == []


def test_the_life_end_cancel_ignores_a_life_that_does_not_own_the_tick(
    manager, session_factory,
):
    """持ち主つきの取り消しの単体: 持ち主でないライフの終了は予約も世代も触らず、
    持ち主のライフの終了は両方を進める。持ち主の記録が無い (再起動の後に誰も
    張っていない) ときは効く。"""
    am = _running_life(manager, session_factory)
    owner = am._tick_life_owner
    assert owner == (PLAN_DATE, 0)
    at = _tick_at(manager)
    generation = am.life_generation

    assert am.cancel_tick_for_life(("2026-07-03", 0)) is False
    assert _tick_at(manager) == at
    assert am.life_generation == generation

    assert am.cancel_tick_for_life(owner) is True
    assert _tick_at(manager) is None
    assert am.life_generation == generation + 1
    assert am._tick_life_owner is None

    assert am.cancel_tick_for_life(("2026-07-03", 0)) is False  # 予約は無い
    assert am.life_generation == generation + 2  # 記録が無いので効く


def test_the_watchdog_restore_records_the_running_life_as_the_owner(
    manager, session_factory,
):
    """見張りの回復で張った予約も、走っているライフが持ち主になる — その
    ライフの終了の取り消しが効く。"""
    am = _running_life(manager, session_factory)
    am.stop()
    am._tick_life_owner = None  # 再起動 (in-memory の記録が消える)
    assert am.start() is True
    clock.advance_to(clock.now() + timedelta(minutes=10))
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["tick_armed"] is True
    assert am._tick_life_owner == (PLAN_DATE, 0)

    clock.advance_to(BASE + timedelta(hours=22))
    _end_life(manager)
    assert _tick_at(manager) is None
