"""自律行動の本番配線 (saiverse/autonomy_wiring.py) のテスト。

活性化配線の検証項目:

- fire_judgment_point: 自律 ON ゲート / Playbook 欠如の WARNING スキップ /
  precondition (Lock 下の再評価) / 実行台帳の重複抑止 (on_event — 残る唯一の
  判断点)
- ScheduleManager の経路分岐: 起床・就寝の行は機械の帳簿処理
  (day_plan.handle_scheduled_life_boundary) へ、判断点の名前空間の行は
  handle_scheduled_judgment の拒否へ (v04 段 1-2 / 1-4)
- handle_conversation_end: 会話終了 → 会話状態を落とす帳簿処理
  (会話終了判断は autonomous_behavior_v3.md §8/§13.3 で退役)
- handle_external_event: on_event 判断の経路判断基準 (自律 ON / 会話中 /
  engage_now の応対起動 / フォールバック)
- watchdog_tick: 正常時 no-op / 当日のライフ欠如時のみ起床の帳簿処理を
  再発火 (時間割のコマ予約の見張りは段 1-4 で撤去)
- 旧経路の停止: pulse_scheduler モジュールと dispatch_subline_poll の不在、
  dispatch_autonomy_tick の watchdog 縮退
"""
from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, Playbook, PersonaSchedule, User
from saiverse import autonomy_wiring as wiring
from saiverse import clock
from saiverse import day_plan
from saiverse import execution_ledger as XL
from saiverse.event_scheduler import EventScheduler
from saiverse.meta_layer import MetaLayer

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"


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


class RecordingPulseController:
    """submit_meta_judgment の呼び出しを記録するだけのフェイク。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def submit_meta_judgment(self, persona_id, building_id, meta_playbook,
                             args=None, event_callback=None):
        self.calls.append({
            "persona_id": persona_id,
            "building_id": building_id,
            "meta_playbook": meta_playbook,
            "args": args,
        })
        return None


class FakeTrackManager:
    def __init__(self):
        self.tracks: Dict[str, Any] = {}
        self.running: Optional[Any] = None

    def get(self, track_id):
        track = self.tracks.get(track_id)
        if track is None:
            raise KeyError(track_id)
        return track

    def get_running(self, persona_id):
        return self.running

    def list_for_persona(self, persona_id, statuses=None):
        return []


def _seed_db(session_factory) -> None:
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


def _import_judgment_playbooks(session_factory, names=None) -> None:
    names = names or list(wiring.JUDGMENT_PLAYBOOK_NAMES)
    db = session_factory()
    try:
        for name in names:
            db.add(Playbook(name=name, schema_json="{}", nodes_json="{}"))
        db.commit()
    finally:
        db.close()


def _make_manager(session_factory, *, active=True, with_playbooks=True):
    _seed_db(session_factory)
    if with_playbooks:
        _import_judgment_playbooks(session_factory)
    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        persona_name="Alice",
        current_building_id="alice_room",
        private_room_id="alice_room",
        autonomy_enabled=active,
    )
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        buildings=[SimpleNamespace(building_id="library", name="図書館")],
        event_scheduler=EventScheduler(),  # start() しない (テストは同期駆動)
        track_manager=FakeTrackManager(),
        pulse_controller=RecordingPulseController(),
        _autonomy_managers={},
    )
    # 判断点の直列化 Lock は本番 manager が無条件に持つ MetaLayer から取る
    # (autonomy_wiring._judgment_lock は無 Lock へ倒さない)。
    manager.meta_layer = MetaLayer(manager)
    return manager, persona


def _add_day_schedule(session_factory, playbook_name, time_of_day,
                      params=None, days_of_week=None, enabled=True):
    import json as _json

    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID,
            SCHEDULE_TYPE="periodic",
            META_PLAYBOOK=playbook_name,
            ENABLED=enabled,
            TIME_OF_DAY=time_of_day,
            DAYS_OF_WEEK=_json.dumps(days_of_week) if days_of_week else None,
            PLAYBOOK_PARAMS=_json.dumps(params) if params else None,
        ))
        db.commit()
    finally:
        db.close()


def _write_legacy_lives(session_factory, plan_date, lives) -> None:
    """旧置き場 persona_day_plan.meta_json.lives を直接書く (旧データの再現)。

    時間割の書き手は段 1-4 で撤去したので、互換読みの検証用に行を直接作る。
    """
    import json as _json

    from database.models import PersonaDayPlan

    now = datetime(2026, 7, 4, 6, 0, 0)
    db = session_factory()
    try:
        db.add(PersonaDayPlan(
            persona_id=PERSONA_ID, plan_date=plan_date, slots_json="[]",
            meta_json=_json.dumps({day_plan.META_LIVES: lives}),
            created_at=now, updated_at=now,
        ))
        db.commit()
    finally:
        db.close()


#: fire_judgment_point の検査で撃つ判断点 (残る唯一の判断点 on_event)。同じ
#: 刺激の ID なので、冪等キーも同じになる。
_EVENT = {"event_text": "来客", "stimulus_id": "test:stimulus:1"}


# ---------------------------------------------------------------------------
# fire_judgment_point: 本番ゲート
# ---------------------------------------------------------------------------


def test_fire_judgment_point_skips_non_active_persona(session_factory):
    manager, _ = _make_manager(session_factory, active=False)
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert result["submitted"] is False
    assert result["reason"] == "persona autonomy disabled"
    assert manager.pulse_controller.calls == []


def test_fire_judgment_point_skips_when_playbook_missing(session_factory, caplog):
    manager, _ = _make_manager(session_factory, with_playbooks=False)
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        result = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert result["submitted"] is False
    assert result["reason"] == "playbook not imported"
    assert manager.pulse_controller.calls == []
    # 運用手順 (import_playbook) がログに出る
    assert any("import_playbook" in r.message for r in caplog.records)


def test_fire_judgment_point_dispatches_when_gates_pass(session_factory):
    manager, _ = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert result["submitted"] is True
    assert [c["meta_playbook"] for c in manager.pulse_controller.calls] == [
        "judgment_on_event",
    ]
    call = manager.pulse_controller.calls[0]
    assert call["persona_id"] == PERSONA_ID
    assert call["building_id"] == "alice_room"
    assert "situation_text" in call["args"]
    assert "response_schema" in call["args"]


def test_fire_judgment_point_precondition_rechecked(session_factory):
    manager, _ = _make_manager(session_factory)
    result = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", _EVENT, precondition=lambda: False,
    )
    assert result["submitted"] is False
    assert result["reason"] == "precondition not met"
    assert manager.pulse_controller.calls == []


def test_fire_judgment_point_uses_meta_layer_lock(session_factory):
    """MetaLayer の per-persona Lock を共有して直列化する (取得の証跡)。"""
    manager, _ = _make_manager(session_factory)
    acquired: List[str] = []

    class _Lock:
        def __enter__(self):
            acquired.append("enter")

        def __exit__(self, *exc):
            acquired.append("exit")
            return False

    manager.meta_layer = SimpleNamespace(_get_lock=lambda pid: _Lock())
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert result["submitted"] is True
    assert acquired == ["enter", "exit"]


# ---------------------------------------------------------------------------
# fire_judgment_point × 実行台帳 (W1 Chunk A: A2 の重複抑止)
# ---------------------------------------------------------------------------


def _attach_ledger(manager, session_factory):
    manager.execution_ledger = XL.ExecutionLedger(session_factory)
    return manager.execution_ledger


class FinalizingPulseController(RecordingPulseController):
    """finalize 相当 (mark_applied) まで進める tracked テスト用フェイク。

    Chunk B 以降、finalize が mark_applied を呼ばずに戻る tracked 実行は
    unknown + submitted=False になる (証跡ベース成功判定)。tracked の正常系を
    検証するテストは、args の judgment_context に同乗した execution_id を
    finalize と同じように applied へ遷移させる必要がある。
    """

    def __init__(self, ledger):
        super().__init__()
        self._ledger = ledger

    def submit_meta_judgment(self, persona_id, building_id, meta_playbook,
                             args=None, event_callback=None):
        super().submit_meta_judgment(
            persona_id, building_id, meta_playbook,
            args=args, event_callback=event_callback,
        )
        import json as _json

        ctx = _json.loads((args or {}).get("judgment_context") or "{}")
        eid = ctx.get("execution_id")
        if eid:
            self._ledger.mark_applied(eid, result={"finalized": True})
        return None


def _attach_finalizing_ledger(manager, session_factory):
    """台帳 + finalize 相当まで進める pulse_controller を取り付ける。"""
    ledger = _attach_ledger(manager, session_factory)
    manager.pulse_controller = FinalizingPulseController(ledger)
    return ledger


def test_fire_on_event_dedup_without_then_with_precondition(session_factory, monkeypatch):
    """A2: 同じ冪等キーの判断点は 2 回目が duplicate になり submit は 1 回だけ。

    2 回目 (precondition 付き) は claim で duplicate になり、precondition の
    評価に到達しない。冪等キーは刺激の ID から作られる。fire_judgment_point は
    ライフ (確定・節目) に一切触れない。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    first = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert first["submitted"] is True
    assert first["execution_id"] is not None
    assert len(manager.pulse_controller.calls) == 1
    # 判断点はライフを確定しない (確定は起床の帳簿処理だけが行う)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    # Chunk B: finalize (フェイクが代行) の mark_applied 証跡で成功と判定
    assert ledger.get_execution(first["execution_id"])["status"] == XL.STATUS_APPLIED

    precondition_evals: List[bool] = []

    def _precondition():
        precondition_evals.append(True)
        return True

    second = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", _EVENT, precondition=_precondition,
    )
    assert second["submitted"] is False
    assert second["reason"] == f"duplicate:{XL.STATUS_APPLIED}"
    assert second["execution_id"] == first["execution_id"]
    # 判断 Pulse は増えず、precondition も走っていない
    assert len(manager.pulse_controller.calls) == 1
    assert precondition_evals == []


