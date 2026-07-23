"""schedule reconciliation (W3 Chunk C / handoff D6) の回帰テスト。

docs/handoff/2026-07-20_w3_schedule_ledger_handoff.md D6 と
自律行動監査 (2026-07-14) A12 の「必要な回帰」:

1. register 失敗 (scheduler.schedule 例外) で予約が無い状態 → 例外を解いて
   ``_reconcile_schedules()`` 1 回 → 最新時刻へ一度だけ登録される
   (プロセス再起動なし)
2. update で SYNC_GENERATION が上がったが再 register が失敗した状態 (予約は
   旧世代のまま) → reconciliation が世代不一致を検出して再登録
   (``_registered[id]`` が新世代に)
3. delete 済み行の予約残留 → reconciliation で cancel + map から除去。
   disable も同様
4. 実行中 occurrence (find_execution が running を返す) → 再登録しない
5. unknown 裁定待ち oneshot → 再登録しない (自動再実行なし、intent §2.5)
6. prepared 残留 (claim 後 crash 相当) → 再登録される (自己回復)
7. ``_recovery_tick`` 経由で reconciliation が呼ばれる (wiring 結線の確認)

構成: manager は SimpleNamespace mock + 実 DB (in-memory SQLite + StaticPool)
+ 実 ExecutionLedger (tests/test_schedule_manager_ledger.py の流儀)。
EventScheduler は start() せず、予約 heap の ``_entries_by_key`` を直接検査
する。「予約消失」は実 dispatch と同じく map から pop して模す
(_dispatch_loop は発火前にエントリを pop する)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, PersonaSchedule, User
from saiverse import clock
from saiverse import execution_ledger_wiring as wiring
from saiverse.event_scheduler import EventScheduler
from saiverse.execution_ledger import ExecutionLedger
from saiverse.schedule_manager import (
    SCHEDULE_DISPATCH_LEDGER_KIND,
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
        event_scheduler=EventScheduler(),  # start() しない (heap 直接検査)
        execution_ledger=ExecutionLedger(session_factory=session_factory),
        pulse_dispatcher=SimpleNamespace(dispatch_schedule_fire=stub),
        _save_modified_buildings=lambda: None,
    )
    sm = ScheduleManager(saiverse_manager=manager)
    # wiring 結線テスト (7) が manager.schedule_manager を辿る
    manager.schedule_manager = sm
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


def _drop_reservation(env, schedule_id):
    """予約を map から落として「予約消失」を模す (実 dispatch の pop と同じ)。"""
    return env.manager.event_scheduler._entries_by_key.pop(
        _schedule_key(schedule_id), None
    )


def _occurrence_key_for(env, schedule_id) -> str:
    """DB の現在状態から次回 occurrence の台帳キーを計算する。

    実装と同じヘルパで組み立てる (初回 interval は "first" = Codex W3 指摘 1、
    行一生トークン = 第三陣、設定世代 g{N} = 第七陣で独立成分。直接 INSERT した
    行は NULL = "legacy")。
    """
    db = env.session_factory()
    try:
        schedule = db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id
        ).first()
        next_fire = env.sm._compute_next_fire_at(schedule, db)
        assert next_fire is not None
        return _occurrence_key(
            schedule_id,
            _instance_token(schedule),
            schedule.SYNC_GENERATION or 0,
            _occurrence_token(schedule, next_fire),
        )
    finally:
        db.close()


def _bump_generation(session_factory, schedule_id) -> int:
    """設定変更を模す (書き手の契約: SYNC_GENERATION += 1、Chunk A)。"""
    db = session_factory()
    try:
        row = db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == schedule_id
        ).first()
        row.SYNC_GENERATION = (row.SYNC_GENERATION or 0) + 1
        db.commit()
        return row.SYNC_GENERATION
    finally:
        db.close()


class _BrokenSchedule:
    """scheduler.schedule を一時的に例外化するコンテキスト。"""

    def __init__(self, scheduler):
        self.scheduler = scheduler

    def __enter__(self):
        def _boom(*args, **kwargs):
            raise RuntimeError("injected register failure")
        self.scheduler.schedule = _boom  # 束縛メソッドを instance 属性で隠す
        return self

    def __exit__(self, *exc):
        del self.scheduler.schedule  # 元のクラスメソッドに戻す
        return False


# ---------------------------------------------------------------------------
# 1. register 失敗 → reconcile 1 周で一度だけ登録 (再起動なし)
# ---------------------------------------------------------------------------


def test_register_failure_recovers_via_reconcile(env):
    sid = _add_schedule(env.session_factory)

    with _BrokenSchedule(env.manager.event_scheduler):
        with pytest.raises(RuntimeError):
            env.sm.register_schedule(sid)
    assert _entry(env, sid) is None  # 予約が無い状態 (CRUD 側は握り潰し相当)
    assert sid not in env.sm._registered

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 1, "cancelled": 0}
    assert _entry(env, sid) is not None
    assert env.sm._registered[sid][1] == 0

    # 同期済みなら 2 周目は何もしない (一度だけ登録)
    entry_before = _entry(env, sid)
    result = env.sm._reconcile_schedules()
    assert result == {"registered": 0, "cancelled": 0}
    assert _entry(env, sid) is entry_before


# ---------------------------------------------------------------------------
# 2. 世代不一致 (update 後の再 register 失敗) → reconcile が新世代で再登録
# ---------------------------------------------------------------------------


def test_generation_mismatch_triggers_reregistration(env):
    sid = _add_schedule(env.session_factory)
    assert env.sm.register_schedule(sid) == "registered"
    stale = _entry(env, sid)
    assert env.sm._registered[sid][1] == 0

    # update で世代が上がったが再 register が失敗した状態 (予約は旧世代のまま)
    new_gen = _bump_generation(env.session_factory, sid)
    assert new_gen == 1

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 1, "cancelled": 0}
    assert env.sm._registered[sid][1] == 1
    fresh = _entry(env, sid)
    assert fresh is not None and fresh is not stale


def test_id_reuse_with_same_generation_triggers_reregistration(env):
    """削除→SCHEDULE_ID 再利用→新規作成で新旧行の世代が偶然一致しても、
    行トークン (INSTANCE_TOKEN) の不一致で reconciliation が新行を登録する
    (Codex W3 第五陣 P1 の再現固定)。世代だけの照合では旧予約が「同期済み」に
    見え、新スケジュールが旧予約の発火時刻まで実行されなかった。"""
    sid = _add_schedule(
        env.session_factory, INSTANCE_TOKEN="tok-old", SYNC_GENERATION=1,
    )
    assert env.sm.register_schedule(sid) == "registered"
    stale = _entry(env, sid)
    assert env.sm._registered[sid] == ("tok-old", 1)

    # 削除 → 同一 SCHEDULE_ID で再作成 (unregister 失敗相当: 旧予約と map は残る)
    db = env.session_factory()
    try:
        db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).delete()
        db.commit()
    finally:
        db.close()
    _add_schedule(
        env.session_factory, SCHEDULE_ID=sid,
        INSTANCE_TOKEN="tok-new", SYNC_GENERATION=1,
    )

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 1, "cancelled": 0}
    assert env.sm._registered[sid] == ("tok-new", 1)
    fresh = _entry(env, sid)
    assert fresh is not None and fresh is not stale


# ---------------------------------------------------------------------------
# 3. delete / disable の予約残留 → reconcile で cancel + map から除去
# ---------------------------------------------------------------------------


def test_deleted_schedule_reservation_is_cancelled(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    assert _entry(env, sid) is not None

    db = env.session_factory()
    try:
        db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).delete()
        db.commit()
    finally:
        db.close()

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 0, "cancelled": 1}
    assert sid not in env.sm._registered
    assert not env.manager.event_scheduler.has_key(_schedule_key(sid))


def test_disabled_schedule_reservation_is_cancelled(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)

    db = env.session_factory()
    try:
        row = db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).first()
        row.ENABLED = False
        db.commit()
    finally:
        db.close()

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 0, "cancelled": 1}
    assert sid not in env.sm._registered
    assert not env.manager.event_scheduler.has_key(_schedule_key(sid))


def test_enabled_but_uncomputable_schedule_reservation_is_cancelled(env):
    """enabled のまま next_fire 計算不能になった schedule の残留予約 →
    reconcile 1 周で予約と ``_registered`` から消える (Codex W3 第二陣 P2)。

    更新で periodic → 日時未指定 oneshot 等に変えた後に register 側が失敗する
    と、旧時刻の予約が heap に残る。発火時の世代照合で空振りにはなるが、DB
    正典に無い予約が残るのは同期の欠け — reconciliation が回収する。
    """
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    assert _entry(env, sid) is not None

    # 更新で「発火時刻を計算できない設定」に変わったが再 register が失敗した
    # 状態を模す: 日時未指定 oneshot + 世代 bump、予約は旧時刻のまま残留
    db = env.session_factory()
    try:
        row = db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid
        ).first()
        row.SCHEDULED_DATETIME = None
        row.SYNC_GENERATION = (row.SYNC_GENERATION or 0) + 1
        db.commit()
    finally:
        db.close()

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 0, "cancelled": 1}
    assert sid not in env.sm._registered
    assert not env.manager.event_scheduler.has_key(_schedule_key(sid))

    # 2 周目は回収するものが無い (冪等)
    result = env.sm._reconcile_schedules()
    assert result == {"registered": 0, "cancelled": 0}


# ---------------------------------------------------------------------------
# 4. 実行中 occurrence (running) → 二重登録しない
# ---------------------------------------------------------------------------


def test_running_occurrence_is_not_reregistered(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    key = _occurrence_key_for(env, sid)

    # 発火 → 実行中 (>60s の長 Pulse) を模す: 予約は dispatch に pop され、
    # 台帳は running のまま
    _drop_reservation(env, sid)
    ledger = env.manager.execution_ledger
    exec_id, runnable, _st = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
    )
    assert runnable
    assert ledger.try_mark_running(exec_id)

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 0, "cancelled": 0}
    assert not env.manager.event_scheduler.has_key(_schedule_key(sid))


# ---------------------------------------------------------------------------
# 5. unknown 裁定待ち oneshot → 再登録しない (自動再実行なし)
# ---------------------------------------------------------------------------


def test_unknown_oneshot_is_not_reregistered(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    key = _occurrence_key_for(env, sid)

    _drop_reservation(env, sid)
    ledger = env.manager.execution_ledger
    exec_id, runnable, _st = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
    )
    assert runnable
    assert ledger.try_mark_running(exec_id)
    ledger.mark_unknown(exec_id, "runtime error")

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 0, "cancelled": 0}
    assert not env.manager.event_scheduler.has_key(_schedule_key(sid))


# ---------------------------------------------------------------------------
# 5b. 初回 interval の unknown → reconcile しても再登録されない (Codex W3 指摘 1)
# ---------------------------------------------------------------------------


def test_unknown_first_interval_is_not_reregistered(env):
    """初回 interval が runtime error → unknown の後、reconciliation を回しても
    別 epoch の key で再登録されない (Codex W3 指摘 1 の再現固定)。

    旧実装は occurrence key を int(next_fire.timestamp()) で焼き込んでいたため、
    初回 (LAST_EXECUTED_AT=None) の再計算が「現在時刻」= 別 epoch = 別 冪等
    キーを生み、unknown による自動再実行禁止 (intent §2.5) を迂回して LLM を
    再起動しえた。sentinel "first" + キーの世代成分により同一世代内でブロックが保持される
    ことを固定する。
    """
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
    )
    env.sm.register_schedule(sid)
    key = _occurrence_key_for(env, sid)
    assert key == f"{sid}:legacy:g0:first"

    # 発火 → runtime error → unknown (実 dispatch と同じく予約は pop される)
    env.stub.result = {"action": "execute", "runtime_outcome": "error", "error": "boom"}
    entry = _drop_reservation(env, sid)
    assert entry is not None
    entry.callback()

    assert len(env.stub.calls) == 1
    ledger = env.manager.execution_ledger
    row = ledger.find_execution(SCHEDULE_DISPATCH_LEDGER_KIND, key)
    assert row is not None and row["status"] == "unknown"

    # reconciliation を (時間を置いて) 何度回しても再登録されない
    for _ in range(2):
        result = env.sm._reconcile_schedules()
        assert result == {"registered": 0, "cancelled": 0}
        assert not env.manager.event_scheduler.has_key(_schedule_key(sid))
    # dispatch も増えていない (LLM 再起動なし)
    assert len(env.stub.calls) == 1


# ---------------------------------------------------------------------------
# 5c. 初回 interval の unknown 封印は設定変更 (gen bump) で解ける
#     (Codex W3 第四陣 P2)
# ---------------------------------------------------------------------------


def test_unknown_first_interval_unblocked_by_generation_bump(env):
    """初回 interval の unknown 封印は SYNC_GENERATION の前進で解ける
    (Codex W3 第四陣 P2 の再現固定)。

    純粋な sentinel "first" では update・disable→enable で世代が進んでも同じ
    冪等キーのままで、「削除して作り直す」以外に回復手段が無かった。世代付き
    キーの世代成分 g{N} により、**ユーザーの設定変更 = 新しい論理 occurrence**
    (intent execution_ledger.md §5「再実行は人間が新しい実行として起動する」の
    schedule 版) として新キーで claim が通り発火する。旧世代の unknown 行は
    裁定待ちのまま残る (歴史は消さない)。
    """
    sid = _add_schedule(
        env.session_factory,
        SCHEDULE_TYPE="interval",
        SCHEDULED_DATETIME=None,
        INTERVAL_SECONDS=3600,
        LAST_EXECUTED_AT=None,
    )
    env.sm.register_schedule(sid)
    old_key = _occurrence_key_for(env, sid)
    assert old_key == f"{sid}:legacy:g0:first"

    # 発火 → runtime error → unknown (裁定待ちの封印)
    env.stub.result = {"action": "execute", "runtime_outcome": "error", "error": "boom"}
    entry = _drop_reservation(env, sid)
    assert entry is not None
    entry.callback()
    assert len(env.stub.calls) == 1
    ledger = env.manager.execution_ledger
    row = ledger.find_execution(SCHEDULE_DISPATCH_LEDGER_KIND, old_key)
    assert row is not None and row["status"] == "unknown"

    # 同世代の reconcile は再登録しない (封印は世代内では維持 — 指摘 1 の挙動)
    assert env.sm._reconcile_schedules() == {"registered": 0, "cancelled": 0}

    # ユーザーの設定変更 (SYNC_GENERATION += 1) → reconcile が新キーで再登録
    new_gen = _bump_generation(env.session_factory, sid)
    result = env.sm._reconcile_schedules()
    assert result == {"registered": 1, "cancelled": 0}
    new_key = _occurrence_key_for(env, sid)
    assert new_key == f"{sid}:legacy:g{new_gen}:first"
    assert new_key != old_key

    # 新キーで claim が通り発火する (旧世代の unknown 行にブロックされない)
    env.stub.result = {
        "action": "execute", "runtime_outcome": "completed", "error": None,
    }
    entry = _drop_reservation(env, sid)
    assert entry is not None
    entry.callback()
    assert len(env.stub.calls) == 2
    assert ledger.find_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, new_key
    )["status"] == "completed"
    # 旧世代の unknown 行は裁定待ちのまま (自動で消えない)
    assert ledger.find_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, old_key
    )["status"] == "unknown"


# ---------------------------------------------------------------------------
# 6. prepared 残留 (claim 後 crash 相当) → 再登録される (自己回復)
# ---------------------------------------------------------------------------


def test_prepared_occurrence_is_reregistered(env):
    sid = _add_schedule(env.session_factory)
    env.sm.register_schedule(sid)
    key = _occurrence_key_for(env, sid)

    # claim (prepared) 直後の crash を模す: 予約は消え、台帳は prepared のまま
    _drop_reservation(env, sid)
    ledger = env.manager.execution_ledger
    exec_id, runnable, _st = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
    )
    assert runnable
    # try_mark_running しない = prepared のまま

    result = env.sm._reconcile_schedules()

    assert result == {"registered": 1, "cancelled": 0}
    assert env.manager.event_scheduler.has_key(_schedule_key(sid))
    # 再発火時は claim が同じ prepared 行を再利用する (二重行にならない)
    row = ledger.find_execution(SCHEDULE_DISPATCH_LEDGER_KIND, key)
    assert row is not None and row["status"] == "prepared"


# ---------------------------------------------------------------------------
# 7. _recovery_tick 経由で reconciliation が呼ばれる (wiring 結線)
# ---------------------------------------------------------------------------


def test_recovery_tick_invokes_reconciliation(env):
    sid = _add_schedule(env.session_factory)

    with _BrokenSchedule(env.manager.event_scheduler):
        with pytest.raises(RuntimeError):
            env.sm.register_schedule(sid)
    assert _entry(env, sid) is None

    wiring._recovery_tick(env.manager)

    assert env.manager.event_scheduler.has_key(_schedule_key(sid))
    assert env.sm._registered[sid][1] == 0


def test_reconcile_helper_is_noop_without_schedule_manager():
    """schedule_manager を持たない manager (テストスタブ等) では no-op。"""
    wiring._reconcile_schedules(SimpleNamespace())  # raise しないこと


# ---------------------------------------------------------------------------
# 8. schedule.dispatch の prepared 回収 (Codex W3 第六陣 P1)
#    claim → try_mark_running の間の crash で残った prepared は予約 (in-memory)
#    が消えており、reconciliation は「次回 occurrence」しか照合しない — periodic
#    の当日分など過去 occurrence は prepared 回収が唯一の再発火経路。
# ---------------------------------------------------------------------------


def _claim_prepared_occurrence(env, sid, *, instance_token="tok", occurrence="1751600000", generation=0):
    """crash 直前 (claim 済み・席取り前・予約消失) の状態を作る。"""
    ledger = env.manager.execution_ledger
    key = _occurrence_key(sid, instance_token, generation, occurrence)
    exec_id, runnable, _ = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
        payload={
            "schedule_id": sid, "persona_id": PERSONA_ID,
            "schedule_type": "periodic", "instance_token": instance_token,
            "occurrence": occurrence, "generation": generation,
            "meta_playbook": "track_user_conversation",
        },
    )
    assert runnable
    return exec_id


def _age_prepared(env, exec_id, seconds=300):
    """台帳行を seconds 秒だけ老化させる (CREATED_AT / UPDATED_AT の両方)。

    prepared 回収は CREATED_AT (claim 時刻)、failed 回収は UPDATED_AT
    (mark_failed 時刻 = 第十陣で猶予の起点) を見る。
    """
    from database.models import ExecutionLedgerEntry

    db = env.session_factory()
    try:
        row = db.query(ExecutionLedgerEntry).filter(
            ExecutionLedgerEntry.EXECUTION_ID == exec_id
        ).one()
        row.CREATED_AT = row.CREATED_AT - seconds
        row.UPDATED_AT = row.UPDATED_AT - seconds
        db.commit()
    finally:
        db.close()


def _periodic_schedule(env, **overrides):
    defaults = dict(
        SCHEDULE_TYPE="periodic", TIME_OF_DAY="09:00",
        SCHEDULED_DATETIME=None, COMPLETED=False,
        INSTANCE_TOKEN="tok", SYNC_GENERATION=0,
    )
    defaults.update(overrides)
    return _add_schedule(env.session_factory, **defaults)


def _persona_tz(env):
    """ScheduleManager が TIME_OF_DAY を解釈するタイムゾーン (ペルソナの city)。"""
    db = env.session_factory()
    try:
        return env.sm._get_persona_timezone(PERSONA_ID, db)
    finally:
        db.close()


def _next_daily_fire(tz, time_of_day: str) -> datetime:
    """毎日 `time_of_day` に発火する periodic の次回発火 (aware, ペルソナ tz)。

    ScheduleManager._next_periodic_fire (曜日指定なし = 毎日) の規則をテスト側で
    独立に組み立てる。「今から N 秒以上先」のような相対条件ではなく期待値そのもの
    を突き合わせるためのもの (相対条件はテストを回す壁時計に依存する)。
    """
    hour, minute = (int(part) for part in time_of_day.split(":"))
    local_now = datetime.now(timezone.utc).astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def test_prepared_schedule_dispatch_is_refired(env):
    """当日分 (過去 occurrence) の prepared が回収 tick で再発火予約され、
    発火 callback の claim が同じ prepared 行を再利用して一度だけ実行される。"""
    sid = _periodic_schedule(env)
    exec_id = _claim_prepared_occurrence(env, sid)
    _age_prepared(env, exec_id)
    assert _entry(env, sid) is None  # 予約は crash で消えている

    wiring._collect_prepared_schedule_dispatch(env.manager)

    entry = _entry(env, sid)
    assert entry is not None
    entry.callback()  # 再発火 → claim が prepared 再利用 → dispatch 1 回
    assert len(env.stub.calls) == 1
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "completed"


def test_prepared_schedule_dispatch_young_rows_left_alone(env):
    """待機秒数未満の prepared は触らない (発火直後の正常経路と衝突しない)。"""
    sid = _periodic_schedule(env)
    exec_id = _claim_prepared_occurrence(env, sid)

    wiring._collect_prepared_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "prepared"


def test_prepared_schedule_dispatch_superseded_generation_is_failed(env):
    """設定変更 (世代 bump) 済みの prepared は failed に落とし再発火しない —
    ユーザーの設定変更 = 新しい論理 occurrence (旧 occurrence は放棄が正)。"""
    sid = _periodic_schedule(env)
    exec_id = _claim_prepared_occurrence(env, sid, generation=0)
    _age_prepared(env, exec_id)
    _bump_generation(env.session_factory, sid)

    wiring._collect_prepared_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "failed"


def test_prepared_schedule_dispatch_removed_schedule_is_failed(env):
    """schedule 行が消えた prepared は failed 終端 (孤児の永久残留を防ぐ)。"""
    sid = _periodic_schedule(env)
    exec_id = _claim_prepared_occurrence(env, sid)
    _age_prepared(env, exec_id)
    db = env.session_factory()
    try:
        db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid).delete()
        db.commit()
    finally:
        db.close()

    wiring._collect_prepared_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "failed"


def test_prepared_schedule_dispatch_manual_mode_persona_is_skipped(env):
    """完全手動モードのペルソナは refire しない (prepared のまま残し、解除後の
    tick が拾う) — _collect_prepared_judgments と同じ規律。"""
    sid = _periodic_schedule(env)
    exec_id = _claim_prepared_occurrence(env, sid)
    _age_prepared(env, exec_id)
    env.manager._debug_manual_mode_personas = {PERSONA_ID}

    wiring._collect_prepared_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "prepared"


# ---------------------------------------------------------------------------
# 9. 設定世代がキーの独立成分 (Codex W3 第七陣 high)
# ---------------------------------------------------------------------------


def test_generation_bump_unblocks_unknown_oneshot(env):
    """unknown で封印された oneshot は、日時を変えない設定変更 (世代 bump)
    でも新しい論理 occurrence として再登録・発火できる (世代がキーの独立成分に
    なったことの回帰)。同一世代内では封印が保持される。"""
    sid = _add_schedule(env.session_factory)
    assert env.sm.register_schedule(sid) == "registered"
    env.stub.result = {"action": "execute", "runtime_outcome": "error",
                       "error": "boom"}
    _entry(env, sid).callback()  # runtime error → unknown
    assert len(env.stub.calls) == 1
    _drop_reservation(env, sid)  # 実 dispatch は発火前に pop 済み

    # 同一世代: reconcile しても封印は保持される
    assert env.sm._reconcile_schedules() == {"registered": 0, "cancelled": 0}

    # 世代 bump (日時は不変) = 新しい論理 occurrence → 再登録・発火できる
    _bump_generation(env.session_factory, sid)
    env.stub.result = {"action": "execute", "runtime_outcome": "completed",
                       "error": None}
    assert env.sm._reconcile_schedules() == {"registered": 1, "cancelled": 0}
    _entry(env, sid).callback()
    assert len(env.stub.calls) == 2
    db = env.session_factory()
    try:
        assert db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid).first().COMPLETED is True
    finally:
        db.close()


def test_generation_bump_expression_survives_interleaved_sessions(env):
    """世代 bump はサーバー側インクリメント (SQL 式)。二つの Session が同じ行を
    読んでから両方 commit しても世代が重複しない (Codex W3 第七陣 high の機構
    固定 — read-modify-write だと両方が同じ番号を書き、reconciliation が
    「予約 = DB 世代」の偽同期判定をする)。書き手 3 箇所 (schedule.py の
    toggle/update / life_settings の _upsert_day_row) はこの式を使う契約。"""
    from sqlalchemy import func

    sid = _add_schedule(env.session_factory, SYNC_GENERATION=0)
    s1 = env.session_factory()
    s2 = env.session_factory()
    try:
        r1 = s1.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid).first()
        r2 = s2.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid).first()
        assert (r1.SYNC_GENERATION, r2.SYNC_GENERATION) == (0, 0)  # 両方が旧値を読む
        r1.SYNC_GENERATION = func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) + 1
        s1.commit()
        r2.SYNC_GENERATION = func.coalesce(PersonaSchedule.SYNC_GENERATION, 0) + 1
        s2.commit()
    finally:
        s1.close()
        s2.close()

    db = env.session_factory()
    try:
        assert db.query(PersonaSchedule).filter(
            PersonaSchedule.SCHEDULE_ID == sid).first().SYNC_GENERATION == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 10. failed periodic の回収 (Codex W3 第八陣 — prepared 回収の failed 版)
# ---------------------------------------------------------------------------


def _fail_periodic_occurrence(env, sid, *, occurrence=None, attempt=0, age=400):
    """mark_failed 後・backoff 予約前に crash した状態 (failed 行のみ)。"""
    ledger = env.manager.execution_ledger
    if occurrence is None:
        occurrence = str(int(datetime.now(timezone.utc).timestamp()) - 600)
    key = _occurrence_key(sid, "tok", 0, occurrence)
    exec_id, runnable, _ = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
        payload={
            "schedule_id": sid, "persona_id": PERSONA_ID,
            "schedule_type": "periodic", "instance_token": "tok",
            "occurrence": occurrence, "generation": 0,
            "meta_playbook": "track_user_conversation",
            "attempt": attempt,
        },
    )
    assert runnable
    assert ledger.try_mark_running(exec_id)
    ledger.mark_failed(exec_id, "dispatch failed")
    if age:
        _age_prepared(env, exec_id, seconds=age)  # CREATED_AT を猶予より古く
    return exec_id


def test_failed_periodic_without_reservation_is_refired(env):
    """failed 行だけ残して retry 予約 (揮発) を失った periodic は、回収 tick が
    同一 occurrence を再発火させる — claim が failed キーを退避して一度だけ
    実行される (Codex W3 第八陣の再現固定)。"""
    sid = _periodic_schedule(env)
    exec_id = _fail_periodic_occurrence(env, sid)
    assert _entry(env, sid) is None

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    entry = _entry(env, sid)
    assert entry is not None
    entry.callback()
    assert len(env.stub.calls) == 1
    # 旧 failed 行はキー退避済み・新実行が completed
    ledger = env.manager.execution_ledger
    assert ledger.get_execution(exec_id)["status"] == "failed"
    assert "#failed-" in (ledger.get_execution(exec_id)["idempotency_key"] or "")


def test_failed_periodic_recovered_after_restart_with_next_reservation(env):
    """**再起動経路の再現** (Codex W3 第九陣 P1 の固定): 再起動後は
    `schedule_manager.start()` が翌回の予約を先に登録するが、crash で retry を
    失った当日分の failed は行齢 + attempt 判定で回収され、翌回予約を同一 key で
    上書きして当日 occurrence を発火する。精算後は翌回が再登録される。"""
    sid = _periodic_schedule(env)
    exec_id = _fail_periodic_occurrence(env, sid)
    # 再起動相当: start() が翌回 occurrence の予約を登録済み
    assert env.sm.register_schedule(sid) == "registered"
    next_entry = _entry(env, sid)
    assert next_entry is not None

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    refire_entry = _entry(env, sid)
    assert refire_entry is not None and refire_entry is not next_entry  # 上書きされた
    refire_entry.callback()
    assert len(env.stub.calls) == 1  # 当日 occurrence が実行された
    ledger = env.manager.execution_ledger
    assert "#failed-" in (ledger.get_execution(exec_id)["idempotency_key"] or "")
    assert _entry(env, sid) is not None  # 精算後、翌回が再登録されている


def test_failed_periodic_young_row_is_left_alone(env):
    """行齢が猶予未満 (backoff 連鎖がまだ生きている可能性) の failed は触らない。"""
    sid = _periodic_schedule(env)
    exec_id = _fail_periodic_occurrence(env, sid, age=0)  # いま失敗したばかり

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.stub.calls == []
    assert env.manager.execution_ledger.get_execution(exec_id)["status"] == "failed"


def test_failed_periodic_exhausted_attempts_is_abandoned(env):
    """payload の attempt が上限 = 最終試行の失敗 (意図的放棄) は回収しない。"""
    from saiverse.schedule_manager import SCHEDULE_DISPATCH_MAX_ATTEMPTS

    sid = _periodic_schedule(env)
    _fail_periodic_occurrence(env, sid, attempt=SCHEDULE_DISPATCH_MAX_ATTEMPTS)

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.stub.calls == []


def test_failed_periodic_stale_occurrence_is_abandoned(env):
    """occurrence が 24h より古い failed は回収しない (「当日中の retry」の
    意味論 — 長時間停止後に古い判断を発火させない)。"""
    sid = _periodic_schedule(env)
    old = str(int(datetime.now(timezone.utc).timestamp()) - 90000)  # 25h 前
    _fail_periodic_occurrence(env, sid, occurrence=old)

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.stub.calls == []


def test_failed_periodic_refire_carries_next_attempt(env):
    """回収 refire は「失敗した試行 + 1」で再開する (Codex W3 第十陣 — 既定 0
    リセットは crash 窓の繰り返しで backoff 上限を実質無制限にする)。attempt=2
    の失敗を回収 → attempt=3 で一度だけ走り、失敗しても backoff 再試行は
    積まれない (上限到達 → 翌回登録へ)。"""
    from saiverse.schedule_manager import SCHEDULE_DISPATCH_MAX_ATTEMPTS

    tz = _persona_tz(env)
    # TIME_OF_DAY は「今から 6 時間後」に置く。ヘルパ既定の 09:00 固定だと、
    # 08:00-09:00 (ペルソナ tz) にテストを回したとき次回発火が 1 時間以内に来て、
    # backoff (今+120秒) と「遠い未来」の区別が壁時計次第になる。
    time_of_day = (
        datetime.now(timezone.utc).astimezone(tz) + timedelta(hours=6)
    ).strftime("%H:%M")
    sid = _periodic_schedule(env, TIME_OF_DAY=time_of_day)
    _fail_periodic_occurrence(env, sid, attempt=SCHEDULE_DISPATCH_MAX_ATTEMPTS - 1)

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    entry = _entry(env, sid)
    assert entry is not None
    env.stub.result = {"action": "unavailable", "runtime_outcome": None,
                       "error": None}  # 再試行も失敗させる
    entry.callback()

    # 上限到達 → backoff (今+120秒) ではなく翌回 (定期の次回発火そのもの) が登録される
    nxt = _entry(env, sid)
    assert nxt is not None
    expected_ts = _next_daily_fire(tz, time_of_day).timestamp()
    assert nxt.fire_at_ts == pytest.approx(expected_ts, rel=0, abs=1.0)


def test_failed_periodic_fresh_failure_after_long_dispatch_is_left_alone(env):
    """猶予の起点は UPDATED_AT (失敗時刻)。claim (CREATED_AT) から時間が経って
    いても失敗直後なら回収しない — 長い dispatch の失敗で生存中の backoff 予約を
    奪わない (Codex W3 第十陣)。"""
    from database.models import ExecutionLedgerEntry

    sid = _periodic_schedule(env)
    exec_id = _fail_periodic_occurrence(env, sid, age=0)  # 失敗したばかり
    db = env.session_factory()
    try:
        row = db.query(ExecutionLedgerEntry).filter(
            ExecutionLedgerEntry.EXECUTION_ID == exec_id).one()
        row.CREATED_AT = row.CREATED_AT - 600  # claim は 10 分前 (長い dispatch)
        db.commit()
    finally:
        db.close()

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.stub.calls == []


def test_failed_periodic_without_attempt_field_is_not_collected(env):
    """payload に attempt が無い / 不正な failed は自動回収しない (出所不明の
    行を勝手に再発火させない — Codex W3 第十陣)。"""
    ledger = env.manager.execution_ledger
    sid = _periodic_schedule(env)
    occurrence = str(int(datetime.now(timezone.utc).timestamp()) - 600)
    key = _occurrence_key(sid, "tok", 0, occurrence)
    exec_id, runnable, _ = ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND, key, persona_id=PERSONA_ID,
        payload={
            "schedule_id": sid, "persona_id": PERSONA_ID,
            "schedule_type": "periodic", "instance_token": "tok",
            "occurrence": occurrence, "generation": 0,
            "meta_playbook": "track_user_conversation",
            # attempt 欠落
        },
    )
    assert runnable and ledger.try_mark_running(exec_id)
    ledger.mark_failed(exec_id, "dispatch failed")
    _age_prepared(env, exec_id)

    wiring._collect_failed_periodic_schedule_dispatch(env.manager)

    assert _entry(env, sid) is None
    assert env.stub.calls == []
