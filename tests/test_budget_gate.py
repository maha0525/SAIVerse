"""日次予算ゲート (saiverse/day_plan.py の台帳 + 発火時ゲート、自律行動 v2 §4.5)。

一時 DB (in-memory SQLite) + mock run_work_session + DaySimulator (仮想クロック):

- 台帳ヘルパ: init_budget_ledger / get_budget_state / consume_budget の積算、
  台帳の無い日は None (ゲート無効)、先行消費が init で保持される
- 発火時ゲート: 残高 < コマ予算 → 残高まで切り詰め (slots_json へ永続化) + WARN、
  残高 0 → skipped + WARN (ハンドラ実行なし・施設移動なし)、
  budget_rounds=0 の作業コマも既定値ベースでゲートされる
- 台帳の無い日はゲート無効 = 従来挙動 (後方互換)。ただし実測 used は記録される
- 休む (consumes_budget でない kind) は残高 0 でも実行される
- 通し: day_open finalize (台帳初期化) → コマ発火 → 消費 (mock セッション)

teardown で engine.dispose() + clock.disable_virtual() を必ず行う。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock
from saiverse import day_plan
from saiverse.day_simulator import DaySimulator
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 7, 0, 0)


# ---------------------------------------------------------------------------
# fixtures (test_day_plan.py と同型の最小スタブ)
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
    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)


@pytest.fixture
def manager(session_factory):
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

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=FakeAdapter(),
    )

    class StubOccupancy:
        def __init__(self, personas):
            self.moves: List[tuple] = []
            self._personas = personas

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            p = self._personas.get(entity_id)
            if p is not None:
                p.current_building_id = to_id
            return True, "ok"

    personas = {PERSONA_ID: persona}
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        occupancy_manager=StubOccupancy(personas),
        event_scheduler=EventScheduler(),  # start() しない (シム前提)
        track_manager=TrackManager(session_factory=session_factory),
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
    )


@pytest.fixture
def task_ref(manager):
    t = PersonaTaskManager(manager.SessionLocal).create_task(
        persona_id=PERSONA_ID, title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする", auto_activate=False,
    )
    assert t["task_ref"] == "task:1"
    return "task:1"


def _mock_result(**over):
    base = dict(digest="digest", artifacts=[], rounds_used=1, ended_reason="finished")
    base.update(over)
    return SimpleNamespace(**base)


def _save_single_slot(manager, *, kind="知る", ref="task:1", facility="library",
                      budget_rounds=12, start="09:00"):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [{
        "start": start, "kind": kind, "ref": ref,
        "facility": facility, "budget_rounds": budget_rounds, "note": "調べもの",
    }])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)


def _run_day(manager, hours=(8, 12)):
    DaySimulator(
        manager.event_scheduler,
        start=BASE.replace(hour=0) + timedelta(hours=hours[0]),
        end=BASE.replace(hour=0) + timedelta(hours=hours[1]),
    ).run()


# ---------------------------------------------------------------------------
# 台帳ヘルパ
# ---------------------------------------------------------------------------


def test_budget_state_none_without_ledger(manager):
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) is None


def test_init_and_consume_accumulates(manager):
    state = day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 40)
    assert state == {"total": 40, "used": 0, "remaining": 40}

    assert day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 5)["used"] == 5
    state = day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 3)
    assert state == {"total": 40, "used": 8, "remaining": 32}
    # 永続化されている (再読)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 8


def test_consume_before_init_is_preserved(manager):
    # 台帳 (total) の無い日でも used は記録され、後から init しても保持される
    assert day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 4) is None
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta[day_plan.META_BUDGET_USED] == 4

    state = day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 10)
    assert state == {"total": 10, "used": 4, "remaining": 6}


def test_init_rejects_invalid_total(manager):
    with pytest.raises(ValueError, match="total_rounds"):
        day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, -1)


def test_consume_ignores_invalid_rounds(manager):
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 10)
    state = day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, -3)
    assert state["used"] == 0  # 無視 (WARN)


# ---------------------------------------------------------------------------
# 発火時ゲート
# ---------------------------------------------------------------------------


def test_fire_clamps_budget_to_remaining(manager, task_ref, caplog):
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 10)
    _save_single_slot(manager, budget_rounds=12)

    calls: List[int] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append(budget_rounds)
        return _mock_result(rounds_used=7)

    with caplog.at_level("WARNING", logger="saiverse.day_plan"):
        with patch("sea.work_session.run_work_session", side_effect=fake_ws):
            _run_day(manager)

    # 残高 10 < 要求 12 → 10 に切り詰めてハンドラへ
    assert calls == [10]
    assert any("clamped" in r.message for r in caplog.records)
    # 切り詰めが slots_json に永続化され、実測 7 が台帳に積算される
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["budget_rounds"] == 10
    assert slots[0]["status"] == "done"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) == {
        "total": 10, "used": 7, "remaining": 3,
    }


def test_fire_skips_when_budget_exhausted(manager, task_ref, caplog):
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 5)
    day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 5)
    _save_single_slot(manager, budget_rounds=3)

    with caplog.at_level("WARNING", logger="saiverse.day_plan"):
        with patch("sea.work_session.run_work_session") as mock_ws:
            _run_day(manager)

    mock_ws.assert_not_called()
    assert any("budget exhausted" in r.message for r in caplog.records)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    # 残高 0 のコマは施設移動もしない
    assert manager.occupancy_manager.moves == []


def test_default_budget_slot_is_gated(manager, task_ref):
    # budget_rounds=0 の作業コマはハンドラで既定値 (8) に化けるため、
    # ゲートも既定値ベースで比較する (残高 5 < 8 → 5 に切り詰め)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 5)
    _save_single_slot(manager, budget_rounds=0)

    calls: List[int] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append(budget_rounds)
        return _mock_result(rounds_used=budget_rounds)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        _run_day(manager)

    assert calls == [5]
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["remaining"] == 0


def test_no_ledger_day_gate_inactive_but_usage_recorded(manager, task_ref):
    # 台帳の無い日 (day_open 前 / 旧データ) はゲート無効 = 従来挙動
    _save_single_slot(manager, budget_rounds=12)

    calls: List[int] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append(budget_rounds)
        return _mock_result(rounds_used=12)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        _run_day(manager)

    assert calls == [12]  # 切り詰めなし
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    # 実測 used は記録される (後から init_budget_ledger しても保持される)
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta[day_plan.META_BUDGET_USED] == 12
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) is None


def test_rest_slot_runs_even_with_zero_remaining(manager):
    # 休む は consumes_budget でないため、残高 0 でも skipped にならない
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 0)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [{
        "start": "09:00", "kind": "休む", "ref": "none",
        "facility": "own_room", "budget_rounds": 0, "note": "",
    }])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    _run_day(manager)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 0


# ---------------------------------------------------------------------------
# 通し: day_open finalize (台帳初期化) → 発火 → 消費
# ---------------------------------------------------------------------------


def test_day_open_to_fire_to_consume_end_to_end(manager, task_ref, tmp_path, caplog):
    """起床判断の編成が台帳を書き、コマ発火が残高を削っていく一日の通し。"""
    clock.enable_virtual(BASE)
    finalize_mod = load_builtin_tool("judgment_finalize")
    from tools.context import persona_context

    output = {
        "monologue": "午前は記事、午後は下書き。",
        "timetable": [
            {"start": "09:00", "kind": "知る", "ref": "task:1",
             "facility": "library", "budget_rounds": 5, "note": "記事の続き"},
            {"start": "14:00", "kind": "作る", "ref": "none",
             "facility": "workshop", "budget_rounds": 6, "note": "下書き"},
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 8})
    with caplog.at_level("WARNING"):
        with persona_context(PERSONA_ID, tmp_path, manager=manager):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_open",
                judgment_context=ctx, situation_text="[起床判断] ...",
            )
    assert "applied=True" in summary

    # 編成時に台帳が書かれ、予算超過 (5+6=11 > 8) はソフト WARN
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) == {
        "total": 8, "used": 0, "remaining": 8,
    }
    assert any("超過" in r.message for r in caplog.records)
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "今日の作業ラウンド予算: 8" in content

    # 発火: 知る (5 消費) → 作る (残 3 に切り詰め → 3 消費) → 残高 0
    calls: List[int] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append(budget_rounds)
        return _mock_result(rounds_used=budget_rounds)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        _run_day(manager, hours=(8, 24))

    assert calls == [5, 3]
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]
    assert [s["budget_rounds"] for s in slots] == [5, 3]  # 切り詰めの永続化
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) == {
        "total": 8, "used": 8, "remaining": 0,
    }