def test_fire_on_event_dedup_with_then_without_precondition(session_factory, monkeypatch):
    """A2: precondition 付き → 無しの逆順でも submit は 1 回だけ (汎用の重複抑止)。"""
    manager, _ = _make_manager(session_factory)
    _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    first = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", _EVENT, precondition=lambda: True,
    )
    assert first["submitted"] is True
    assert len(manager.pulse_controller.calls) == 1

    second = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert second["submitted"] is False
    assert second["reason"].startswith("duplicate:")
    assert len(manager.pulse_controller.calls) == 1


def test_fire_precondition_rejection_marks_failed_and_next_claim_runs(
    session_factory,
):
    """precondition 却下は claim 済みの席を failed に落とし、次の fire は
    failed キー退避で再び走れる (D2/D3)。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    rejected = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", _EVENT, precondition=lambda: False,
    )
    assert rejected["submitted"] is False
    assert rejected["reason"] == "precondition not met"
    assert ledger.get_execution(rejected["execution_id"])["status"] == XL.STATUS_FAILED
    assert manager.pulse_controller.calls == []

    retried = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert retried["submitted"] is True
    assert retried["execution_id"] != rejected["execution_id"]
    assert len(manager.pulse_controller.calls) == 1


def test_fire_precondition_exit_does_not_break_a_running_winner(session_factory):
    """入口側の離脱は prepared 限定 CAS — 勝者の running 台帳を壊さない。

    claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重 claim は
    同じ execution_id を両方へ返す。敗者側の precondition 離脱が無条件
    mark_failed だと、勝者が LLM 実行中の running 台帳を failed に上書きし、
    finalize の applied 遷移が失敗して結果の証跡が消える
    (docs/issues/judgment_seat_contention_and_event_loss.md ②)。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    stolen: List[str] = []

    def _steal_seat_then_reject():
        # Lock 下・claim 後に走る precondition を「もう一人の claimant が同じ
        # 席を先に running へ進めた」瞬間の再現に使う
        rows = ledger.list_prepared("judgment.")
        assert rows, "claim 済みの prepared 行があるはず"
        eid = rows[0]["execution_id"]
        assert ledger.try_mark_running(eid)
        stolen.append(eid)
        return False

    result = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", _EVENT, precondition=_steal_seat_then_reject,
    )
    assert result["submitted"] is False
    # 席を放棄できなかった = 結末不明 (呼び出し側は代替経路を走らせない)
    assert result["outcome"] == wiring.OUTCOME_INDETERMINATE
    # 勝者の running 台帳は無傷
    assert ledger.get_execution(stolen[0])["status"] == XL.STATUS_RUNNING


def test_fire_force_bypasses_idempotency_key(session_factory):
    """force=True はキーを None に落とし、debug 明示発火が duplicate に阻まれない。"""
    manager, _ = _make_manager(session_factory)
    _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    first = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert first["submitted"] is True
    forced = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT, force=True)
    assert forced["submitted"] is True
    assert forced["execution_id"] != first["execution_id"]
    assert len(manager.pulse_controller.calls) == 2


def test_fire_without_ledger_degrades_with_single_warning(session_factory, caplog):
    """台帳の無い manager (旧テストスタブ) は WARN 一回で従来挙動に degrade する。"""
    wiring._LEDGER_MISSING_WARNED.discard(PERSONA_ID)
    manager, _ = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        r1 = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
        r2 = wiring.fire_judgment_point(manager, PERSONA_ID, "on_event", _EVENT)
    assert r1["submitted"] is True
    assert r2["submitted"] is True  # 台帳なし = dedup も無し (従来挙動)
    assert r1["execution_id"] is None
    warns = [m for m in caplog.messages if "no execution_ledger" in m]
    assert len(warns) == 1


# ---------------------------------------------------------------------------
# handle_scheduled_judgment + ScheduleManager 経路分岐
# ---------------------------------------------------------------------------


def test_scheduled_judgment_refuses_retired_judgment_names(session_factory, monkeypatch, caplog):
    """退役した判断点の名前 (起床・就寝・セッション終了) が拒否口へ届いても、
    LLM の判断へ流さず WARNING で拒否する (v04 段 1-4)。起床・就寝の行は本番では
    ScheduleManager が先に機械の帳簿処理へ振り分ける。"""
    manager, _ = _make_manager(session_factory)
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append((kind, context))
        or {"submitted": True},
    )
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        for name in (
            "judgment_day_open", "judgment_day_close", "judgment_post_session",
        ):
            result = wiring.handle_scheduled_judgment(
                manager, PERSONA_ID, name, params={"daily_budget_pulses": 24},
            )
            assert result["submitted"] is False
            assert result["reason"] == "not a judgment playbook"
    assert fired == []
    assert manager.pulse_controller.calls == []
    assert sum("retired or unknown" in m for m in caplog.messages) == 3


def test_life_settings_from_params_picks_only_life_settings():
    """起床行の PLAYBOOK_PARAMS からライフ確定に効く値だけを拾う
    (daily_budget_pulses / life_mode_override)。LIFE_MODES 外の値・非正の予算・
    時間割用の daily_budget_rounds は落とす (書ける口をなくす、life.md v0.5 §3)。"""
    assert day_plan.life_settings_from_params({
        "daily_budget_pulses": 24, "life_mode_override": "even",
        "daily_budget_rounds": 30, "other": "x",
    }) == {"daily_budget_pulses": 24, "life_mode_override": "even"}
    assert day_plan.life_settings_from_params({
        "daily_budget_pulses": 0, "life_mode_override": "bogus",
    }) == {}
    assert day_plan.life_settings_from_params({"daily_budget_pulses": True}) == {}
    assert day_plan.life_settings_from_params(None) == {}


def test_scheduled_judgment_rejects_non_schedulable_kind(session_factory, caplog):
    manager, _ = _make_manager(session_factory)
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        result = wiring.handle_scheduled_judgment(
            manager, PERSONA_ID, "judgment_on_event",
        )
    assert result["submitted"] is False
    assert result["reason"] == "kind not schedulable"
    assert manager.pulse_controller.calls == []


def _routing_schedule(schedule_id, playbook, time_of_day, params=None):
    import json as _json

    return PersonaSchedule(
        SCHEDULE_ID=schedule_id,
        PERSONA_ID=PERSONA_ID,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK=playbook,
        ENABLED=True,
        TIME_OF_DAY=time_of_day,
        PLAYBOOK_PARAMS=_json.dumps(params) if params is not None else None,
    )


