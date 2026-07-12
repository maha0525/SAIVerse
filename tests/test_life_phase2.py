"""ライフ Phase 2「ライフの器」のテスト (docs/intent/life.md §4/§5/§7/§11.2)。

一時 DB (in-memory SQLite) + 仮想クロックで検証する:

- lives の永続化 (get_lives/save_lives の round trip、bookkeeping フィールドの
  既定値・再宣言時の引き継ぎ)
- 検証: フォーマット不正・ライフ同士の重なり・谷にコマ (両方向: save_lives が
  既存コマを見る / save_day_plan・replace_remaining_slots が既存ライフを見る)・
  均等モードの間隔違反 (LIFE_EVEN_MAX_GAP_MINUTES)
- mode の既定導出 (derive_default_life_mode): DEFAULT_MODEL の provider から
- 予算台帳のライフ世代交代: consume_life_pulse / consume_life_rounds の積算、
  get_budget_state のライフ由来導出、ライフ単位ゲート (二値・クランプしない)
- ライフ境界イベント: schedule_lives の push、発火時の通知 (event_message タグ)
  と start_fired/end_fired の永続化、watchdog による途絶再 push
- 後方互換: lives が無い日は全経路 (get_budget_state/_apply_budget_gate/
  save_day_plan/replace_remaining_slots) が従来挙動のまま (既存テスト群
  test_day_plan.py / test_budget_gate.py が無宣言のまま緑であることも参照)

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

from database.models import AI, Base, City, PersonaSchedule, Playbook, User
from saiverse import autonomy_wiring as wiring
from saiverse import clock
from saiverse import day_plan
from saiverse import judgment_points as jp
from saiverse.day_simulator import DaySimulator
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)


# ---------------------------------------------------------------------------
# fixtures (test_day_plan.py / test_budget_gate.py と同型の最小スタブ)
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
    """SAIMemory adapter の最小スタブ (append_persona_message の記録のみ)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)


class StubOccupancy:
    def __init__(self, personas):
        self.moves: List[tuple] = []
        self._personas = personas

    def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
        self.moves.append((entity_id, entity_type, from_id, to_id))
        return True, "ok"


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
        model=None,
    )
    personas = {PERSONA_ID: persona}
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        occupancy_manager=StubOccupancy(personas),
        event_scheduler=EventScheduler(),  # start() しない (シム/同期検証)
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


def _slot(start, *, kind="知る", ref="task:1", facility="library",
          budget_rounds=5, note=""):
    return {"start": start, "kind": kind, "ref": ref,
            "facility": facility, "budget_rounds": budget_rounds, "note": note}


def _default_lives():
    # mode は両方 "free" にしておく (均等モードの間隔検証は専用テストで扱う。
    # ここでは round trip / 重なり / フォーマット検証だけが関心事)。
    return [
        {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
    ]


# ---------------------------------------------------------------------------
# 永続化: get_lives / save_lives round trip
# ---------------------------------------------------------------------------


def test_get_lives_empty_when_not_declared(manager):
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_save_and_get_lives_round_trip(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("09:00"), _slot("15:00", ref="none", kind="休む", facility="own_room",
              budget_rounds=0),
    ])
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert [(l["start"], l["end"]) for l in saved] == [
        ("08:00", "12:00"), ("14:00", "20:00"),
    ]
    for life in saved:
        assert life["used_pulses"] == 0
        assert life["used_rounds"] == 0
        assert life["start_fired"] is False
        assert life["end_fired"] is False
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == saved


def test_save_lives_empty_list_is_noop(manager):
    assert day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, []) == []
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_save_lives_redeclare_preserves_bookkeeping_by_start_end(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())

    life = day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["used_pulses"] == 1
    life = day_plan.consume_life_rounds(
        manager, PERSONA_ID, PLAN_DATE, 3, at_time="09:00",
    )
    assert life["used_rounds"] == 3

    # 起床判断のやり直し: 同じ (start, end) のライフを再宣言 → 消費は引き継がれる
    resaved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert resaved[0]["used_pulses"] == 1
    assert resaved[0]["used_rounds"] == 3

    # 時刻が変わったライフは別物 (0 から)
    changed = [
        {"start": "08:00", "end": "13:00", "budget_pulses": 6, "mode": "free"},
    ]
    resaved2 = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, changed)
    assert resaved2[0]["used_pulses"] == 0
    assert resaved2[0]["used_rounds"] == 0


