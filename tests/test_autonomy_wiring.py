"""自律行動 v2 の本番配線 (saiverse/autonomy_wiring.py) のテスト。

活性化配線の検証項目:

- fire_judgment_point: 自律 ON ゲート / Playbook 欠如の WARNING スキップ /
  precondition (Lock 下の再評価)
- handle_scheduled_judgment: 判断点スケジュール (day_open / day_close) の変換と
  時刻駆動できない kind の棄却。ScheduleManager からの経路分岐
- handle_wait_response_timeout / handle_conversation_end: 会話終了 →
  post_conversation の発火 / 0 往復会話の抑止 / social Track の従来経路温存
- handle_external_event: on_event 判断の経路判断基準 (自律 ON / 会話中 /
  engage_now の応対起動 / フォールバック)
- watchdog_tick: 正常時 no-op / plan 欠如時のみ day_open 再発火 /
  コマ予約の途絶検知
- 旧経路の停止: pulse_scheduler モジュールと dispatch_subline_poll の不在、
  dispatch_autonomy_tick の watchdog 縮退
- 本番プロセス (EventScheduler dispatch スレッド) でコマが発火すること
"""
from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

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
        city = City(USERID=1, CITYNAME="test_city", UI_PORT=3001, API_PORT=8001)
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


# ---------------------------------------------------------------------------
# fire_judgment_point: 本番ゲート
# ---------------------------------------------------------------------------


def test_fire_judgment_point_skips_non_active_persona(session_factory):
    manager, _ = _make_manager(session_factory, active=False)
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is False
    assert result["reason"] == "persona autonomy disabled"
    assert manager.pulse_controller.calls == []


def test_fire_judgment_point_skips_when_playbook_missing(session_factory, caplog):
    manager, _ = _make_manager(session_factory, with_playbooks=False)
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is False
    assert result["reason"] == "playbook not imported"
    assert manager.pulse_controller.calls == []
    # 運用手順 (import_playbook) がログに出る
    assert any("import_playbook" in r.message for r in caplog.records)


def test_fire_judgment_point_dispatches_when_gates_pass(session_factory):
    manager, _ = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is True
    assert [c["meta_playbook"] for c in manager.pulse_controller.calls] == [
        "judgment_day_close",
    ]
    call = manager.pulse_controller.calls[0]
    assert call["persona_id"] == PERSONA_ID
    assert call["building_id"] == "alice_room"
    assert "situation_text" in call["args"]
    assert "response_schema" in call["args"]