def test_schedule_manager_routes_wake_and_close_to_life_bookkeeping(
    session_factory, monkeypatch,
):
    """ScheduleManager._execute_schedule は起床・就寝の行を判断点ではなく
    機械の帳簿処理 (day_plan.handle_scheduled_life_boundary) へ流す。
    判断点の入口 (handle_scheduled_judgment) も汎用の dispatch も通らない。"""
    from saiverse.schedule_manager import ScheduleManager

    manager, _ = _make_manager(session_factory)
    routed: List[Any] = []
    monkeypatch.setattr(
        day_plan, "handle_scheduled_life_boundary",
        lambda mgr, pid, boundary, params=None: routed.append((pid, boundary, params))
        or True,
    )
    judged: List[Any] = []
    monkeypatch.setattr(
        wiring, "handle_scheduled_judgment",
        lambda *a, **kw: judged.append(a) or {"submitted": True},
    )
    dispatched: List[Any] = []
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: dispatched.append(kw),
    )
    manager.all_personas = manager.personas

    sm = ScheduleManager(saiverse_manager=manager)
    opened = sm._execute_schedule(
        _routing_schedule(1, "judgment_day_open", "08:00", {"daily_budget_pulses": 30}),
        session=None,
    )
    closed = sm._execute_schedule(
        _routing_schedule(2, "judgment_day_close", "22:00"), session=None,
    )

    assert routed == [
        (PERSONA_ID, "start", {"daily_budget_pulses": 30}),
        (PERSONA_ID, "end", None),
    ]
    assert judged == []
    assert dispatched == []  # 通常の submit_schedule 経路は通らない
    assert opened[0] == "executed"
    assert closed[0] == "executed"


def test_schedule_manager_life_bookkeeping_failure_is_retryable(
    session_factory, monkeypatch,
):
    """帳簿処理の失敗 (False / 例外) は failed — backoff 再試行に乗る。"""
    from saiverse.schedule_manager import ScheduleManager

    manager, _ = _make_manager(session_factory)
    sm = ScheduleManager(saiverse_manager=manager)
    schedule = _routing_schedule(1, "judgment_day_open", "08:00")

    monkeypatch.setattr(
        day_plan, "handle_scheduled_life_boundary",
        lambda *a, **kw: False,
    )
    assert sm._execute_schedule(schedule, session=None)[0] == "failed"

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(day_plan, "handle_scheduled_life_boundary", _boom)
    assert sm._execute_schedule(schedule, session=None)[0] == "failed"


def test_schedule_manager_routes_other_judgment_names_to_the_refusal(
    session_factory, monkeypatch,
):
    """起床・就寝以外の判断点 Playbook の名前空間の行は拒否口へ流れ、
    settled_skip (前進・再試行なし) になる。退役した名前 (judgment_post_session)
    も汎用経路へ流さない — 流すと「Playbook が無い」失敗の再試行を繰り返す。"""
    from saiverse.schedule_manager import ScheduleManager

    manager, _ = _make_manager(session_factory)
    sm = ScheduleManager(saiverse_manager=manager)
    outcome = sm._execute_schedule(
        _routing_schedule(3, "judgment_on_event", "08:00"), session=None,
    )
    assert outcome == ("settled_skip", "kind not schedulable")
    retired = sm._execute_schedule(
        _routing_schedule(4, "judgment_post_session", "08:00"), session=None,
    )
    assert retired == ("settled_skip", "not a judgment playbook")
    assert manager.pulse_controller.calls == []


def test_schedule_manager_normal_playbooks_untouched(session_factory, monkeypatch):
    """判断点でない META_PLAYBOOK は従来どおり dispatch_schedule_fire に流れる。"""
    from saiverse.schedule_manager import ScheduleManager

    manager, _ = _make_manager(session_factory)
    routed: List[Any] = []
    monkeypatch.setattr(
        wiring, "handle_scheduled_judgment",
        lambda *a, **kw: routed.append(a) or {"submitted": True},
    )
    dispatched: List[Any] = []
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: dispatched.append(kw) or {
            "action": "execute", "runtime_outcome": "completed", "error": None,
        },
    )
    manager.all_personas = manager.personas
    manager._save_modified_buildings = lambda: None
    manager.personas[PERSONA_ID]._save_session_metadata = lambda: None

    sm = ScheduleManager(saiverse_manager=manager)
    schedule = PersonaSchedule(
        SCHEDULE_ID=2,
        PERSONA_ID=PERSONA_ID,
        SCHEDULE_TYPE="interval",
        META_PLAYBOOK="track_user_conversation",
        ENABLED=True,
        INTERVAL_SECONDS=3600,
    )
    db = session_factory()
    try:
        outcome = sm._execute_schedule(schedule, session=db)
    finally:
        db.close()

    assert routed == []
    assert len(dispatched) == 1
    assert dispatched[0]["meta_playbook"] == "track_user_conversation"
    assert outcome[0] == "executed"


# ---------------------------------------------------------------------------
# 会話終了 (wait_response タイムアウト) の帳簿処理
#
# 会話終了判断 (post_conversation) は 2026-08-16 の裁定で退役した
# (autonomous_behavior_v3.md §8/§13.3)。残るのは「会話状態を落とす」だけ。
# 器は束 6c (2026-08-22) に出来事の行からメモリ内状態へ移った (同 §7)。
# ---------------------------------------------------------------------------


def _open_conversation(manager):
    from saiverse import user_conversation as uc

    return uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room",
        participants=[PERSONA_ID, "1"],
    )


def test_conversation_end_clears_the_conversation_state(session_factory, monkeypatch):
    from saiverse import user_conversation as uc

    manager, _persona = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 15, 0, 0))
    conv = _open_conversation(manager)

    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append(kind)
        or {"submitted": True},
    )
    result = wiring.handle_conversation_end(manager, PERSONA_ID)
    # 判断は撃たれない (退役済み) — 帳簿処理だけが起きる
    assert fired == []
    assert result["closed"] is True
    assert result["conversation_id"] == conv["conversation_id"]
    assert uc.get_open_conversation(manager, PERSONA_ID) is None


def test_conversation_end_without_an_open_conversation_is_a_no_op(
    session_factory, monkeypatch,
):
    """閉じるべき会話が無ければ何も起きない (帳簿は事実だけを記す)。"""
    manager, _ = _make_manager(session_factory)  # 会話状態なし
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append(kind)
        or {"submitted": True},
    )
    result = wiring.handle_conversation_end(manager, PERSONA_ID)
    assert fired == []
    assert result["closed"] is False
    assert "no open conversation" in result["reason"]


def test_conversation_end_cancels_the_silence_timeout(session_factory):
    """会話を閉じたら沈黙タイマーの予約も解除する。

    残すと、次の会話の途中で古い予約が発火して始まったばかりの会話を閉じる。
    """
    from saiverse import user_conversation as uc

    manager, _persona = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 15, 0, 0))
    _open_conversation(manager)
    assert uc.arm_conversation_timeout(manager, PERSONA_ID) is True

    wiring.handle_conversation_end(manager, PERSONA_ID)

    entry = manager.event_scheduler._entries_by_key.get(uc._timeout_key(PERSONA_ID))
    assert entry is None or entry.cancelled


def test_conversation_timeout_defers_the_watchdog_tick(session_factory, monkeypatch):
    """沈黙タイマーの発火は帳簿処理のあと watchdog の次回 tick を押し戻す。"""
    from saiverse import user_conversation as uc

    manager, _persona = _make_manager(session_factory)
    conv_ends: List[Any] = []
    monkeypatch.setattr(
        wiring, "handle_conversation_end",
        lambda mgr, pid, **kw: conv_ends.append(pid) or {"closed": True},
    )
    defers: List[str] = []
    manager._autonomy_managers = {
        PERSONA_ID: SimpleNamespace(defer_next_tick=lambda: defers.append("defer")),
    }

    uc.handle_conversation_timeout(manager, PERSONA_ID)

    assert conv_ends == [PERSONA_ID]
    assert defers == ["defer"]


# ---------------------------------------------------------------------------
# on_event: 実イベント (inject_persona_event 経由)
# ---------------------------------------------------------------------------


def _fake_fire(monkeypatch, result):
    calls: List[Any] = []

    def _fake(mgr, pid, kind, context=None, **kw):
        calls.append((kind, context))
        return result

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake)
    return calls


def _fake_life_start(monkeypatch):
    """watchdog の撃ち直し先 (起床の帳簿処理) を記録だけのフェイクに差し替える。

    記録は ``(boundary, params)``。v04 段 1-2 以降、watchdog は判断点
    (fire_judgment_point) を撃たない — 撃つのは
    ``day_plan.handle_scheduled_life_boundary`` の start だけ。判断点の方も
    記録だけのフェイクにして、撃たれたら ``("judgment", kind)`` として同じ
    リストに残す (撃たれないことの検査)。
    """
    calls: List[Any] = []

    def _fake_boundary(mgr, pid, boundary, params=None):
        calls.append((boundary, params))
        return True

    def _fake_judgment(mgr, pid, kind, context=None, **kw):
        calls.append(("judgment", kind))
        return {"submitted": True}

    monkeypatch.setattr(day_plan, "handle_scheduled_life_boundary", _fake_boundary)
    monkeypatch.setattr(wiring, "fire_judgment_point", _fake_judgment)
    return calls