# ---------------------------------------------------------------------------
# 検証: フォーマット・重なり・谷コマ・均等モード間隔
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda l: l.__setitem__(0, {**l[0], "start": "8:00"}), "HH:MM"),
        (lambda l: l.__setitem__(0, {**l[0], "end": "07:00"}), "must be after start"),
        (lambda l: l.__setitem__(0, {**l[0], "budget_pulses": 0}), "budget_pulses"),
        (lambda l: l.__setitem__(0, {**l[0], "budget_pulses": -1}), "budget_pulses"),
        (lambda l: l.__setitem__(0, {**l[0], "mode": "chaotic"}), "mode"),
    ],
)
def test_save_lives_rejects_invalid_format(manager, mutate, match):
    lives = _default_lives()
    mutate(lives)
    with pytest.raises(ValueError, match=match):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, lives)


def test_save_lives_rejects_overlap(manager):
    lives = [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
        {"start": "11:00", "end": "15:00", "budget_pulses": 4, "mode": "free"},
    ]
    with pytest.raises(ValueError, match="重なっています"):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, lives)


def test_save_lives_rejects_slot_in_valley(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("13:00")])
    with pytest.raises(ValueError, match="谷にコマは置けません"):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
        ])


def test_save_day_plan_rejects_slot_outside_declared_lives(manager, task_ref):
    """save_day_plan 自身が既存ライフを見て谷コマを拒否する (life.md §4.3)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    with pytest.raises(ValueError, match="谷にコマは置けません"):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            _slot("09:00"), _slot("13:00", ref="none", kind="休む",
                                   facility="own_room", budget_rounds=0),
        ])
    # 拒否された保存は既存 plan を壊さない
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["09:00"]


def test_save_day_plan_allows_slot_when_no_lives_declared(manager, task_ref):
    """ライフ宣言の無い日は谷の概念自体が無い (後方互換)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("23:50")])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["start"] == "23:50"


def test_save_lives_rejects_even_mode_gap_violation(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("08:10"), _slot("10:00", ref="none", kind="休む",
                                facility="own_room", budget_rounds=0),
    ])
    with pytest.raises(ValueError, match="上限 50 分を超えています"):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "even"},
        ])


def test_save_lives_rejects_even_mode_gap_violation_with_no_slots(manager):
    """コマが 1 つも無いライフ (start→end 直結) も間隔検証の対象。"""
    with pytest.raises(ValueError, match="上限 50 分を超えています"):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "08:00", "end": "09:00", "budget_pulses": 2, "mode": "even"},
        ])


def test_save_lives_accepts_even_mode_within_gap(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("08:30"),
        _slot("09:10", ref="none", kind="休む", facility="own_room", budget_rounds=0),
    ])
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "09:40", "budget_pulses": 6, "mode": "even"},
    ])
    assert saved[0]["mode"] == "even"


