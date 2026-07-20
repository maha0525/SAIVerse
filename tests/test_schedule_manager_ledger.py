"""ScheduleManager 発火の台帳化 (W3 Chunk B) の回帰テスト。

docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D1/D3/D4/D5 と
自律行動監査 (2026-07-14) A13 の「必要な回帰」:

1. dispatch 前例外 (persona 不在) → oneshot COMPLETED=False のまま + 台帳
   failed + backoff 再予約 → 再試行成功で一度だけ実行・COMPLETED=True・
   台帳 applied→completed
2. queued (action=queued) → accepted: COMPLETED=True + 台帳 completed +
   result に action 記録
3. runtime 例外 (runtime_outcome=error) → 台帳 unknown + COMPLETED=False +
   再予約なし + 同一 occurrence の再 claim が runnable=False
4. interval 失敗時 LAST_EXECUTED_AT 不変 / 成功時のみ更新
5. periodic 判断点で handle_scheduled_judgment 例外 → failed + 同日 backoff
   再予約 (翌日へ飛ばない)
6. 判断点 reason="duplicate:completed" → settled_skip: 前進 + 台帳 applied +
   再試行なし
7. 同一 occurrence の二重発火 (callback を 2 回呼ぶ) → dispatch 1 回のみ
8. 旧世代 callback (generation 不一致) → dispatch 0 回 + 最新で再登録される
9. settle が単一 commit: mark_applied(session=) 後 commit 前の例外で状態前進も
   applied も残らない
10. attempt 上限到達: oneshot は再登録なし / periodic は次 occurrence 登録

構成: manager は SimpleNamespace mock + 実 DB (in-memory SQLite + StaticPool)
+ 実 ExecutionLedger。EventScheduler は start() せず、予約 heap の
``_entries_by_key`` を直接検査し、callback を手動で呼んで発火を模す
(dispatch スレッドの直列実行と等価)。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, PersonaSchedule, User
from saiverse import autonomy_wiring as wiring
from saiverse import clock
from saiverse.event_scheduler import EventScheduler
from saiverse.execution_ledger import ExecutionLedger
from saiverse.schedule_manager import (
    SCHEDULE_DISPATCH_LEDGER_KIND,
    SCHEDULE_DISPATCH_MAX_ATTEMPTS,
    ScheduleManager,
    _instance_token,
    _occurrence_key,
    _occurrence_token,
    _schedule_key,
)

PERSONA_ID = "alice"


# ---------------------------------------------------------------------------
# fixtures / helpers
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


class DispatchStub:
    """dispatch_schedule_fire の型付き戻り値を差し替えられるフェイク。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.result: Dict[str, Any] = {
            "action": "execute", "runtime_outcome": "completed", "error": None,
        }

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _seed_db(session_factory) -> None:
    db = session_factory()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="tester"))
        db.flush()
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
        db.add(city)
        db.flush()
        db.add(AI(AIID=PERSONA_ID, HOME_CITYID=city.CITYID, AINAME="Alice"))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def env(session_factory):
    """(manager, ScheduleManager, DispatchStub) の束。"""
    _seed_db(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        _save_session_metadata=lambda: None,
    )
    stub = DispatchStub()
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        all_personas={PERSONA_ID: persona},
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (手動発火)
        execution_ledger=ExecutionLedger(session_factory=session_factory),
        pulse_dispatcher=SimpleNamespace(dispatch_schedule_fire=stub),
        _save_modified_buildings=lambda: None,
    )
    sm = ScheduleManager(saiverse_manager=manager)
    return SimpleNamespace(manager=manager, sm=sm, stub=stub,
                           session_factory=session_factory)


def _add_schedule(session_factory, **overrides) -> int:
    defaults = dict(
        PERSONA_ID=PERSONA_ID,
        SCHEDULE_TYPE="oneshot",
        META_PLAYBOOK="track_user_conversation",
        ENABLED=True,
        SCHEDULED_DATETIME=datetime.now(timezone.utc).replace(tzinfo=None)
        - timedelta(minutes=1),
        COMPLETED=False,
    )
    defaults.update(overrides)
    db = session_factory()
    try:
        row = PersonaSchedule(**defaults)
        db.add(row)
        db.commit()
        return row.SCHEDULE_ID
    finally:
        db.close()