def _mark_life_started(manager, plan_date):
    """その営業日の lives[0] に開始の節目の済印 (``started``) を立てる。

    起床の帳簿処理が最後まで済んだ通常の状態を作る (確定だけして開始の節目が
    済んでいないライフは、watchdog が開始の節目を決着させに行く —
    ``_resume_life_start``)。
    """
    day_plan._mutate_lives(
        manager, PERSONA_ID, plan_date,
        day_plan._life_mark_mutator(0, "started"), context="test",
    )


def test_external_event_not_active_goes_direct(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory, active=False)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-1",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_AUTONOMY_DISABLED
    assert dispatched == ["direct"]
    assert calls == []


def test_external_event_in_conversation_goes_direct(session_factory, monkeypatch):
    """会話中判定はメモリ内の会話状態 (autonomous_behavior_v3.md §7)。

    正典は三代目 — Track の status (v1) → 開いている会話の出来事 (案 Y、
    2026-07-13) → メモリ内の会話状態 (束 6c、2026-08-22)。判定の意味論は不変。
    """
    manager, _ = _make_manager(session_factory)
    _open_conversation(manager)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-2",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_IN_CONVERSATION
    assert dispatched == ["direct"]
    assert calls == []


def test_external_event_engage_now_dispatches_response(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    calls = _fake_fire(monkeypatch, {
        "submitted": True,
        "applied_events": [{
            "type": "judgment_applied", "kind": "on_event",
            "extras": ["reaction=engage_now"],
        }],
    })
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "呼びかけ",
        stimulus_id="ev-3",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_JUDGED_ENGAGE_NOW
    assert dispatched == ["direct"]
    assert calls[0][0] == "on_event"
    assert calls[0][1]["event_text"] == "呼びかけ"
    assert calls[0][1]["is_alert"] is False


def test_external_event_non_engage_reactions_do_not_dispatch(
    session_factory, monkeypatch,
):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {
        "submitted": True,
        "applied_events": [{
            "type": "judgment_applied", "kind": "on_event",
            "extras": ["reaction=ignore"],
        }],
    })
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-4",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == "judged:ignore"
    assert dispatched == []


def test_external_event_falls_back_when_judgment_unavailable(session_factory):
    """Playbook 未 import は本物の fire_judgment_point 経由でも従来経路へ落ちる。

    フェイクの戻り dict ではなく実物を通す — 「機構の不備でイベントを落とさない」
    保証は、結末 (outcome) を書く側と読む側が噛み合って初めて成立する
    (2026-08-14 F3 で代替経路を結末で絞ったため、片方だけ直すと沈黙する)。
    """
    manager, _ = _make_manager(session_factory, with_playbooks=False)
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-5",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert dispatched == ["direct"]


def test_external_event_without_outcome_refuses_the_fallback(
    session_factory, monkeypatch, caplog,
):
    """結末を書き忘れた結果は代替経路を許さない (既定は拒否・WARNING で表に出す)。

    2026-08-14 F3: 「submitted=False かつ indeterminate でなければ起動不能」と
    読む旧実装は、判断が走った後の失敗まで「起動できなかった」に含めていた。
    未知の結果は走ったかもしれない側へ倒す。
    """
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {"submitted": False, "reason": "何かの新しい経路"})
    dispatched: List[str] = []
    with caplog.at_level("WARNING", logger="saiverse.judgment_points"):
        route = wiring.handle_external_event(
            manager, PERSONA_ID, "掲示板の告知",
            stimulus_id="ev-6",
            dispatch_direct=lambda: dispatched.append("direct"),
        )
    assert route == wiring.ROUTE_NONE_INDETERMINATE
    assert dispatched == []
    assert any("no outcome" in r.message for r in caplog.records)


def test_contract_violation_is_checked_before_claiming_a_seat(session_factory):
    """契約違反は台帳に触れる前に落とす (席を孤児化させない)。

    2026-07-30 Codex 五巡目。claim の後で ValueError を出すと、その席は誰にも
    放棄されず prepared のまま残り、回復 tick が同じ不正 payload で再発火して
    は同じ例外を繰り返す。
    """
    manager, _ = _make_manager(session_factory)
    claimed: List[str] = []
    manager.execution_ledger = SimpleNamespace(
        claim_execution=lambda *a, **k: (claimed.append("claimed"), ("e1", True, None))[1],
    )
    with pytest.raises(ValueError, match="event_text"):
        wiring.fire_judgment_point(manager, PERSONA_ID, wiring.KIND_ON_EVENT, {})
    assert claimed == []


def test_external_event_indeterminate_seat_avoids_double_handling(
    session_factory, monkeypatch,
):
    """席を放棄できなかった判断では代替経路を走らせない。

    2026-07-30 Codex 四巡目。実行台帳の席が prepared のまま残ると回復 tick が
    後で再発火するので、ここで応対すると同じイベントを二度処理する。
    「起動できなかった」(= 席は確実に残っていない) との区別が要る。
    """
    from saiverse.judgment_points import OUTCOME_INDETERMINATE

    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {
        "submitted": False,
        "reason": "args build failed: RuntimeError('db is down')",
        "outcome": OUTCOME_INDETERMINATE,
        "execution_id": "exec-1",
    })
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-7",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_NONE_INDETERMINATE
    assert dispatched == []


def test_external_event_freezes_dispatch_envelope_into_context(
    session_factory, monkeypatch,
):
    """応対の材料 (組み立て済み user_input / playbook / args) が判断 context に
    同乗する — 台帳 payload に凍結され、回復 tick の回収が初回とまったく同じ
    入力で応対を再構成できる (Codex 四巡目 medium)。"""
    manager, _ = _make_manager(session_factory)
    captured: Dict[str, Any] = {}

    def _fake(mgr, pid, kind, context=None, **kw):
        captured["context"] = context
        return {"submitted": True, "applied_events": []}

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake)
    envelope = {
        "user_input": "<system>\n[外部イベント通知]\n差出人: X\n</system>",
        "event_type": "x_mention",
        "meta_playbook": "track_user_conversation",
        "args": {"trigger_author_name": "X"},
    }
    wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-8",
        dispatch_direct=lambda: None,
        dispatch_envelope=envelope,
    )
    assert captured["context"]["dispatch_envelope"] == envelope
    assert captured["context"]["event_text"] == "掲示板の告知"


def test_recovered_response_dispatch_result_is_typed(session_factory):
    """応対復元は dispatch の顛末を三値で返す — 副作用ゼロ確定 (safe_failure)
    だけが再試行対象で、実行中の例外 (unknown = LLM が動いたか不明) は二重応対を
    避けて再試行しない (Codex 五巡目 high2 / 六巡目 high1)。"""
    manager, _ = _make_manager(session_factory)
    # Beat 関所閉鎖 = 副作用ゼロ確定 → 再試行してよい
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: {
            "action": "execute", "runtime_outcome": "gate_closed", "error": None,
        },
    )
    assert wiring._dispatch_recovered_event_response(
        manager, PERSONA_ID, {"event_text": "x"},
    ) == wiring.RECOVERED_DISPATCH_SAFE_FAILURE
    # 実行中の例外 = LLM が動いたか不明 → 再試行禁止
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: {
            "action": "execute", "runtime_outcome": "error", "error": "boom",
        },
    )
    assert wiring._dispatch_recovered_event_response(
        manager, PERSONA_ID, {"event_text": "x"},
    ) == wiring.RECOVERED_DISPATCH_UNKNOWN

    sent = []
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: (sent.append(kw), {
            "action": "execute", "runtime_outcome": "completed", "error": None,
        })[1],
    )
    envelope = {"user_input": "<system>原文</system>", "event_type": "sensor",
                "meta_playbook": "track_user_conversation", "args": {"k": 1}}
    assert wiring._dispatch_recovered_event_response(
        manager, PERSONA_ID,
        {"event_text": "x", "dispatch_envelope": envelope},
    ) == wiring.RECOVERED_DISPATCH_OK
    # envelope の実物がそのまま再送される (再構成の別テキストに化けない)
    assert sent[0]["user_input"] == "<system>原文</system>"
    assert sent[0]["args"] == {"k": 1}
    assert sent[0]["metadata"]["event_type"] == "sensor"
    assert sent[0]["metadata"]["recovered"] is True