def test_replace_remaining_slots_rejects_valley_slot(manager, task_ref):
    """日中の残り時間割の全置換 (post_conversation 等) も谷コマを拒否する。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    with pytest.raises(ValueError, match="谷にコマは置けません"):
        day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
            _slot("15:00", ref="none", kind="休む", facility="own_room",
                  budget_rounds=0),
        ])


# ---------------------------------------------------------------------------
# mode の既定導出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-sonnet-5", day_plan.LIFE_MODE_EVEN),
        ("gpt-5.1-instant", day_plan.LIFE_MODE_EVEN),
        ("gemini-2.5-flash", day_plan.LIFE_MODE_FREE),
        (None, day_plan.LIFE_MODE_FREE),
        ("no-such-model-config", day_plan.LIFE_MODE_FREE),
    ],
)
def test_derive_default_life_mode(manager, model, expected):
    manager.personas[PERSONA_ID].model = model
    assert day_plan.derive_default_life_mode(manager, PERSONA_ID) == expected


# ---------------------------------------------------------------------------
# 予算台帳のライフ世代交代
# ---------------------------------------------------------------------------


def test_consume_life_pulse_and_rounds_accumulate(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 10, "mode": "free"},
    ])
    life = day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["used_pulses"] == 1
    life = day_plan.consume_life_rounds(
        manager, PERSONA_ID, PLAN_DATE, 5, at_time="09:00",
    )
    assert life["used_rounds"] == 5

    state = day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)
    assert state["total"] == 10
    assert state["used"] == pytest.approx(1 + 5 * day_plan.LIFE_ROUND_BUDGET_FACTOR)
    assert state["remaining"] == pytest.approx(10 - state["used"])


def test_consume_life_pulse_outside_any_life_is_noop(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    assert day_plan.consume_life_pulse(
        manager, PERSONA_ID, PLAN_DATE, at_time="20:00",
    ) is None
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 0


def test_consume_life_pulse_and_rounds_noop_without_lives(manager):
    assert day_plan.consume_life_pulse(
        manager, PERSONA_ID, PLAN_DATE, at_time="09:00",
    ) is None
    assert day_plan.consume_life_rounds(
        manager, PERSONA_ID, PLAN_DATE, 5, at_time="09:00",
    ) is None


def test_get_budget_state_falls_back_to_legacy_ledger_without_lives(manager):
    """lives の無い日は旧日次ラウンド台帳のまま (後方互換)。"""
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 40)
    day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 8)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE) == {
        "total": 40, "used": 8, "remaining": 32,
    }


# ---------------------------------------------------------------------------
# ライフ単位の予算ゲート (二値・クランプしない)
# ---------------------------------------------------------------------------


def test_life_budget_gate_skips_when_exhausted(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=5)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 3, "mode": "free"},
    ])
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    lives[0]["used_pulses"] = 3  # 使い切っておく
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {day_plan.META_LIVES: lives})
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session") as mock_ws:
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8), end=BASE + timedelta(hours=10),
        ).run()

    mock_ws.assert_not_called()
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_BUDGET_EXHAUSTED


def test_life_budget_gate_does_not_clamp_rounds(manager, task_ref):
    """旧ゲートと異なり、ライフゲートは残高があればラウンド数をクランプしない。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=20)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 1, "mode": "free"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    calls: List[int] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append(budget_rounds)
        return _mock_result(rounds_used=budget_rounds)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8), end=BASE + timedelta(hours=10),
        ).run()

    assert calls == [20]  # 残高わずか (パルス予算 1) でもラウンドは切り詰めない
    # 発火後、そのライフのパルス消費・ラウンド消費が両方積算される
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 1
    assert lives[0]["used_rounds"] == 20