def test_fire_judgment_point_precondition_rechecked(session_factory):
    manager, _ = _make_manager(session_factory)
    result = wiring.fire_judgment_point(
        manager, PERSONA_ID, "day_close", precondition=lambda: False,
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
    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
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


def test_fire_day_open_dedup_scheduled_then_watchdog(session_factory, monkeypatch):
    """A2: 定刻 (schedule) → watchdog の順でも day_open の submit は 1 回だけ。

    2 回目 (watchdog 相当、precondition 付き) は claim で duplicate になり、
    precondition の評価にも境界副作用 (ライフ確定) にも到達しない。
    """
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    confirmed: List[str] = []
    monkeypatch.setattr(
        wiring, "_confirm_life_at_day_open",
        lambda mgr, pid, ctx: confirmed.append(pid),
    )

    first = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert first["submitted"] is True
    assert first["execution_id"] is not None
    assert len(manager.pulse_controller.calls) == 1
    assert confirmed == [PERSONA_ID]
    # Chunk B: finalize (フェイクが代行) の mark_applied 証跡で成功と判定
    assert ledger.get_execution(first["execution_id"])["status"] == XL.STATUS_APPLIED

    precondition_evals: List[bool] = []

    def _precondition():
        precondition_evals.append(True)
        return True

    second = wiring.fire_judgment_point(
        manager, PERSONA_ID, "day_open", precondition=_precondition,
    )
    assert second["submitted"] is False
    assert second["reason"] == f"duplicate:{XL.STATUS_APPLIED}"
    assert second["execution_id"] == first["execution_id"]
    # 判断 Pulse は増えず、precondition も境界副作用も走っていない
    assert len(manager.pulse_controller.calls) == 1
    assert precondition_evals == []
    assert confirmed == [PERSONA_ID]


def test_fire_day_open_dedup_watchdog_then_scheduled(session_factory, monkeypatch):
    """A2: watchdog (precondition 付き) → 定刻の逆順でも submit は 1 回だけ。"""
    manager, _ = _make_manager(session_factory)
    _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    confirmed: List[str] = []
    monkeypatch.setattr(
        wiring, "_confirm_life_at_day_open",
        lambda mgr, pid, ctx: confirmed.append(pid),
    )

    first = wiring.fire_judgment_point(
        manager, PERSONA_ID, "day_open", precondition=lambda: True,
    )
    assert first["submitted"] is True
    assert len(manager.pulse_controller.calls) == 1
    assert confirmed == [PERSONA_ID]

    second = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert second["submitted"] is False
    assert second["reason"].startswith("duplicate:")
    assert len(manager.pulse_controller.calls) == 1
    assert confirmed == [PERSONA_ID]


def test_fire_precondition_rejection_marks_failed_and_next_claim_runs(
    session_factory,
):
    """precondition 却下は claim 済みの席を failed に落とし、次の fire は
    failed キー退避で再び走れる (D2/D3)。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    rejected = wiring.fire_judgment_point(
        manager, PERSONA_ID, "day_open", precondition=lambda: False,
    )
    assert rejected["submitted"] is False
    assert rejected["reason"] == "precondition not met"
    assert ledger.get_execution(rejected["execution_id"])["status"] == XL.STATUS_FAILED
    assert manager.pulse_controller.calls == []

    retried = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert retried["submitted"] is True
    assert retried["execution_id"] != rejected["execution_id"]
    assert len(manager.pulse_controller.calls) == 1


def test_fire_force_bypasses_idempotency_key(session_factory):
    """force=True はキーを None に落とし、debug 明示発火が duplicate に阻まれない。"""
    manager, _ = _make_manager(session_factory)
    _attach_finalizing_ledger(manager, session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))

    first = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert first["submitted"] is True
    forced = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open", force=True)
    assert forced["submitted"] is True
    assert forced["execution_id"] != first["execution_id"]
    assert len(manager.pulse_controller.calls) == 2


def test_fire_without_ledger_degrades_with_single_warning(session_factory, caplog):
    """台帳の無い manager (旧テストスタブ) は WARN 一回で従来挙動に degrade する。"""
    wiring._LEDGER_MISSING_WARNED.discard(PERSONA_ID)
    manager, _ = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        r1 = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
        r2 = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert r1["submitted"] is True
    assert r2["submitted"] is True  # 台帳なし = dedup も無し (従来挙動)
    assert r1["execution_id"] is None
    warns = [m for m in caplog.messages if "no execution_ledger" in m]
    assert len(warns) == 1


# ---------------------------------------------------------------------------
# handle_scheduled_judgment + ScheduleManager 経路分岐
# ---------------------------------------------------------------------------


def test_scheduled_judgment_day_open_passes_budget(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append((kind, context))
        or {"submitted": True},
    )
    wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open",
        params={"daily_budget_rounds": 24, "other": "x"},
    )
    assert fired == [("day_open", {"daily_budget_rounds": 24})]


def test_scheduled_judgment_day_open_passes_life_mode_override(session_factory, monkeypatch):
    """life.md v0.5 §5.1: life_mode_override (even/free) が context に透過される。"""
    manager, _ = _make_manager(session_factory)
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append((kind, context))
        or {"submitted": True},
    )
    wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open",
        params={"life_mode_override": "even", "other": "x"},
    )
    assert fired == [("day_open", {"life_mode_override": "even"})]


def test_scheduled_judgment_day_open_rejects_invalid_life_mode_override(session_factory, monkeypatch):
    """LIFE_MODES 外の値は無視される (書ける口をなくす、life.md v0.5 §3)。"""
    manager, _ = _make_manager(session_factory)
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append((kind, context))
        or {"submitted": True},
    )
    wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open",
        params={"life_mode_override": "bogus"},
    )
    assert fired == [("day_open", {})]


def test_scheduled_judgment_rejects_non_schedulable_kind(session_factory, caplog):
    manager, _ = _make_manager(session_factory)
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        result = wiring.handle_scheduled_judgment(
            manager, PERSONA_ID, "judgment_post_session",
        )
    assert result["submitted"] is False
    assert manager.pulse_controller.calls == []


def test_schedule_manager_routes_judgment_playbooks(session_factory, monkeypatch):
    """ScheduleManager._execute_schedule が判断点 Playbook を専用経路へ流す。"""
    from saiverse.schedule_manager import ScheduleManager

    manager, _ = _make_manager(session_factory)
    routed: List[Any] = []
    monkeypatch.setattr(
        wiring, "handle_scheduled_judgment",
        lambda mgr, pid, name, params=None: routed.append((pid, name, params))
        or {"submitted": True},
    )
    dispatched: List[Any] = []
    manager.pulse_dispatcher = SimpleNamespace(
        dispatch_schedule_fire=lambda **kw: dispatched.append(kw),
    )
    manager.all_personas = manager.personas

    sm = ScheduleManager(saiverse_manager=manager)
    schedule = PersonaSchedule(
        SCHEDULE_ID=1,
        PERSONA_ID=PERSONA_ID,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK="judgment_day_open",
        ENABLED=True,
        TIME_OF_DAY="08:00",
        PLAYBOOK_PARAMS='{"daily_budget_rounds": 30}',
    )
    sm._execute_schedule(schedule, session=None)

    assert routed == [(PERSONA_ID, "judgment_day_open", {"daily_budget_rounds": 30})]
    assert dispatched == []  # 通常の submit_schedule 経路は通らない


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
        dispatch_schedule_fire=lambda **kw: dispatched.append(kw),
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
        sm._execute_schedule(schedule, session=db)
    finally:
        db.close()

    assert routed == []
    assert len(dispatched) == 1
    assert dispatched[0]["meta_playbook"] == "track_user_conversation"


# ---------------------------------------------------------------------------
# post_conversation: 会話終了 (wait_response タイムアウト)
# ---------------------------------------------------------------------------


class _ExchangeAdapter:
    """has_track_assistant_message_since の記録付きスタブ。"""

    def __init__(self, answer):
        self.answer = answer
        self.queries: List[Any] = []

    def has_track_assistant_message_since(self, track_id, since_epoch):
        self.queries.append((track_id, since_epoch))
        return self.answer


def _open_conversation_episode(manager):
    from saiverse import episodes

    return episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="alice_room",
        participants=[PERSONA_ID, "1"],
    )


def test_conversation_end_fires_post_conversation(session_factory, monkeypatch):
    manager, persona = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 15, 0, 0))
    ep = _open_conversation_episode(manager)
    persona.sai_memory = _ExchangeAdapter(answer=True)

    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append(kind)
        or {"submitted": True},
    )
    result = wiring.handle_conversation_end(manager, PERSONA_ID, "track-1")
    assert fired == ["post_conversation"]
    assert result["submitted"] is True
    # 往復判定は会話区間 (出来事の started_at) で切っている
    assert persona.sai_memory.queries == [("track-1", ep["started_at"])]
    # 出来事は閉じられている
    from saiverse import episodes

    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_CONVERSATION,
    ) is None


def test_conversation_end_zero_exchange_skips_judgment(
    session_factory, monkeypatch, caplog,
):
    manager, persona = _make_manager(session_factory)
    clock.enable_virtual(datetime(2026, 7, 4, 15, 0, 0))
    _open_conversation_episode(manager)
    persona.sai_memory = _ExchangeAdapter(answer=False)

    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda *a, **kw: fired.append(a) or {"submitted": True},
    )
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        result = wiring.handle_conversation_end(manager, PERSONA_ID, "track-1")
    assert fired == []
    assert result["submitted"] is False
    assert "no exchange" in result["reason"]
    # 出来事は 0 往復でも閉じる (会話区間の帳簿は事実)
    from saiverse import episodes

    assert episodes.get_open_episode(
        manager, PERSONA_ID, kind=episodes.KIND_CONVERSATION,
    ) is None


def test_conversation_end_defaults_to_fire_when_undetectable(
    session_factory, monkeypatch,
):
    """出来事なし / adapter 未対応では判断を撃つ側に倒す (収穫の取りこぼし回避)。"""
    manager, persona = _make_manager(session_factory)
    # 出来事なし + adapter なし
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append(kind)
        or {"submitted": True},
    )
    wiring.handle_conversation_end(manager, PERSONA_ID, "track-1")
    assert fired == ["post_conversation"]


def test_wait_response_timeout_routes_by_track_type(session_factory, monkeypatch):
    """user_conversation → post_conversation / それ以外 → 従来メタ判断。"""
    manager, persona = _make_manager(session_factory)
    conv_track = SimpleNamespace(track_type="user_conversation", track_id="t-conv")
    social_track = SimpleNamespace(track_type="social", track_id="t-soc")
    manager.track_manager.tracks = {"t-conv": conv_track, "t-soc": social_track}

    conv_ends: List[Any] = []
    monkeypatch.setattr(
        wiring, "handle_conversation_end",
        lambda mgr, pid, tid: conv_ends.append(tid) or {"submitted": True},
    )
    ticks: List[Any] = []
    manager.meta_layer = SimpleNamespace(
        on_periodic_tick=lambda pid, context=None, force=False: ticks.append(context),
    )
    defers: List[str] = []
    manager._autonomy_managers = {
        PERSONA_ID: SimpleNamespace(defer_next_tick=lambda: defers.append("defer")),
    }

    wiring.handle_wait_response_timeout(manager, PERSONA_ID, "t-conv")
    assert conv_ends == ["t-conv"]
    assert ticks == []

    wiring.handle_wait_response_timeout(manager, PERSONA_ID, "t-soc")
    assert conv_ends == ["t-conv"]  # 増えない
    assert len(ticks) == 1
    assert ticks[0]["trigger"] == "wait_response_timeout"
    # 両経路とも watchdog tick を押し戻す
    assert defers == ["defer", "defer"]


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


def test_external_event_not_active_goes_direct(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory, active=False)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_AUTONOMY_DISABLED
    assert dispatched == ["direct"]
    assert calls == []


def test_external_event_in_conversation_goes_direct(session_factory, monkeypatch):
    """会話中判定は開いている kind='conversation' の出来事 (life.md §7 案 Y)。

    旧実装は running Track の種別で判定していたが、Track はもう時間経過で
    状態を動かさないため出来事の open/close が「会話中」の唯一の真実になった。
    """
    manager, _ = _make_manager(session_factory)
    _open_conversation_episode(manager)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
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
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == "judged:ignore"
    assert dispatched == []


def test_external_event_falls_back_when_judgment_unavailable(
    session_factory, monkeypatch,
):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {"submitted": False, "reason": "playbook not imported"})
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert dispatched == ["direct"]


def test_external_event_unknown_reaction_avoids_double_handling(
    session_factory, monkeypatch, caplog,
):
    manager, _ = _make_manager(session_factory)
    _fake_fire(monkeypatch, {"submitted": True, "applied_events": []})
    dispatched: List[str] = []
    with caplog.at_level("WARNING", logger="saiverse.autonomy_wiring"):
        route = wiring.handle_external_event(
            manager, PERSONA_ID, "掲示板の告知",
            dispatch_direct=lambda: dispatched.append("direct"),
        )
    assert route == wiring.ROUTE_JUDGED_UNKNOWN
    assert dispatched == []


def test_external_event_runtime_error_marks_unknown_and_falls_back_once(
    session_factory,
):
    """A7: メタレーンの例外が [] に偽装されず submitted=False になり、
    direct dispatch fallback が 1 回だけ起きる。台帳は unknown 終端
    (prepared ではないので回復 tick の refire 対象にならない)。"""
    manager, _ = _make_manager(session_factory)
    ledger = _attach_ledger(manager, session_factory)

    class _BoomController:
        def __init__(self):
            self.calls = 0

        def submit_meta_judgment(self, **kwargs):
            self.calls += 1
            raise RuntimeError("meta lane down")

    manager.pulse_controller = _BoomController()
    dispatched: List[str] = []
    route = wiring.handle_external_event(
        manager, PERSONA_ID, "掲示板の告知",
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
    assert dispatched == ["direct"]  # fallback は 1 回だけ
    assert manager.pulse_controller.calls == 1
    unknown = ledger.list_unknown()
    assert len(unknown) == 1
    assert unknown[0]["kind"] == "judgment.on_event"


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
        dispatch_direct=lambda: dispatched.append("direct"),
    )
    assert route == wiring.ROUTE_JUDGED_ENGAGE_NOW
    assert dispatched == ["direct"]


# ---------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------


def test_watchdog_noop_without_day_open_schedule(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "skip", "reason": "no day_open schedule"}
    assert calls == []


def test_watchdog_noop_for_non_active(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory, active=False)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "skip"
    assert calls == []


def test_watchdog_refires_day_open_when_plan_missing(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "08:00",
        params={"daily_budget_rounds": 16},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "day_open_refire"
    assert calls == [("day_open", {"daily_budget_rounds": 16})]


def test_watchdog_refires_when_plan_row_exists_but_slots_are_empty(
    session_factory, monkeypatch,
):
    """2026-07-14 実機の教訓の回帰: confirm_life_for_today がライフ確定で
    day_plan 行を先に作るため、day_open の時間割編成が (丸めても救済できず)
    全滅した日は、行はあるが slots_json="[]" のまま残る。``plan is None`` だけ
    を見ていた旧 watchdog はこの日を永久にリカバリできなかった——行の有無で
    なくコマの有無で判定することを確認する (main check + precondition の
    両方)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))

    # 実際の破綻を再現: ライフだけ確定させ、時間割 (slots) は空のまま残す。
    day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "08:00", "22:00",
        requested_budget_pulses=10,
    )
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == []

    captured: Dict[str, Any] = {}

    def _fake(mgr, pid, kind, context=None, **kw):
        captured["kind"] = kind
        captured["precondition"] = kw.get("precondition")
        return {"submitted": True}

    monkeypatch.setattr(wiring, "fire_judgment_point", _fake)
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "day_open_refire"
    assert captured["kind"] == "day_open"
    # precondition も「行はあるがコマが無い」を正しく「まだ必要」と判定する。
    assert captured["precondition"]() is True