def test_recovered_response_failure_schedules_bounded_retry(
    session_factory, monkeypatch,
):
    """回収応対の明示失敗 (submit が起動しなかった) は揮発予約の bounded backoff
    で再試行される — 判断台帳は completed 済みで prepared 回収に戻れないため、
    ここで諦めるとイベント応対が黙って消える (Codex 五巡目 high3)。"""
    manager, _ = _make_manager(session_factory)
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda *a, **kw: {
            "submitted": True, "execution_id": "exec-9",
            "applied_events": [{
                "type": "judgment_applied", "extras": ["reaction=engage_now"],
            }],
        },
    )
    calls = {"n": 0}

    def _flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 初回は関所閉鎖 (副作用ゼロ確定の明示失敗)
            return {"action": "execute", "runtime_outcome": "gate_closed",
                    "error": None}
        return {"action": "execute", "runtime_outcome": "completed",
                "error": None}

    manager.pulse_dispatcher = SimpleNamespace(dispatch_schedule_fire=_flaky)
    wiring.refire_judgment_from_recovery(
        manager, PERSONA_ID, "on_event", {"event_text": "x"}, "exec-9",
    )
    assert calls["n"] == 1
    entry = manager.event_scheduler._entries_by_key.get(
        "recovered_response:exec-9"
    )
    assert entry is not None  # 再試行が予約されている
    entry.callback()  # backoff 発火 → 再試行成功
    assert calls["n"] == 2
    # 成功したので次の再試行は積まれない (キーは同じ予約の消費で終わる)


def test_recovered_response_unknown_outcome_is_not_retried(
    session_factory, monkeypatch,
):
    """実行中の例外 (unknown = LLM が動いたか不明) は再試行を積まない — 発話や
    記憶更新まで進んだ応対の再実行は二重応対になる (Codex 六巡目 high1。台帳の
    unknown = 自動再実行禁止と同じ規律)。"""
    manager, _ = _make_manager(session_factory)
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda *a, **kw: {
            "submitted": True, "execution_id": "exec-8",
            "applied_events": [{
                "type": "judgment_applied", "extras": ["reaction=engage_now"],
            }],
        },
    )
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: {
            "action": "execute", "runtime_outcome": "error", "error": "boom",
        },
    )
    wiring.refire_judgment_from_recovery(
        manager, PERSONA_ID, "on_event", {"event_text": "x"}, "exec-8",
    )
    assert "recovered_response:exec-8" not in \
        manager.event_scheduler._entries_by_key


def test_external_event_unknown_reaction_avoids_double_handling(
    session_factory, monkeypatch, caplog,
):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {"submitted": True, "applied_events": []})
    dispatched: List[str] = []
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        route = wiring.handle_external_event(
            manager, PERSONA_ID, "掲示板の告知",
            stimulus_id="ev-9",
            dispatch_direct=lambda: dispatched.append("direct"),
        )
    assert route == wiring.ROUTE_JUDGED_UNKNOWN
    assert dispatched == []


class _BoomController:
    """submit_meta_judgment が必ず例外を投げるフェイク。"""

    def __init__(self, exc: Exception):
        self.calls = 0
        self._exc = exc

    def submit_meta_judgment(self, **kwargs):
        self.calls += 1
        raise self._exc


def test_external_event_runtime_error_marks_unknown_and_does_not_fall_back(
    session_factory,
):
    """A7 + F3: メタレーンの例外が [] に偽装されず submitted=False になり、台帳は
    unknown 終端。**代替経路は走らせない** —— unknown = LLM が動いたか不明であり、
    finalize が既に判断を適用している可能性がある。ここで応対すると判断の決定を
    上書きする (2026-08-14 Codex 指摘 F3。旧実装はここで fallback していた)。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    manager.pulse_controller = _BoomController(RuntimeError("meta lane down"))

    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-10",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_NONE_JUDGMENT_RAN
    assert dispatched == []
    assert manager.pulse_controller.calls == 1
    unknown = ledger.list_unknown()
    assert len(unknown) == 1
    assert unknown[0]["kind"] == "judgment.on_event"


def test_external_event_side_effect_free_failure_falls_back_once(session_factory):
    """副作用ゼロ確定の失敗 (LLM エラー) だけは代替経路を許す。

    台帳は failed 終端 = 世界へ何も作用していない。イベントを落とす方が害なので
    従来経路で 1 回だけ応対する。unknown 終端 (上のテスト) との対比。
    """
    from llm_clients.exceptions import LLMError

    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    manager.pulse_controller = _BoomController(LLMError("provider down"))

    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-11",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert dispatched == ["direct"]  # fallback は 1 回だけ
    assert ledger.list_unknown() == []


def test_runtime_exception_after_finalize_is_indeterminate(session_factory):
    """finalize が適用した**後**のクラッシュでは代替応対を走らせない。

    2026-08-14 Codex 二巡目 medium: `sea/runtime_graph.py` と `runtime_llm.py` は
    Playbook 実行中の**任意の例外**を LLMError に包み直すので、例外型だけでは
    「出力前に落ちた」と「判断を適用した後に落ちた」を区別できない。歯止めは
    台帳の合法遷移 —— applied → failed は不正遷移なので分類が書けず、結末は
    indeterminate になる。ここが崩れると、機構が判断の決定を上書きして応答する。
    """
    from llm_clients.exceptions import LLMError

    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)

    class _ApplyThenBoom(FinalizingPulseController):
        def submit_meta_judgment(self, persona_id, building_id, meta_playbook,
                                 args=None, event_callback=None):
            super().submit_meta_judgment(
                persona_id, building_id, meta_playbook,
                args=args, event_callback=event_callback,
            )  # finalize 相当 (mark_applied) まで進む
            # 汎用ラッパーと同じ形 — 中身は何であれ LLMError で届く
            raise LLMError("wrapped failure after finalize")

    manager.pulse_controller = _ApplyThenBoom(ledger)
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-12",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_NONE_INDETERMINATE
    assert dispatched == []


def test_ledger_claim_failure_is_reported_as_indeterminate(session_factory):
    """台帳 claim の例外を裸で上げず、結末のある結果にする。

    2026-08-14 Codex 二巡目: 例外のまま上げると呼び出し側は結末を受け取れず、
    代替経路の可否を判定する材料が無い。行が作られたかも分からない (作られて
    いれば回復 tick が拾う) ので indeterminate = 応答しない。
    """
    manager, _ = _make_manager(session_factory)

    def _boom(*a, **k):
        raise RuntimeError("database is locked")

    manager.execution_ledger = SimpleNamespace(claim_execution=_boom)
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        stimulus_id="ev-13",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_NONE_INDETERMINATE
    assert dispatched == []


def test_recovery_refire_runs_the_typed_fallback_on_no_effect(
    session_factory, monkeypatch,
):
    """回収の refire でも、副作用ゼロ確定の失敗は代替応対へ落とす。

    2026-08-14 Codex 二巡目: 回収入口だけ無条件に return していたため、通常入口
    なら応対されるはずの失敗でイベントが消えていた。しかも runtime exception で
    台帳が終端した行は prepared 回収にも戻らないので、そこで永久に失われる。
    """
    from saiverse.judgment_points import OUTCOME_NO_EFFECT

    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {
        "submitted": False, "reason": "runtime exception: LLMError('down')",
        "outcome": OUTCOME_NO_EFFECT,
    })
    dispatched: List[Dict[str, Any]] = []
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: (
            dispatched.append(kw)
            or {"action": "execute", "runtime_outcome": "completed"}
        ),
    )
    wiring.refire_judgment_from_recovery(
        manager, PERSONA_ID, "on_event",
        {"event_text": "掲示板の告知", "is_alert": False}, "exec-1",
    )
    assert len(dispatched) == 1
    assert "[外部イベント通知]" in dispatched[0]["user_input"]


def test_external_event_reaction_falls_back_to_ledger_result(
    session_factory, monkeypatch,
):
    """D6 の分岐: callback で reaction が読めなくても台帳 RESULT_JSON の
    reaction から応対を起動できる (Chunk B で finalize が result を刻む前提の口)。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    eid, runnable, _ = ledger.claim_execution(
        "judgment.on_event", idempotency_key=None, persona_id=PERSONA_ID,
    )
    assert runnable
    ledger.mark_running(eid)
    ledger.mark_applied(eid, result={"reaction": "engage_now"})

    _fake_fire(monkeypatch, {
        "submitted": True, "applied_events": [], "execution_id": eid,
    })
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "呼びかけ",
        stimulus_id="ev-14",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_JUDGED_ENGAGE_NOW
    assert dispatched == ["direct"]