def _entry(env, schedule_id):
    """現在有効な予約エントリ (無ければ None)。"""
    return env.manager.event_scheduler._entries_by_key.get(_schedule_key(schedule_id))


def _fire(env, schedule_id):
    """現在の予約の callback を手動で呼ぶ (dispatch スレッドの直列実行を模す)。"""
    entry = _entry(env, schedule_id)
    assert entry is not None, f"no reservation for schedule {schedule_id}"
    entry.callback()
    return entry


def _occurrence_key_of(env, entry, schedule_id) -> str:
    """予約エントリと DB の現在状態から台帳の冪等キーを計算する。

    初回 interval (LAST_EXECUTED_AT=None) は epoch でなく安定 sentinel "first"
    (Codex W3 指摘 1)。key は {id}:{instance}:g{世代}:{occurrence} —
    行一生トークン (第三陣) と設定世代 (第七陣で独立成分へ昇格) を含む。
    直接 INSERT した行は INSTANCE_TOKEN NULL = "legacy"。実装と同じヘルパで
    組み立てる。
    """
    schedule = _get_schedule(env, schedule_id)
    token = _occurrence_token(schedule, datetime.fromtimestamp(entry.fire_at_ts))
    return _occurrence_key(
        schedule_id, _instance_token(schedule),
        schedule.SYNC_GENERATION or 0, token,
    )


def _ledger_row(env, key):
    return env.manager.execution_ledger.find_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key,
    )


def _get_schedule(env, schedule_id) -> PersonaSchedule:
    db = env.session_factory()
    try:
        return db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id
        ).first()
    finally:
        db.close()


def _past_time_of_day(minutes_ago: int = 5) -> str:
    """UTC で minutes_ago 分前の "HH:MM" (periodic の次回発火を遠い未来にする)。"""
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).strftime("%H:%M")


# ---------------------------------------------------------------------------
# 1. dispatch 前例外 → failed + backoff → 再試行成功で一度だけ実行
# ---------------------------------------------------------------------------


def test_oneshot_failed_then_retry_succeeds_once(env):
    sid = _add_schedule(env.session_factory)
    assert env.sm.register_schedule(sid) == "registered"
    first = _entry(env, sid)
    key = _occurrence_key_of(env, first, sid)

    # persona 不在 → dispatch 前の failed (副作用ゼロ)
    env.manager.all_personas.pop(PERSONA_ID)
    _fire(env, sid)

    assert env.stub.calls == []  # dispatch まで到達しない
    assert _get_schedule(env, sid).COMPLETED is False
    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "failed"

    # backoff 再予約が同一 key に積まれている (~120 秒後)
    retry = _entry(env, sid)
    assert retry is not None and retry is not first
    delta = retry.fire_at_ts - time.time()
    assert 60 < delta < 300

    # persona 復帰 → 再試行成功: 一度だけ実行され completed へ
    env.manager.all_personas[PERSONA_ID] = env.manager.personas[PERSONA_ID]
    _fire(env, sid)

    assert len(env.stub.calls) == 1
    assert _get_schedule(env, sid).COMPLETED is True
    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "completed"
    # oneshot 完了 → 次回予約なし (cancel 済み)
    assert _entry(env, sid) is None


# ---------------------------------------------------------------------------
# 2. queued → accepted: 前進 + 台帳 completed + result に action 記録
# ---------------------------------------------------------------------------


def test_queued_dispatch_is_accepted_and_advances(env):
    sid = _add_schedule(env.session_factory)
    env.stub.result = {"action": "queued", "runtime_outcome": None, "error": None}
    env.sm.register_schedule(sid)
    key = _occurrence_key_of(env, _entry(env, sid), sid)

    _fire(env, sid)

    assert len(env.stub.calls) == 1
    assert _get_schedule(env, sid).COMPLETED is True
    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "completed"
    assert row["result"]["action"] == "accepted"
    assert "queued" in row["result"]["reason"]
    assert row["result"]["schedule_type"] == "oneshot"