def test_life_budget_gate_allows_through_when_slot_outside_any_life(manager, task_ref):
    """save_lives の谷検証を経ずに直接台帳を触った防御的経路 (通す + WARN)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=5)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    # ライフを谷検証をバイパスして直接組み替える (異常系の再現)
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {
        day_plan.META_LIVES: [
            {"start": "13:00", "end": "18:00", "budget_pulses": 4, "mode": "free",
             "used_pulses": 0, "used_rounds": 0, "start_fired": False, "end_fired": False},
        ],
    })
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session") as mock_ws:
        mock_ws.return_value = _mock_result(rounds_used=5)
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8), end=BASE + timedelta(hours=10),
        ).run()
    mock_ws.assert_called_once()
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


# ---------------------------------------------------------------------------
# ライフ境界イベント: schedule / 発火 / 通知
# ---------------------------------------------------------------------------


def test_schedule_lives_pushes_start_and_end(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    pushed = day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE)
    assert pushed == 2
    assert manager.event_scheduler.has_key(day_plan._life_key(PERSONA_ID, PLAN_DATE, 0, "start"))
    assert manager.event_scheduler.has_key(day_plan._life_key(PERSONA_ID, PLAN_DATE, 0, "end"))


def test_schedule_lives_without_lives_is_noop(manager):
    assert day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE) == 0


def test_life_boundary_events_fire_and_notify(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    pushed = day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE)
    assert pushed == 2

    DaySimulator(
        manager.event_scheduler,
        start=BASE + timedelta(hours=8), end=BASE + timedelta(hours=24),
    ).run()

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["start_fired"] is True
    assert lives[0]["end_fired"] is True

    messages = manager.personas[PERSONA_ID].sai_memory.messages
    texts = [m["content"] for m in messages]
    assert any("ライフ開始" in t and "09:00" in t for t in texts)
    assert any("ライフ終了" in t and "11:00" in t for t in texts)
    for m in messages:
        assert m["metadata"]["tags"] == ["internal", "event_message", "day_plan"]
        assert m["role"] == "user"
        assert m["content"].startswith("<system>") and m["content"].endswith("</system>")

    # 発火済みの境界は再発火しても no-op (二重通知しない)
    day_plan._fire_life_boundary(manager, PERSONA_ID, PLAN_DATE, 0, "start")
    assert len(manager.personas[PERSONA_ID].sai_memory.messages) == 2


def test_schedule_lives_does_not_repush_already_fired_boundary(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE)
    day_plan._fire_life_boundary(manager, PERSONA_ID, PLAN_DATE, 0, "start")

    # 再起動を模して EventScheduler を新規化 (インメモリ予約が失われる)。
    manager.event_scheduler = EventScheduler()
    assert day_plan.find_lost_life_reservations(manager, PERSONA_ID, PLAN_DATE) == [
        (0, "end"),
    ]
    # 発火済み (start) は再 push されない
    assert day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE) == 1


# ---------------------------------------------------------------------------
# watchdog: 予約途絶の再 push (autonomy_wiring.watchdog_tick)
# ---------------------------------------------------------------------------


def _add_day_schedule(session_factory, playbook_name, time_of_day):
    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK=playbook_name, ENABLED=True, TIME_OF_DAY=time_of_day,
        ))
        db.commit()
    finally:
        db.close()


def _import_judgment_playbooks(session_factory):
    db = session_factory()
    try:
        for name in wiring.JUDGMENT_PLAYBOOK_NAMES:
            db.add(Playbook(name=name, schema_json="{}", nodes_json="{}"))
        db.commit()
    finally:
        db.close()


def test_watchdog_reschedules_lost_life_reservations(manager, session_factory, task_ref):
    manager.personas[PERSONA_ID].activity_state = "Active"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    clock.enable_virtual(BASE + timedelta(hours=9))

    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:30")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    # あえて schedule_day_plan / schedule_lives を呼ばない
    # (再起動直後のインメモリ予約消失を模す)。
    assert day_plan.find_lost_life_reservations(manager, PERSONA_ID, PLAN_DATE) == [
        (0, "start"), (0, "end"),
    ]

    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "reschedule"
    assert out["lives_pushed"] == 2
    assert day_plan.find_lost_life_reservations(manager, PERSONA_ID, PLAN_DATE) == []


def test_watchdog_noop_when_life_reservations_intact(manager, session_factory, task_ref):
    manager.personas[PERSONA_ID].activity_state = "Active"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(BASE + timedelta(hours=9))

    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:30")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    day_plan.schedule_lives(manager, PERSONA_ID, PLAN_DATE)

    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "none"}


# ---------------------------------------------------------------------------
# fire_judgment_point: 判断点発火のライフパルス積算
# ---------------------------------------------------------------------------


def test_fire_judgment_point_consumes_life_pulse(manager, session_factory):
    manager.personas[PERSONA_ID].activity_state = "Active"
    _import_judgment_playbooks(session_factory)
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "end": "23:00", "budget_pulses": 5, "mode": "free"},
    ])

    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is True

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 1


def test_fire_judgment_point_noop_life_pulse_without_lives(manager, session_factory):
    """lives が無い日は判断点発火してもライフ台帳に触らない (後方互換)。"""
    manager.personas[PERSONA_ID].activity_state = "Active"
    _import_judgment_playbooks(session_factory)
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))

    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


# ---------------------------------------------------------------------------
# day_open スキーマ: lives フィールド (judgment_points.build_day_open_schema)
# ---------------------------------------------------------------------------


class FakePulseController:
    """submit_meta_judgment の呼び出しを記録するだけのスタブ。"""

    def __init__(self):
        self.submissions: List[Dict[str, Any]] = []

    def submit_meta_judgment(
        self, persona_id, building_id, meta_playbook, args=None, event_callback=None
    ):
        self.submissions.append({
            "persona_id": persona_id, "building_id": building_id,
            "meta_playbook": meta_playbook, "args": args,
        })
        return None


def test_day_open_schema_includes_lives_field(manager):
    manager.pulse_controller = FakePulseController()
    clock.enable_virtual(datetime(2026, 7, 4, 7, 0, 0))
    result = jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    assert result["submitted"] is True

    args = manager.pulse_controller.submissions[0]["args"]
    schema = args["response_schema"]
    assert "lives" in schema["properties"]
    assert "lives" in schema["required"]
    life_schema = schema["properties"]["lives"]["items"]
    assert set(life_schema["required"]) == {"start", "end", "budget_pulses", "mode"}
    assert life_schema["properties"]["mode"]["enum"] == ["even", "free"]

    text = args["situation_text"]
    assert "ライフ" in text
    assert "50分以内" in text
    assert "自由」が向いています" in text  # model=None → 既定は自由


def test_day_open_situation_text_recommends_even_for_anthropic_model(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    manager.pulse_controller = FakePulseController()
    clock.enable_virtual(datetime(2026, 7, 4, 7, 0, 0))
    jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    text = manager.pulse_controller.submissions[0]["args"]["situation_text"]
    assert "均等」が向いています" in text


# ---------------------------------------------------------------------------
# day_open finalize: lives の保存 (judgment_finalize ツール, life.md Phase2)
# ---------------------------------------------------------------------------


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def test_day_open_finalize_saves_and_schedules_lives(manager, finalize_mod, tmp_path):
    output = {
        "monologue": "……",
        "lives": [
            {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        ],
        "timetable": [_slot("08:30", ref="none", kind="休む", facility="own_room",
                             budget_rounds=0)],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )
    assert "applied=True" in summary

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["mode"] == "free"
    assert lives[0]["budget_pulses"] == 6
    # 境界イベントは finalize が schedule_lives まで済ませている (途絶なし)
    assert day_plan.find_lost_life_reservations(manager, PERSONA_ID, PLAN_DATE) == []


def test_day_open_finalize_lives_failure_does_not_block_timetable(
    manager, finalize_mod, tmp_path, caplog
):
    """ライフの均等モード間隔違反は timetable の保存を巻き込まない (分離された失敗)。"""
    output = {
        "monologue": "……",
        "lives": [
            {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "even"},
        ],
        "timetable": [
            _slot("08:10", ref="none", kind="休む", facility="own_room", budget_rounds=0),
            _slot("10:30", ref="none", kind="休む", facility="own_room", budget_rounds=0),
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_open",
                judgment_context=ctx, situation_text="[起床判断] ...",
            )

    # timetable は保存される (140分 > 50分 のライフ間隔違反とは独立)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["08:10", "10:30"]
    # ライフは保存されない
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    assert any("上限 50 分を超えています" in r.message for r in caplog.records)


def test_day_open_finalize_missing_lives_is_harmless(manager, finalize_mod, tmp_path):
    """lives キーが無い出力 (旧 LLM 応答) でも timetable 保存は動く (後方互換)。"""
    output = {
        "monologue": "……",
        "timetable": [_slot("09:00", ref="none", kind="休む", facility="own_room",
                             budget_rounds=0)],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )
    assert "applied=True" in summary
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


# ---------------------------------------------------------------------------
# get_life_status_now: 「話しかけやすさ」表示の唯一の判定源 (life.md §9.1, Phase4)
# ---------------------------------------------------------------------------


def test_life_status_now_undeclared(manager):
    """lives 未宣言の日は lives_declared=False (「休止中」と嘘の表示をしない)。"""
    clock.enable_virtual(BASE + timedelta(hours=15))
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["lives_declared"] is False
    assert status["in_life"] is False
    assert status["life_index"] is None
    assert status["life"] is None


def test_life_status_now_in_life(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["lives_declared"] is True
    assert status["in_life"] is True
    assert status["life_index"] == 0
    assert status["life"]["start"] == "09:00"
    assert status["life"]["end"] == "11:00"


def test_life_status_now_in_valley(manager):
    """宣言はあるが区間外 (谷) は in_life=False で「未宣言」と区別される。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=12))
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["lives_declared"] is True
    assert status["in_life"] is False
    assert status["life_index"] is None
    assert status["life"] is None


def test_life_status_now_reflects_budget_consumption(manager):
    """予算値 (budget_pulses/used_pulses/used_rounds) がライフ状態にそのまま乗る。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:30")
    day_plan.consume_life_rounds(manager, PERSONA_ID, PLAN_DATE, 2, at_time="09:30")
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=45))
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["life"]["used_pulses"] == 1
    assert status["life"]["used_rounds"] == 2
    assert status["life"]["budget_pulses"] == 4


def test_life_status_now_defaults_undeclared_on_lookup_failure():
    """異常系 (SessionLocal を持たない manager) は lives_declared=False にフォールバック。

    is_keepalive_allowed の失敗時フォールバック (許可側=True) とは安全方向が
    逆——話しかけやすさ表示は「無い」方が「熱くないのに熱いと見せる」より安全
    (life.md 不変条件5)。
    """
    broken_manager = SimpleNamespace()
    status = day_plan.get_life_status_now(broken_manager, PERSONA_ID)
    assert status["lives_declared"] is False
    assert status["in_life"] is False