# ---------------------------------------------------------------------------
# handle_user_utterance_conflict: 別行動中のユーザー発話の仲裁
# (track_retirement.md §7.4 の直結化。旧 set_alert → v1 メタ判断の後継)
# ---------------------------------------------------------------------------


def _conflict(manager, engaged: List[str]) -> str:
    return wiring.handle_user_utterance_conflict(
        manager, PERSONA_ID, "ちょっといい？",
        engage=lambda: engaged.append("engage"),
        user_id="1",
        stimulus_id="msg:alice_room:1",
    )


def test_utterance_conflict_autonomy_off_engages_directly(session_factory):
    manager, _ = _make_manager(session_factory, active=False)
    engaged: List[str] = []
    assert _conflict(manager, engaged) == wiring.ROUTE_DIRECT_AUTONOMY_DISABLED
    assert engaged == ["engage"]


def test_utterance_conflict_playbook_missing_engages_directly(session_factory):
    """機構の不備 (Playbook 未 import) でユーザーの呼びかけを黙殺しない。"""
    manager, _ = _make_manager(session_factory, with_playbooks=False)
    engaged: List[str] = []
    assert _conflict(manager, engaged) == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert engaged == ["engage"]


def test_utterance_conflict_runtime_error_does_not_engage(session_factory):
    """F3 の本題: 判断が走った後に証跡なく落ちたら activate しない。

    submit_meta_judgment の実行時例外は台帳 unknown = LLM が動いたか不明。
    finalize が note_only を適用した後の例外だと、ここで会話を開始すると
    「応答しない」という判断の決定を機構が上書きして応答してしまう。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    manager.pulse_controller = _BoomController(RuntimeError("meta lane down"))

    engaged: List[str] = []
    assert _conflict(manager, engaged) == wiring.ROUTE_NONE_JUDGMENT_RAN
    assert engaged == []
    assert len(ledger.list_unknown()) == 1


def test_utterance_conflict_side_effect_free_failure_engages(session_factory):
    """副作用ゼロ確定の失敗 (LLM エラー) なら activate してよい (台帳は failed)。"""
    from llm_clients.exceptions import LLMError

    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    manager.pulse_controller = _BoomController(LLMError("provider down"))

    engaged: List[str] = []
    assert _conflict(manager, engaged) == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert engaged == ["engage"]
    assert ledger.list_unknown() == []


def test_utterance_conflict_engage_now_engages(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {
        "submitted": True,
        "applied_events": [{
            "type": "judgment_applied", "kind": "on_event",
            "extras": ["reaction=engage_now"],
        }],
    })
    engaged: List[str] = []
    assert _conflict(manager, engaged) == wiring.ROUTE_JUDGED_ENGAGE_NOW
    assert engaged == ["engage"]


def test_utterance_conflict_note_only_does_not_engage(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {
        "submitted": True,
        "applied_events": [{
            "type": "judgment_applied", "kind": "on_event",
            "extras": ["reaction=note_only"],
        }],
    })
    engaged: List[str] = []
    assert _conflict(manager, engaged) == "judged:note_only"
    assert engaged == []


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------


def test_watchdog_noop_without_day_open_schedule(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "skip", "reason": "no day_open schedule"}
    assert calls == []


def test_watchdog_noop_for_non_active(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory, active=False)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "skip"
    assert calls == []


def test_watchdog_refires_life_start_when_today_has_no_life(session_factory, monkeypatch):
    """当日のライフが無い → 起床の帳簿処理 (start) を撃ち直す。起床行の
    PLAYBOOK_PARAMS はそのまま渡る (ライフ確定に効く値の選別は受け手が行う)。
    判断点 (LLM) は撃たない。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "08:00",
        params={"daily_budget_pulses": 16},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "life_start_refire", "settled": True}
    assert calls == [("start", {"daily_budget_pulses": 16})]


def test_watchdog_does_not_refire_once_the_life_is_confirmed(
    session_factory, monkeypatch,
):
    """ライフが確定済みなら撃ち直さない。

    旧 watchdog は「コマが無い」で day_open (時間割を編成する LLM 判断) を
    撃ち直していた (2026-07-14 実機の教訓)。v04 段 1-2 で watchdog が撃つのは
    起床の帳簿処理だけになり、見る条件も「ライフが確定しているか」になった。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))

    day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "08:00", "22:00",
        requested_budget_pulses=10,
    )
    _mark_life_started(manager, PLAN_DATE)

    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none"}
    assert calls == []


def test_watchdog_does_not_refire_life_start_without_a_close_schedule(
    session_factory, monkeypatch,
):
    """就寝スケジュールが無い設定はライフを定義できない (ライフ無しの日として
    決着する) ので、撃ち直さない — 毎 tick の空撃ちにしない。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none"}
    assert calls == []