# ---------------------------------------------------------------------------
# 3. runtime 例外 → unknown: 前進なし + 再予約なし + 再 claim ブロック
# ---------------------------------------------------------------------------


def test_runtime_error_marks_unknown_and_blocks_reexecution(env):
    sid = _add_schedule(env.session_factory)
    env.stub.result = {"action": "execute", "runtime_outcome": "error", "error": "boom"}
    env.sm.register_schedule(sid)
    fired = _entry(env, sid)
    key = _occurrence_key_of(env, fired, sid)

    _fire(env, sid)

    assert len(env.stub.calls) == 1
    assert _get_schedule(env, sid).COMPLETED is False
    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "unknown"
    # 再予約なし (map のエントリは発火済みの残骸のまま = schedule() は呼ばれていない)
    assert _entry(env, sid) is fired

    # 同一 occurrence の再発火 (残骸 callback の再実行) → claim runnable=False で
    # dispatch は走らず、同一 occurrence への再登録もしない (hot loop 防止)
    fired.callback()
    assert len(env.stub.calls) == 1
    assert _ledger_row(env, key)["status"] == "unknown"
    assert _entry(env, sid) is fired


# ---------------------------------------------------------------------------
# 4. interval: 失敗時 LAST_EXECUTED_AT 不変 / 成功時のみ更新
# ---------------------------------------------------------------------------


def test_interval_last_executed_at_only_advances_on_success(env):
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
    )
    env.stub.result = {"action": "unavailable", "runtime_outcome": None, "error": None}
    env.sm.register_schedule(sid)
    key = _occurrence_key_of(env, _entry(env, sid), sid)

    _fire(env, sid)  # failed

    assert _get_schedule(env, sid).LAST_EXECUTED_AT is None
    assert _ledger_row(env, key)["status"] == "failed"
    retry = _entry(env, sid)
    assert 60 < retry.fire_at_ts - time.time() < 300  # backoff 再予約

    env.stub.result = {"action": "execute", "runtime_outcome": "completed", "error": None}
    _fire(env, sid)  # 再試行成功

    schedule = _get_schedule(env, sid)
    assert schedule.LAST_EXECUTED_AT is not None
    assert _ledger_row(env, key)["status"] == "completed"
    # 次回は LAST_EXECUTED_AT + interval (~1 時間後)
    nxt = _entry(env, sid)
    assert nxt.fire_at_ts - time.time() > 3000


# ---------------------------------------------------------------------------
# 5. periodic 判断点の例外 → failed + 同日 backoff (翌日へ飛ばない)
# ---------------------------------------------------------------------------


def test_periodic_judgment_exception_retries_same_day(env, monkeypatch):
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK="judgment_day_open",
        SCHEDULED_DATETIME=None,
        TIME_OF_DAY=_past_time_of_day(),
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("precondition raised")

    monkeypatch.setattr(wiring, "handle_scheduled_judgment", _boom)

    env.sm.register_schedule(sid)
    fired = _entry(env, sid)
    key = _occurrence_key_of(env, fired, sid)
    # 登録された occurrence は遠い未来 (翌日のその時刻) — 手動発火で failed を作る
    _fire(env, sid)

    assert _ledger_row(env, key)["status"] == "failed"
    retry = _entry(env, sid)
    assert retry is not fired
    # backoff は ~120 秒後 = 同日。翌日の occurrence (>1h) へ飛ばない
    assert 60 < retry.fire_at_ts - time.time() < 600


# ---------------------------------------------------------------------------
# 6. 判断点 duplicate → settled_skip: 前進 + 台帳 applied/completed + 再試行なし
# ---------------------------------------------------------------------------


def test_judgment_duplicate_is_settled_skip(env, monkeypatch):
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK="judgment_day_open",
        SCHEDULED_DATETIME=None,
        TIME_OF_DAY=_past_time_of_day(),
    )
    monkeypatch.setattr(
        wiring, "handle_scheduled_judgment",
        lambda *a, **kw: {"submitted": False, "reason": "duplicate:completed"},
    )

    env.sm.register_schedule(sid)
    key = _occurrence_key_of(env, _entry(env, sid), sid)
    _fire(env, sid)

    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "completed"
    assert row["result"]["action"] == "settled_skip"
    assert row["result"]["reason"] == "duplicate:completed"
    # backoff (~120 秒) ではなく次 occurrence (>1 時間先) が登録されている
    nxt = _entry(env, sid)
    assert nxt is not None
    assert nxt.fire_at_ts - time.time() > 3600