def test_watchdog_refire_passes_life_mode_override(session_factory, monkeypatch):
    """再起動後の watchdog day_open 再発火経路でも life_mode_override が透過される。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "08:00",
        params={"life_mode_override": "free"},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "day_open_refire"
    assert calls == [("day_open", {"life_mode_override": "free"})]


def test_watchdog_respects_waking_window(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    calls = _fake_fire(monkeypatch, {"submitted": True})

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
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["reason"] == "not a scheduled day"
    assert calls == []


def test_watchdog_noop_when_plan_and_reservations_intact(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "11:00", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none"}
    assert calls == []


def test_watchdog_reschedules_lost_slot_reservations(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "08:00")
    clock.enable_virtual(datetime(2026, 7, 4, 10, 0, 0))
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:30", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "",
         "status": "done"},
        {"start": "11:00", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    # 予約を積んでいない (= 再起動で失われた状態)。done コマは対象外。
    assert day_plan.find_lost_slot_reservations(manager, PERSONA_ID, PLAN_DATE) == [1]

    rescheduled: List[str] = []
    monkeypatch.setattr(
        day_plan, "reschedule_pending_slots",
        lambda mgr, pid, plan_d=None, **kw: rescheduled.append(pid) or 1,
    )
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "reschedule"
    assert out["lost"] == [1]
    assert rescheduled == [PERSONA_ID]


# ---------------------------------------------------------------------------
# post_session の恒久配線 (day_plan 組み込みハンドラ)
# ---------------------------------------------------------------------------


def test_builtin_worker_slot_handler_fires_post_session(session_factory, monkeypatch):
    manager, _ = _make_manager(session_factory)
    session_result = SimpleNamespace(
        digest="やった", artifacts=["item-1"], rounds_used=3,
        ended_reason="finished", task_ref="task:1",
    )
    monkeypatch.setattr(
        day_plan, "run_worker_slot_session",
        lambda mgr, pid, date_str, slot, index: session_result,
    )
    fired: List[Any] = []
    monkeypatch.setattr(
        wiring, "fire_judgment_point",
        lambda mgr, pid, kind, context=None, **kw: fired.append((kind, context))
        or {"submitted": True},
    )
    slot = {"start": "10:00", "kind": "作る", "ref": "task:1",
            "facility": "own_room", "budget_rounds": 5, "note": ""}
    used = day_plan._handle_worker_slot(manager, PERSONA_ID, PLAN_DATE, slot, 0)

    assert used == 3
    assert len(fired) == 1
    kind, context = fired[0]
    assert kind == "post_session"
    assert context["session_result"] is session_result
    assert context["task_ref"] == "task:1"
    assert context["budget_rounds"] == 5


def test_builtin_worker_slot_handler_survives_judgment_failure(
    session_factory, monkeypatch,
):
    """判断の失敗はコマの帳簿 (rounds 返却) を壊さない。"""
    manager, _ = _make_manager(session_factory)
    session_result = SimpleNamespace(
        digest="", artifacts=[], rounds_used=2, ended_reason="finished",
        task_ref=None,
    )
    monkeypatch.setattr(
        day_plan, "run_worker_slot_session",
        lambda *a, **kw: session_result,
    )

    def _boom(*a, **kw):
        raise RuntimeError("judgment down")

    monkeypatch.setattr(wiring, "fire_judgment_point", _boom)
    slot = {"start": "10:00", "kind": "知る", "ref": "none",
            "facility": "own_room", "budget_rounds": 3, "note": ""}
    used = day_plan._handle_worker_slot(manager, PERSONA_ID, PLAN_DATE, slot, 0)
    assert used == 2


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
# adapter: has_track_assistant_message_since (往復判定の実 SQL)
# ---------------------------------------------------------------------------


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def test_adapter_has_track_assistant_message_since(tmp_path, monkeypatch):
    """実 SAIMemoryAdapter で往復判定 SQL (role / origin_track_id / created_at) を検証。"""
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
                "origin_track_id": "t-1",
            })
            adapter.append_persona_message({
                "role": "user",
                "content": "ただいま",
                "timestamp": t0.isoformat(),
                "origin_track_id": "t-1",
            })
            epoch = int(t0.timestamp())
            # 会話区間に assistant 応答がある
            assert adapter.has_track_assistant_message_since("t-1", epoch - 200) is True
            # 区間開始が応答より後 → 今回の会話では応答していない
            # (user 発話だけでは往復にならない)
            assert adapter.has_track_assistant_message_since("t-1", epoch - 50) is False
            # 別 Track には無い
            assert adapter.has_track_assistant_message_since("t-9", epoch - 200) is False
        finally:
            adapter.close()


# ---------------------------------------------------------------------------
# 本番プロセスでのコマ発火 (EventScheduler dispatch スレッド)
# ---------------------------------------------------------------------------


def test_slots_fire_on_real_dispatch_thread(session_factory):
    """day_plan の予約はシム専用ではない — 実時刻の dispatch スレッドで発火する。"""
    manager, _ = _make_manager(session_factory)
    today = datetime.now().date().isoformat()
    past = (datetime.now()).strftime("%H:%M")  # 過去/現在時刻 → 即時発火
    day_plan.save_day_plan(manager, PERSONA_ID, today, [
        {"start": past, "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    pushed = day_plan.schedule_day_plan(manager, PERSONA_ID, today)
    assert pushed == 1

    manager.event_scheduler.start()
    try:
        # 通常 1 秒未満で発火する。上限は負荷時の余裕。
        # NOTE: dispatch スレッドの書き込みと同時に読むと、共有 in-memory
        # SQLite の癖で load_day_plan が一瞬 None を返すことがある
        # (2026-07-07 に間欠観測)。「まだ読めない」は「まだ done でない」と
        # 同じ扱いでポーリングを続け、最終 assert は締切後の再読で行う。
        deadline = time.monotonic() + 20.0
        status = None
        while time.monotonic() < deadline:
            slots = day_plan.load_day_plan(manager, PERSONA_ID, today)
            if slots:
                status = slots[0]["status"]
                if status == "done":
                    break
            time.sleep(0.05)
        slots = day_plan.load_day_plan(manager, PERSONA_ID, today)
        assert slots, "day plan unreadable after polling deadline"
        status = slots[0]["status"]
        assert status == "done", f"slot did not fire on dispatch thread (status={status})"
        assert slots[0]["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY
    finally:
        manager.event_scheduler.stop()


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
    """跨ぎリズムで昼間 (12:00) に plan が無い → day_open を再発火する。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(
        session_factory, "judgment_day_open", "07:00",
        params={"daily_budget_rounds": 12},
    )
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    clock.enable_virtual(datetime(2026, 7, 4, 12, 0, 0))
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "day_open_refire"
    assert calls[0][0] == "day_open"