def test_watchdog_refire_passes_life_mode_override(session_factory, monkeypatch):
    """再起動後の watchdog 再発火経路でも life_mode_override が透過される。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "08:00",
        params={"life_mode_override": "free"},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "life_start_refire"
    assert calls == [("start", {"life_mode_override": "free"})]


def test_watchdog_respects_waking_window(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    calls = _fake_life_start(monkeypatch)

    clock.enable_virtual(datetime(2026, 7, 4, 7, 0, 0))
    assert wiring.watchdog_tick(manager, PERSONA_ID)["reason"] == "before wake"
    clock.enable_virtual(datetime(2026, 7, 4, 23, 0, 0))
    assert wiring.watchdog_tick(manager, PERSONA_ID)["reason"] == "after close"
    assert calls == []


def test_watchdog_respects_days_of_week(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    # 2026-07-04 は土曜 (weekday=5)。月曜のみのスケジュールなら発火しない。
    _add_day_schedule(
        session_factory, "judgment_day_open", "08:00", days_of_week=[0],
    )
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["reason"] == "not a scheduled day"
    assert calls == []




# ---------------------------------------------------------------------------
# 旧経路の停止
# ---------------------------------------------------------------------------


def test_pulse_scheduler_module_is_gone():
    """SubLineScheduler (track_autonomous 連続 Pulse) はモジュールごと廃止。"""
    with pytest.raises(ModuleNotFoundError):
        import saiverse.pulse_scheduler  # noqa: F401


def test_pulse_dispatcher_has_no_subline_route():
    from saiverse.pulse_dispatcher import PulseDispatcher

    assert not hasattr(PulseDispatcher, "dispatch_subline_poll")


def test_autonomy_tick_dispatches_watchdog_not_meta_judgment(monkeypatch):
    """dispatch_autonomy_tick は watchdog へ縮退し、定期メタ判断を撃たない。"""
    from saiverse.pulse_dispatcher import PulseDispatcher

    ticks: List[Any] = []
    manager = SimpleNamespace(
        meta_layer=SimpleNamespace(
            on_periodic_tick=lambda *a, **kw: ticks.append("meta"),
        ),
    )
    watched: List[str] = []
    monkeypatch.setattr(
        wiring, "watchdog_tick",
        lambda mgr, pid: watched.append(pid) or {"action": "none"},
    )
    PulseDispatcher(manager).dispatch_autonomy_tick(PERSONA_ID)
    assert watched == [PERSONA_ID]
    assert ticks == []  # 旧: on_periodic_tick 直叩き — もう呼ばれない


# ---------------------------------------------------------------------------
# adapter: has_assistant_message_since (回収の重複判定の実 SQL)
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def test_adapter_has_assistant_message_since(tmp_path, monkeypatch):
    """実 SAIMemoryAdapter で重複判定 SQL (role / created_at) を検証。"""
    from unittest.mock import patch as _patch
    from datetime import timezone, timedelta

    monkeypatch.setenv("SAIMEMORY_MEMORY", "1")
    persona_dir = tmp_path / "personas" / "tester"
    persona_dir.mkdir(parents=True, exist_ok=True)

    with _patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter

        adapter = SAIMemoryAdapter(
            "tester", persona_dir=persona_dir, resource_id="tester",
        )
        try:
            t0 = datetime.now(timezone.utc)
            adapter.append_persona_message({
                "role": "assistant",
                "content": "おかえり",
                "timestamp": (t0 - timedelta(seconds=100)).isoformat(),
            })
            adapter.append_persona_message({
                "role": "user",
                "content": "ただいま",
                "timestamp": t0.isoformat(),
            })
            epoch = int(t0.timestamp())
            # 会話区間に assistant 応答がある
            assert adapter.has_assistant_message_since(epoch - 200) is True
            # 区間開始が応答より後 → 今回の会話では応答していない
            # (user 発話だけでは往復にならない)
            assert adapter.has_assistant_message_since(epoch - 50) is False
        finally:
            adapter.close()




# ---------------------------------------------------------------------------
# 深夜跨ぎ (overnight) ヘルパ
# ---------------------------------------------------------------------------


def test_is_overnight_with_close_before_wake():
    assert wiring.is_overnight("07:00", "01:00") is True


def test_is_overnight_with_close_after_wake():
    assert wiring.is_overnight("07:00", "22:00") is False


def test_is_overnight_with_no_close():
    assert wiring.is_overnight("07:00", None) is False


def test_effective_plan_date_normal_time():
    """非跨ぎリズム: 常に暦日を返す。"""
    from datetime import date as _date
    now = datetime(2026, 7, 5, 12, 0, 0)  # 12:00 (wake=07:00, close=22:00)
    result = wiring.effective_plan_date(now, "07:00", "22:00")
    assert result == _date(2026, 7, 5)


def test_effective_plan_date_midnight_tail_is_previous_day():
    """跨ぎリズムで深夜帯 (01:00 発火) → 前日が営業日。"""
    from datetime import date as _date
    now = datetime(2026, 7, 5, 1, 0, 0)  # 01:00 (wake=07:00, close=01:00)
    result = wiring.effective_plan_date(now, "07:00", "01:00")
    assert result == _date(2026, 7, 4)


def test_effective_plan_date_daytime_is_same_day():
    """跨ぎリズムで昼間 (12:00) → 当日が営業日。"""
    from datetime import date as _date
    now = datetime(2026, 7, 5, 12, 0, 0)  # 12:00
    result = wiring.effective_plan_date(now, "07:00", "01:00")
    assert result == _date(2026, 7, 5)


def test_effective_plan_date_no_wake():
    """wake が None → 暦日をそのまま返す。"""
    from datetime import date as _date
    now = datetime(2026, 7, 5, 1, 0, 0)
    result = wiring.effective_plan_date(now, None, None)
    assert result == _date(2026, 7, 5)


def test_in_waking_window_overnight():
    """跨ぎリズム (wake=07:00, close=01:00)。"""
    assert wiring.in_waking_window("12:00", "07:00", "01:00") is True   # 昼間 = 窓内
    assert wiring.in_waking_window("00:30", "07:00", "01:00") is True   # 深夜帯 = 窓内
    assert wiring.in_waking_window("06:00", "07:00", "01:00") is False  # 起床前 = 窓外
    assert wiring.in_waking_window("01:30", "07:00", "01:00") is False  # 就寝後 = 窓外


def test_in_waking_window_normal():
    """非跨ぎリズム (wake=07:00, close=22:00)。"""
    assert wiring.in_waking_window("12:00", "07:00", "22:00") is True
    assert wiring.in_waking_window("06:00", "07:00", "22:00") is False
    assert wiring.in_waking_window("22:00", "07:00", "22:00") is False  # close は窓外
    assert wiring.in_waking_window("23:00", "07:00", "22:00") is False


def test_in_waking_window_no_close():
    """close なし: 起床以降はずっと窓内。"""
    assert wiring.in_waking_window("07:00", "07:00", None) is True
    assert wiring.in_waking_window("06:59", "07:00", None) is False


# ---------------------------------------------------------------------------
# watchdog — 深夜跨ぎリズム
# ---------------------------------------------------------------------------


def test_watchdog_overnight_daytime_no_plan_refires(session_factory, monkeypatch):
    """跨ぎリズムで昼間 (12:00) にライフが無い → 起床の帳簿処理を再発火する。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "07:00",
        params={"daily_budget_rounds": 12},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    clock.enable_virtual(datetime(2026, 7, 4, 12, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "life_start_refire"
    assert calls[0][0] == "start"


def test_watchdog_overnight_midnight_no_plan_does_not_refire(session_factory, monkeypatch):
    """跨ぎリズムで深夜帯 (00:30) にライフが無い → 再発火しない (深夜制約)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    # 00:30 は深夜帯 (前日リズムの尻尾)。ライフが無くても撃たない。
    clock.enable_virtual(datetime(2026, 7, 5, 0, 30, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "none"
    assert out["reason"] == "previous business day still in effect: no life-start refire"
    assert calls == []


def test_watchdog_does_not_open_a_new_day_while_a_life_is_running(
    session_factory, monkeypatch,
):
    """走行中ライフの営業日に plan が無くても day_open は撃たない。

    ゲートを外した先で最も危険な並び — 前営業日のライフが続いている最中に
    新しい一日を開くと、前のライフと新しいライフが重なる (Codex十巡目 #3)。
    """
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 5, 0, 30, 0))
    day_plan.save_lives(manager, PERSONA_ID, "2026-07-04", [
        {"start": "23:00", "end": "06:00", "budget_pulses": 20, "mode": "free"},
    ])
    _mark_life_started(manager, "2026-07-04")
    calls = _fake_life_start(monkeypatch)

    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out["action"] == "none"
    # 走っているライフがある (= その営業日のライフは確定済み) ので撃ち直しの
    # 判定そのものに入らない
    assert out == {"action": "none"}
    assert calls == []


def test_watchdog_ignores_the_weekday_gate_while_a_life_is_running(
    session_factory, monkeypatch,
):
    """曜日ゲートも走行中ライフには適用しない (ライフがある日 = 起きた日)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "07:00", days_of_week=[0],  # 月曜のみ
    )
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 5, 8, 0, 0))   # 日曜 (weekday=6)

    yesterday = "2026-07-04"
    day_plan.save_lives(manager, PERSONA_ID, yesterday, [
        {"start": "20:00", "end": "10:00", "budget_pulses": 20, "mode": "free"},
    ])
    _mark_life_started(manager, yesterday)
    calls = _fake_life_start(monkeypatch)

    out = wiring.watchdog_tick(manager, PERSONA_ID)

    # 走っているライフがある = 確定済み。曜日ゲートの理由 ("not a scheduled
    # day") も出さずに何もしない
    assert out == {"action": "none"}
    assert calls == []


def test_watchdog_settles_the_life_start_left_unapplied_after_confirmation(
    session_factory, monkeypatch,
):
    """ライフの確定後に開始の節目が失敗した日は、次の watchdog が開始の節目を
    決着させる (2026-10-10 Codex 指摘)。

    確定 (persona_life への commit) と開始の節目 (apply_life_boundary) は別の
    段。以前の watchdog はライフの存在だけを見て action:none を返したので、
    「確定済みだが開始が済んでいない」ライフは永久に復旧しなかった。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    # 起床の帳簿処理: 確定は commit され、開始の節目で落ちる。
    with patch.object(
        day_plan, "apply_life_boundary", side_effect=RuntimeError("boom"),
    ):
        assert day_plan.handle_scheduled_life_boundary(
            manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START,
        ) is False
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1 and not lives[0].get("started")
    assert ledger.find_execution(
        day_plan.LIFE_BOUNDARY_KIND_START, f"{PERSONA_ID}:{PLAN_DATE}",
    ) is None

    # 次の watchdog: 新しい一日は開かず (起床の再発火は撃たない)、開始の
    # 節目だけを同じ営業日で決着させる。
    clock.enable_virtual(datetime(2026, 7, 4, 9, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "life_start_resume", "settled": True}
    assert calls == []
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0].get("started") is True
    entry = ledger.find_execution(
        day_plan.LIFE_BOUNDARY_KIND_START, f"{PERSONA_ID}:{PLAN_DATE}",
    )
    assert entry is not None and entry["status"] in ("applied", "completed")

    # その次の watchdog は何もしない (済印を見て撃たない)。
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none"}


def test_watchdog_life_start_resume_is_idempotent_against_a_settled_ledger(
    session_factory, monkeypatch,
):
    """台帳で開始の節目が決着済みなら、済印が無くても二度目の適用はしない。

    apply_life_boundary は冪等キー {persona}:{plan_date} を claim し、
    applied / completed なら何もせず True を返す — watchdog の撃ち直しと
    ScheduleManager の再試行が重なっても通知は一度きり。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 9, 0, 0))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    execution_id, runnable, _ = ledger.claim_execution(
        day_plan.LIFE_BOUNDARY_KIND_START,
        idempotency_key=f"{PERSONA_ID}:{PLAN_DATE}", persona_id=PERSONA_ID,
    )
    assert runnable and ledger.try_mark_running(execution_id)
    ledger.mark_applied(execution_id, result={"boundary": "start"})

    with patch.object(
        day_plan, "_sync_cache_ttl_for_life_start",
        side_effect=AssertionError("idempotent steps must not run again"),
    ):
        out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "life_start_resume", "settled": True}
    # 済印は付かない (台帳側で決着済みの扱い) が、副作用は二度走っていない。
    assert ledger.find_execution(
        day_plan.LIFE_BOUNDARY_KIND_START, f"{PERSONA_ID}:{PLAN_DATE}",
    )["execution_id"] == execution_id