# ---------------------------------------------------------------------------
# 7. 同一 occurrence の二重発火 → dispatch 1 回のみ
# ---------------------------------------------------------------------------


def test_double_fire_dispatches_once(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    first = _fire(env, sid)  # 成功 → completed

    assert len(env.stub.calls) == 1
    # 旧予約の残留発火 (同一 closure の再実行) → claim dedup で dispatch されない
    first.callback()
    assert len(env.stub.calls) == 1
    key = _occurrence_key_of(env, first, sid)
    assert _ledger_row(env, key)["status"] == "completed"


# ---------------------------------------------------------------------------
# 8. 旧世代 callback → dispatch 0 回 + 最新 DB で再登録
# ---------------------------------------------------------------------------


def test_stale_generation_reregisters_without_executing(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    stale = _entry(env, sid)
    key = _occurrence_key_of(env, stale, sid)

    # 設定変更を模す: SYNC_GENERATION += 1 (書き手の契約、Chunk A)
    db = env.session_factory()
    try:
        row = db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).first()
        row.SYNC_GENERATION = (row.SYNC_GENERATION or 0) + 1
        db.commit()
    finally:
        db.close()

    stale.callback()  # 旧世代 (generation=0) の発火

    assert env.stub.calls == []  # 実行されない
    assert _ledger_row(env, key) is None  # claim もしない (世代照合が先)
    # 最新世代で再登録されている
    assert env.sm._registered[sid][1] == 1
    fresh = _entry(env, sid)
    assert fresh is not None and fresh is not stale


# ---------------------------------------------------------------------------
# 9. settle は単一 commit: applied 後 commit 前の例外で全て巻き戻る
# ---------------------------------------------------------------------------


def test_settle_is_atomic_when_commit_never_happens(env, monkeypatch):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    key = _occurrence_key_of(env, _entry(env, sid), sid)

    ledger = env.manager.execution_ledger
    real_mark_applied = ledger.mark_applied

    def _applied_then_crash(*args, **kwargs):
        real_mark_applied(*args, **kwargs)  # session 上で applied 遷移まで実行
        raise RuntimeError("injected between mark_applied and commit")

    monkeypatch.setattr(ledger, "mark_applied", _applied_then_crash)

    _fire(env, sid)  # 例外は _handle_fire の外殻 try/except が吸収する

    # commit されていないので状態前進 (COMPLETED) も applied も残らない
    assert _get_schedule(env, sid).COMPLETED is False
    row = _ledger_row(env, key)
    assert row is not None and row["status"] == "running"


# ---------------------------------------------------------------------------
# 10. attempt 上限到達: oneshot は再登録なし / periodic は次 occurrence 登録
# ---------------------------------------------------------------------------


def test_oneshot_retry_exhaustion_stops_registering(env):
    sid = _add_schedule(env.session_factory)
    env.stub.result = {"action": "unavailable", "runtime_outcome": None, "error": None}
    env.sm.register_schedule(sid)

    # 初回 (attempt=0) + 再試行 MAX 回 = 計 MAX+1 回すべて失敗させる
    for _ in range(SCHEDULE_DISPATCH_MAX_ATTEMPTS + 1):
        last = _fire(env, sid)

    assert len(env.stub.calls) == SCHEDULE_DISPATCH_MAX_ATTEMPTS + 1
    # 尽きた後は再登録なし (map は発火済みの残骸のまま) — 回復は Chunk C の
    # reconciliation (60 秒周期) が拾う
    assert _entry(env, sid) is last
    assert _get_schedule(env, sid).COMPLETED is False


# ---------------------------------------------------------------------------
# 11. 初回 interval の occurrence token (Codex W3 指摘 1)
# ---------------------------------------------------------------------------


def test_interval_first_occurrence_uses_stable_token_then_epoch(env):
    """初回 interval は sentinel key "{id}:{instance}:g{N}:first"、成功後は epoch ベースに戻る。

    初回成功で LAST_EXECUTED_AT が入った後の次回 occurrence は epoch key で
    claim でき、通常どおり発火する (Codex W3 指摘 1 の回帰 (b))。
    """
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
    )
    env.sm.register_schedule(sid)
    first_key = _occurrence_key_of(env, _entry(env, sid), sid)
    assert first_key == f"{sid}:legacy:g0:first"

    _fire(env, sid)  # 初回成功 → LAST_EXECUTED_AT 設定

    assert len(env.stub.calls) == 1
    assert _ledger_row(env, first_key)["status"] == "completed"
    schedule = _get_schedule(env, sid)
    assert schedule.LAST_EXECUTED_AT is not None

    # 次回は epoch ベースの別 key で登録されている
    nxt = _entry(env, sid)
    assert nxt is not None
    next_key = _occurrence_key_of(env, nxt, sid)
    assert next_key == f"{sid}:legacy:g0:{int(nxt.fire_at_ts)}"
    assert next_key != first_key

    # 次回 key はブロックされておらず、発火すれば claim → dispatch が走る
    _fire(env, sid)
    assert len(env.stub.calls) == 2
    assert _ledger_row(env, next_key)["status"] == "completed"