def test_watchdog_overnight_midnight_no_plan_does_not_refire(session_factory, monkeypatch):
    """跨ぎリズムで深夜帯 (00:30) に plan が無い → 再発火しない (深夜制約)。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    # 00:30 は深夜帯 (前日リズムの尻尾)。plan が無くても撃たない。
    clock.enable_virtual(datetime(2026, 7, 5, 0, 30, 0))
    calls = _fake_fire(monkeypatch, {"submitted": True})
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "none"
    assert out["reason"] == "overnight tail: no refire in midnight zone"
    assert calls == []


def test_watchdog_overnight_midnight_with_plan_reschedules(session_factory, monkeypatch):
    """跨ぎリズムで深夜帯 (00:30)、前日 plan がある + 予約消失 → re-push する。"""
    manager, _ = _make_manager(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")
    # 2026-07-05 00:30 は 2026-07-04 の営業日の深夜帯
    clock.enable_virtual(datetime(2026, 7, 5, 0, 30, 0))

    # 前日 (営業日) の plan を保存
    yesterday = "2026-07-04"
    day_plan.save_day_plan(manager, PERSONA_ID, yesterday, [
        {"start": "00:30", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    # 予約は push していない (= 消失状態)
    assert day_plan.find_lost_slot_reservations(manager, PERSONA_ID, yesterday) == [0]

    rescheduled: List[str] = []
    monkeypatch.setattr(
        day_plan, "reschedule_pending_slots",
        lambda mgr, pid, plan_d=None, **kw: rescheduled.append(pid) or 1,
    )
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "reschedule"
    assert rescheduled == [PERSONA_ID]


# ---------------------------------------------------------------------------
# day_close: 就寝判断の plan_date が前日になる (深夜跨ぎ)
# ---------------------------------------------------------------------------


def test_day_close_judgment_plan_date_is_previous_day_at_midnight(
    session_factory, monkeypatch
):
    """跨ぎリズムで 01:00 に発火した就寝判断は、judgment_context の
    plan_date が前日の暦日になる。"""
    from saiverse import judgment_points

    manager, _ = _make_manager(session_factory)
    # wake=07:00 / close=01:00 のスケジュールを DB に入れる
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "01:00")

    # 2026-07-05 01:00 に発火 → 営業日は 2026-07-04
    clock.enable_virtual(datetime(2026, 7, 5, 1, 0, 0))

    captured_args: List[Any] = []

    def _fake_submit(persona_id, building_id, meta_playbook, args=None, event_callback=None):
        captured_args.append(args or {})

    manager.pulse_controller.submit_meta_judgment = _fake_submit

    from saiverse.judgment_points import run_judgment_point
    run_judgment_point(manager, PERSONA_ID, "day_close")

    assert captured_args, "submit_meta_judgment was not called"
    import json as _json
    jctx = _json.loads(captured_args[0].get("judgment_context", "{}"))
    assert jctx.get("plan_date") == "2026-07-04", (
        f"expected plan_date=2026-07-04 but got {jctx.get('plan_date')!r}"
    )


# ---------------------------------------------------------------------------
# day_plan: 深夜コマの発火時刻 (plan_date + 1 日)
# ---------------------------------------------------------------------------


def test_slot_fire_at_overnight_midnight_slot():
    """wake=07:00, start=00:30 → plan_date+1日 の datetime を返す。"""
    from datetime import datetime as _dt
    fire = day_plan._slot_fire_at(
        "2026-07-04",
        {"start": "00:30"},
        wake="07:00",
    )
    assert fire == _dt(2026, 7, 5, 0, 30, 0)


def test_slot_fire_at_normal_slot():
    """wake=07:00, start=09:00 → 同日の datetime を返す。"""
    from datetime import datetime as _dt
    fire = day_plan._slot_fire_at(
        "2026-07-04",
        {"start": "09:00"},
        wake="07:00",
    )
    assert fire == _dt(2026, 7, 4, 9, 0, 0)


def test_slot_fire_at_no_wake_is_same_day():
    """wake=None → 従来どおり同日 combine (後方互換)。"""
    from datetime import datetime as _dt
    fire = day_plan._slot_fire_at(
        "2026-07-04",
        {"start": "00:30"},
    )
    assert fire == _dt(2026, 7, 4, 0, 30, 0)