def test_watchdog_settles_the_start_of_an_overnight_life_on_its_own_business_day(
    session_factory, monkeypatch,
):
    """深夜跨ぎライフの尻尾 (暦日は翌日) で開始が未決着なら、開始の節目は
    **そのライフの営業日** で決着させる — 暦日のライフを確定する起床の再発火
    (handle_scheduled_life_boundary) は撃たない。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "23:00")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    clock.enable_virtual(datetime(2026, 7, 5, 0, 30, 0))
    day_plan.save_lives(manager, PERSONA_ID, "2026-07-04", [
        {"start": "23:00", "end": "06:00", "budget_pulses": 20, "mode": "free"},
    ])
    calls = _fake_life_start(monkeypatch)

    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "life_start_resume", "settled": True}
    assert calls == []
    assert day_plan.get_lives(manager, PERSONA_ID, "2026-07-04")[0]["started"] is True
    assert day_plan.get_lives(manager, PERSONA_ID, "2026-07-05") == []
    assert ledger.find_execution(
        day_plan.LIFE_BOUNDARY_KIND_START, f"{PERSONA_ID}:2026-07-04",
    ) is not None


def test_watchdog_keeps_the_window_gate_for_a_zero_length_life(
    session_factory, monkeypatch,
):
    """区間として成立しないライフはゲートを外す資格がない (Codex十巡目 #2)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 23, 30, 0))   # 現行設定では窓外
    _write_legacy_lives(session_factory, "2026-07-04", [
        {"start": "07:00", "end": "07:00", "budget_pulses": 20, "mode": "free",
         "used_pulses": 0, "judgment_pulses": 0},
    ])
    calls = _fake_life_start(monkeypatch)

    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "none", "reason": "after close"}
    assert calls == []


def test_watchdog_opens_the_new_day_even_after_yesterdays_life_ended(
    session_factory, monkeypatch,
):
    """朝の day_open が失敗した日は、ちゃんと撃ち直される。

    営業日の解決に「最後に始まったライフの日」という段を入れたとき、それを無条件に
    採ると 7/5 08:00 (7/4 のライフは終了済み、7/5 にはまだライフ無し) で前日 7/4 を
    指し、watchdog が「前の営業日がまだ続いている」と読んで撃たなくなる — 一日が
    始まらないまま止まる。一日は前へしか進まない (設定が言う営業日の方が新しければ
    そちらを採る) の逆側の見張り (Codex十二巡目)。
    """
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    day_plan.save_lives(manager, PERSONA_ID, "2026-07-04", [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])

    clock.enable_virtual(datetime(2026, 7, 5, 8, 0, 0))
    calls = _fake_life_start(monkeypatch)
    out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out["action"] == "life_start_refire"
    assert calls[0][0] == "start"


def test_watchdog_skips_the_tick_when_lives_are_unreadable(
    session_factory, monkeypatch,
):
    """ライフを読めない tick は何もしない (Codex八巡目 #2)。

    どの営業日を見ているか分からないままライフの有無を判定すると、起床の
    撃ち直しに化けうる。次の tick へ委ねる。
    """
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))

    calls = _fake_life_start(monkeypatch)
    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        out = wiring.watchdog_tick(manager, PERSONA_ID)

    assert out == {"action": "skip", "reason": "lives unreadable"}
    assert calls == []






# ---------------------------------------------------------------------------
# 刺激の ID (v0.4 段 1-3): on_event の冪等キーと、ID の無い刺激の fail-closed
# (docs/issues/on_event_judgment_has_no_idempotency_key.md の「決まったこと」)
# ---------------------------------------------------------------------------


def _on_event_context(stimulus_id):
    return {"event_text": "掲示板の告知", "is_alert": False, "stimulus_id": stimulus_id}


def test_on_eventの冪等キーは刺激のIDから作られる(session_factory):
    manager, _ = _make_manager(session_factory)
    key = wiring._judgment_idempotency_key(
        manager, PERSONA_ID, wiring.KIND_ON_EVENT, _on_event_context("x:mention:42"),
    )
    assert key == f"{PERSONA_ID}:x:mention:42"


def test_同じ刺激IDのon_event判断の二席目は拒まれ判断は一度しか走らない(session_factory):
    """以前は on_event のキーが None で、発火のたびに新しい席を取っていた。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))

    first = wiring.fire_judgment_point(
        manager, PERSONA_ID, wiring.KIND_ON_EVENT, _on_event_context("ev-same"),
    )
    assert first["submitted"] is True
    assert ledger.get_execution(first["execution_id"])["status"] == XL.STATUS_APPLIED

    second = wiring.fire_judgment_point(
        manager, PERSONA_ID, wiring.KIND_ON_EVENT, _on_event_context("ev-same"),
    )
    assert second["submitted"] is False
    assert second["reason"] == f"duplicate:{XL.STATUS_APPLIED}"
    assert second["execution_id"] == first["execution_id"]
    assert len(manager.pulse_controller.calls) == 1

    # 別の刺激は別の席を取って走る
    other = wiring.fire_judgment_point(
        manager, PERSONA_ID, wiring.KIND_ON_EVENT, _on_event_context("ev-other"),
    )
    assert other["submitted"] is True
    assert other["execution_id"] != first["execution_id"]
    assert len(manager.pulse_controller.calls) == 2


@pytest.mark.parametrize("active", [False, True])
def test_刺激IDの無い外部イベントは応対も判断も起動せずERRORで落ちる(
    session_factory, monkeypatch, caplog, active,
):
    """自律 OFF (直接応対の経路) でも ON (判断の経路) でも、ID の無い刺激は
    代理採番せずに落とす (義務の fail-closed)。"""
    manager, _ = _make_manager(session_factory, active=active)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    dispatched: List[str] = []
    with caplog.at_level("ERROR", logger="saiverse.autonomy_wiring"):
        route = wiring.handle_external_event(
            manager, PERSONA_ID, "掲示板の告知",
            stimulus_id=None,
            dispatch_direct=lambda: dispatched.append("direct"),
        )
    assert route == wiring.ROUTE_NONE_MISSING_STIMULUS_ID
    assert dispatched == []
    assert calls == []
    assert any("without a stimulus_id" in r.message for r in caplog.records)


def test_刺激IDの無い別行動中の発話は判断を経ず直接会話を始める(
    session_factory, monkeypatch,
):
    """発話は黙殺しない — 判断を冪等にできないので仲裁を経ずに応答する。"""
    manager, _ = _make_manager(session_factory)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    engaged: List[str] = []
    route = wiring.handle_user_utterance_conflict(
        manager, PERSONA_ID, "ちょっといい？",
        engage=lambda: engaged.append("engage"),
        user_id="1",
        stimulus_id=None,
    )
    assert route == wiring.ROUTE_DIRECT_MISSING_STIMULUS_ID
    assert engaged == ["engage"]
    assert calls == []


def test_別行動中の発話の刺激IDが判断の文脈に同乗する(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    captured: Dict[str, Any] = {}

    def _fake(mgr, pid, kind, context=None, **kw):
        captured["context"] = context
        return {"submitted": True, "applied_events": []}

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake)
    wiring.handle_user_utterance_conflict(
        manager, PERSONA_ID, "ちょっといい？",
        engage=lambda: None, user_id="1", stimulus_id="msg:alice_room:3",
    )
    assert captured["context"]["stimulus_id"] == "msg:alice_room:3"


def test_day_close_plan_date_is_previous_day_at_midnight(session_factory):
    """跨ぎリズムで 01:00 の就寝の節目は前日が営業日 (ライフ終了の帳簿処理が使う)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    clock.enable_virtual(datetime(2026, 7, 5, 1, 0, 0))
    assert wiring._day_close_plan_date(manager, PERSONA_ID) == "2026-07-04"


def test_reserved_schedule_playbooks():
    """ペルソナのスペルから触らせない名前 (判断点の名前空間 + 起床・就寝の行)。"""
    for name in (
        "judgment_day_open", "judgment_day_close", "judgment_on_event",
        "judgment_post_session", "  judgment_day_open  ",
    ):
        assert wiring.is_reserved_schedule_playbook(name), name
    for name in ("track_user_conversation", "", None, "my_judgment_helper"):
        assert not wiring.is_reserved_schedule_playbook(name), name