# ---------------------------------------------------------------------------
# 12. SCHEDULE_ID 再利用 (削除 → 同一 id で新規作成) が新 schedule を塞がない
#     (Codex W3 第三陣)
# ---------------------------------------------------------------------------


def test_schedule_id_reuse_does_not_block_new_schedule(env):
    """実行済み行の削除 → 同一 SCHEDULE_ID での新規作成でも新行が発火する。

    SQLite は AUTOINCREMENT 無しの INTEGER PK で削除済み最大 ID を再利用しうる。
    旧鍵 {id}:{occurrence} では旧行の completed 台帳行が新行の初回 claim
    (interval なら同じ "first") を runnable=False にし、hot-loop ガードが再登録
    も抑止して新 schedule が一度も発火しないまま死ぬ。行一生トークン
    INSTANCE_TOKEN を鍵に含めることで新旧行が分離される (再現固定 (a))。
    あわせて、死んだ行を狙う残留予約はトークン照合で実行されず現在の行で
    再登録されることも確認する。
    """
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
        INSTANCE_TOKEN="tok-old",
    )
    env.sm.register_schedule(sid)
    old_key = _occurrence_key_of(env, _entry(env, sid), sid)
    assert old_key == f"{sid}:tok-old:g0:first"

    _fire(env, sid)  # 旧行の初回実行 → completed 台帳行が残る
    assert len(env.stub.calls) == 1
    assert _ledger_row(env, old_key)["status"] == "completed"
    stale = _entry(env, sid)  # 旧行の次 occurrence 予約 (closure は tok-old)
    assert stale is not None

    # 旧行を削除し、同じ SCHEDULE_ID を明示指定して新行を作る (PK 再利用の強制)
    db = env.session_factory()
    try:
        db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).delete()
        db.commit()
    finally:
        db.close()
    new_sid = _add_schedule(
        env.session_factory,
        SCHEDULE_ID=sid,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
        INSTANCE_TOKEN="tok-new",
    )
    assert new_sid == sid

    # 死んだ行を狙う残留予約の発火 → トークン照合で実行されず、現在の行で再登録
    stale.callback()
    assert len(env.stub.calls) == 1  # 実行されない
    fresh = _entry(env, sid)
    assert fresh is not None and fresh is not stale
    new_key = _occurrence_key_of(env, fresh, sid)
    assert new_key == f"{sid}:tok-new:g0:first"
    assert new_key != old_key

    # 新行の初回発火: 旧行の completed 台帳行にブロックされず claim が通る
    _fire(env, sid)
    assert len(env.stub.calls) == 2
    assert _ledger_row(env, new_key)["status"] == "completed"
    assert _get_schedule(env, sid).LAST_EXECUTED_AT is not None


def test_periodic_retry_exhaustion_registers_next_occurrence(env):
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="periodic",
        SCHEDULED_DATETIME=None,
        TIME_OF_DAY=_past_time_of_day(),
    )
    env.stub.result = {"action": "unavailable", "runtime_outcome": None, "error": None}
    env.sm.register_schedule(sid)

    for _ in range(SCHEDULE_DISPATCH_MAX_ATTEMPTS + 1):
        last = _fire(env, sid)

    assert len(env.stub.calls) == SCHEDULE_DISPATCH_MAX_ATTEMPTS + 1
    # periodic は occurrence を諦め、次回 (>1 時間先) が登録される
    nxt = _entry(env, sid)
    assert nxt is not None and nxt is not last
    assert nxt.fire_at_ts - time.time() > 3600


# ---------------------------------------------------------------------------
# 12. 実行中の設定変更を旧発火が精算しない (Codex W3 第七陣 high)
# ---------------------------------------------------------------------------


def test_midrun_reconfig_is_not_settled_onto_new_generation(env):
    """LLM 実行中にユーザーが同じ行を更新 (世代 bump) した場合、旧発火の精算は
    新設定の COMPLETED / LAST_EXECUTED_AT を書かない ((行, 世代) 条件付き
    UPDATE)。実行そのものは起きたので台帳は applied→completed で閉じ、result に
    superseded_during_run を残す。精算後の再登録は最新設定で行われる。"""
    sid = _add_schedule(env.session_factory)  # oneshot 過去時刻
    env.sm.register_schedule(sid)
    old_entry = _entry(env, sid)
    old_key = _occurrence_key_of(env, old_entry, sid)

    def _dispatch_and_reconfigure(**kwargs):
        # 実行中 (dispatch の最中) に oneshot を未来時刻へ再設定 + 世代 bump
        db = env.session_factory()
        try:
            row = db.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == sid).first()
            row.SCHEDULED_DATETIME = (
                datetime.now(timezone.utc).replace(tzinfo=None)
                + timedelta(hours=3)
            )
            row.SYNC_GENERATION = (row.SYNC_GENERATION or 0) + 1
            db.commit()
        finally:
            db.close()
        return {"action": "execute", "runtime_outcome": "completed", "error": None}

    env.manager.pulse_dispatcher.dispatch_schedule_fire = _dispatch_and_reconfigure
    old_entry.callback()

    schedule = _get_schedule(env, sid)
    assert schedule.COMPLETED is False  # 新設定の oneshot は未完了のまま
    row = _ledger_row(env, old_key)
    assert row["status"] == "completed"  # 実行の事実は台帳が持つ
    assert (row.get("result") or {}).get("superseded_during_run") is True
    # 最新設定 (未来時刻) の予約が生きている
    nxt = _entry(env, sid)
    assert nxt is not None
    assert nxt.fire_at_ts > old_entry.fire_at_ts + 3600


def test_periodic_midrun_reconfig_records_superseded(env):
    """periodic も精算フェンス (行・世代照合) を通り、実行中の設定変更を
    superseded_during_run=True で正しく記録する (Codex W3 第八陣 — oneshot/
    interval に入れたフェンスの同族横展開)。periodic は前進すべき状態を持たない
    ため、照合のみで書き込みは行わない。"""
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="periodic",
        TIME_OF_DAY=_past_time_of_day(),
        SCHEDULED_DATETIME=None,
    )
    env.sm.register_schedule(sid)
    old_entry = _entry(env, sid)
    old_key = _occurrence_key_of(env, old_entry, sid)

    def _dispatch_and_reconfigure(**kwargs):
        db = env.session_factory()
        try:
            row = db.query(PersonaSchedule).filter(
                PersonaSchedule.SCHEDULE_ID == sid).first()
            row.SYNC_GENERATION = (row.SYNC_GENERATION or 0) + 1
            db.commit()
        finally:
            db.close()
        return {"action": "execute", "runtime_outcome": "completed", "error": None}

    env.manager.pulse_dispatcher.dispatch_schedule_fire = _dispatch_and_reconfigure
    old_entry.callback()

    row = _ledger_row(env, old_key)
    assert row["status"] == "completed"
    assert (row.get("result") or {}).get("superseded_during_run") is True
