"""時間割 (saiverse/day_plan.py) のテスト — 保存・コマ発火配線 (自律行動 v2 §4.2)。

一時 DB (in-memory SQLite; Windows のファイルロック問題を構造的に回避) +
mock run_work_session + DaySimulator (仮想クロック) で検証する:

- 3 コマ (9:00 知る / 14:00 作る / 20:00 休む) が仮想時刻順に発火し、
  作る/知るコマが mock run_work_session を正しい指示書引数で呼ぶ
- ユーザー会話中 (running user_conversation Track) は繰り下げ → 10 分後に
  再発火。繰り下げ 3 回で skipped。会話終了 (pause) 後は再発火で実行される
- reschedule_pending_slots の冪等性 (二重 push で二重発火しない)
- バリデーション: 時刻降順・不正 kind・kind/ref 不整合などで save が ValueError

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

from database.models import AI, Base, City, PersonaDayPlan, User
from saiverse import clock
from saiverse import day_plan
from saiverse.day_simulator import DaySimulator
from saiverse.event_scheduler import EventScheduler
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PersonaTaskManager
from saiverse.track_manager import STATUS_RUNNING, TrackManager

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory (thread 跨ぎ共有可)。

    ファイルを作らないため Windows の SQLite ロック (teardown 時の削除失敗)
    を構造的に回避する。teardown で必ず dispose する。
    """
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


@pytest.fixture
def manager(session_factory):
    """SAIVerseManager の最小スタブ。

    day_plan が触る実属性のみ: SessionLocal / personas / occupancy_manager /
    event_scheduler / track_manager。
    """
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
    )

    class StubOccupancy:
        def __init__(self, personas: Dict[str, Any]):
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
    )


@pytest.fixture
def task_refs(manager):
    """task:1 (知る用) と desire:2 (作る用: desire ノート内の候補) を用意する。"""
    task_manager = PersonaTaskManager(manager.SessionLocal)
    t1 = task_manager.create_task(
        persona_id=PERSONA_ID,
        title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする",
        auto_activate=False,
    )
    note_id = NoteManager(session_factory=manager.SessionLocal).ensure_desire_note(PERSONA_ID)
    t2 = task_manager.create_task(
        persona_id=PERSONA_ID,
        title="言葉の標本集",
        goal="気に入った言い回しを document にまとめる",
        parent_kind=PARENT_NOTE,
        note_id=note_id,
        origin="autonomous",
        auto_activate=False,
    )
    assert t1["task_ref"] == "task:1"
    assert t2["task_ref"] == "task:2"
    return {"task": "task:1", "desire": "desire:2"}


def _mock_work_session_result(**over):
    base = dict(
        digest="digest", artifacts=[], rounds_used=1, ended_reason="finished",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _three_slots(task_refs) -> List[Dict[str, Any]]:
    return [
        {
            "start": "09:00", "kind": "知る", "ref": task_refs["task"],
            "facility": "library", "budget_rounds": 5,
            "note": "記事の続きを調べる",
        },
        {
            "start": "14:00", "kind": "作る", "ref": task_refs["desire"],
            "facility": "workshop", "budget_rounds": 12,
            "note": "標本集の下書きを作る",
        },
        {
            "start": "20:00", "kind": "休む", "ref": "none",
            "facility": "own_room", "budget_rounds": 0, "note": "",
        },
    ]


# ---------------------------------------------------------------------------
# 保存とバリデーション
# ---------------------------------------------------------------------------


def test_save_and_load_normalizes_slots(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, _three_slots(task_refs))
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert len(slots) == 3
    for slot in slots:
        assert slot["status"] == "pending"
        assert slot["defer_count"] == 0

    # 1 ペルソナ 1 日 1 行 (upsert)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, _three_slots(task_refs)[:1])
    db = manager.SessionLocal()
    try:
        rows = db.query(PersonaDayPlan).filter_by(
            persona_id=PERSONA_ID, plan_date=PLAN_DATE
        ).all()
        assert len(rows) == 1
        assert len(json.loads(rows[0].slots_json)) == 1
    finally:
        db.close()


def test_save_accepts_date_object(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, BASE.date(), _three_slots(task_refs))
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) is not None


@pytest.mark.parametrize(
    "mutate, match",
    [
        # 時刻降順
        (lambda s: s.__setitem__(1, {**s[1], "start": "08:00"}), "ascending"),
        # 同時刻も不可
        (lambda s: s.__setitem__(1, {**s[1], "start": "09:00"}), "ascending"),
        # 不正 kind
        (lambda s: s.__setitem__(0, {**s[0], "kind": "遊ぶ"}), "kind"),
        # 休む に ref が付いている (kind/ref 不整合)
        (lambda s: s.__setitem__(2, {**s[2], "ref": "task:1"}), "ref='none'"),
        # ref 書式不正
        (lambda s: s.__setitem__(0, {**s[0], "ref": "task-1"}), "ref"),
        # 時刻書式不正
        (lambda s: s.__setitem__(0, {**s[0], "start": "9:00"}), "HH:MM"),
        # facility 欠落
        (lambda s: s.__setitem__(0, {**s[0], "facility": ""}), "facility"),
        # budget_rounds 負値
        (lambda s: s.__setitem__(0, {**s[0], "budget_rounds": -1}), "budget_rounds"),
    ],
)
def test_save_rejects_invalid_slots(manager, task_refs, mutate, match):
    slots = _three_slots(task_refs)
    mutate(slots)
    with pytest.raises(ValueError, match=match):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, slots)


def test_save_rejects_empty_slots(manager):
    with pytest.raises(ValueError, match="non-empty"):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [])


def test_schedule_without_plan_returns_zero(manager):
    assert day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE) == 0


# ---------------------------------------------------------------------------
# 3 コマの仮想時刻発火 + 指示書引数
# ---------------------------------------------------------------------------


def test_three_slots_fire_in_virtual_time_order(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, _three_slots(task_refs))
    assert day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE) == 3

    calls: List[Dict[str, Any]] = []

    def fake_run_work_session(persona_id, instruction, budget_rounds, task_ref=None,
                              metadata=None, *, manager=None, track_id=None):
        calls.append({
            "at": clock.now(),
            "persona_id": persona_id,
            "instruction": instruction,
            "budget_rounds": budget_rounds,
            "task_ref": task_ref,
            "metadata": metadata,
        })
        return _mock_work_session_result(rounds_used=budget_rounds)

    with patch("sea.work_session.run_work_session", side_effect=fake_run_work_session):
        sim = DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=24),
        )
        total = sim.run()

    assert total == 3
    assert len(calls) == 2  # 休む は run_work_session を呼ばない

    # 知る (9:00): 仮想時刻・指示書・予算・task_ref
    learn = calls[0]
    assert learn["at"] == BASE + timedelta(hours=9)
    assert learn["persona_id"] == PERSONA_ID
    assert learn["budget_rounds"] == 5
    assert learn["task_ref"] == "task:1"
    assert "記事の続きを調べる" in learn["instruction"]          # slot.note
    assert "蒸留記事の続きを読む" in learn["instruction"]        # ref のタイトル
    assert "要点を覚え書きにする" in learn["instruction"]        # ref の goal
    assert "実際に調べて得られた内容だけ" in learn["instruction"]  # 接地文言

    # 作る (14:00)
    create = calls[1]
    assert create["at"] == BASE + timedelta(hours=14)
    assert create["budget_rounds"] == 12
    assert create["task_ref"] == "desire:2"
    assert "標本集の下書きを作る" in create["instruction"]
    assert "言葉の標本集" in create["instruction"]  # desire:2 のタイトル
    assert "document_create で実際に作成すること" in create["instruction"]
    assert "完成条件: 成果物が実在し、読み直して整えてあること" in create["instruction"]

    # 施設移動: 自室→図書館→工房→自室 (own_room は private_room_id に解決)
    assert manager.occupancy_manager.moves == [
        (PERSONA_ID, "ai", "alice_room", "library"),
        (PERSONA_ID, "ai", "library", "workshop"),
        (PERSONA_ID, "ai", "workshop", "alice_room"),
    ]

    # status 更新: 全コマ done
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done", "done"]


# ---------------------------------------------------------------------------
# ユーザー会話中の繰り下げ
# ---------------------------------------------------------------------------


def _start_user_conversation(manager) -> str:
    """running な対ユーザー会話 Track を作る (= 会話中の状態)。"""
    return manager.track_manager.create(
        persona_id=PERSONA_ID,
        track_type="user_conversation",
        title="対 tester 会話",
        is_persistent=True,
        output_target="building:current",
        metadata=json.dumps({"user_id": "1"}),
        initial_status=STATUS_RUNNING,
    )


def test_slot_deferred_three_times_then_skipped(manager, task_refs):
    _start_user_conversation(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session") as mock_ws:
        sim = DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=12),
        )
        sim.run()

    # 9:00/9:10/9:20 で繰り下げ (3 回)、9:30 の 4 回目衝突で skipped
    mock_ws.assert_not_called()
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["defer_count"] == 3
    # 会話中は施設移動もしない
    assert manager.occupancy_manager.moves == []
    # キューに残イベントが無い (skipped 後は再 push しない)
    assert manager.event_scheduler.pending_count() == 0


def test_deferred_slot_fires_after_conversation_ends(manager, task_refs):
    track_id = _start_user_conversation(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result()) as mock_ws:
        # 9:00 の発火は会話中 → 9:10 へ繰り下げ
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=9, minutes=5),
        ).run()
        assert mock_ws.call_count == 0
        slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
        assert slots[0]["status"] == "deferred"
        assert slots[0]["defer_count"] == 1

        # 会話終了 (wait_response timeout 相当: running → pending)
        manager.track_manager.pause(track_id)

        # 9:10 の再発火で実行される
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=9, minutes=5),
            end=BASE + timedelta(hours=10),
        ).run()

    assert mock_ws.call_count == 1
    assert clock.now() == BASE + timedelta(hours=10)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert slots[0]["defer_count"] == 1


# ---------------------------------------------------------------------------
# reschedule_pending_slots の冪等性
# ---------------------------------------------------------------------------


def test_reschedule_pending_slots_is_idempotent(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        {"start": "20:00", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    # reschedule は clock.now() の日付で当日 plan を引くため、仮想時刻を先に立てる
    clock.enable_virtual(BASE + timedelta(hours=8))

    # 二重 push (schedule + reschedule x2): 同 key 上書きなので二重発火しない
    assert day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE) == 2
    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID) == 2
    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID) == 2
    assert manager.event_scheduler.pending_count() == 2

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result()) as mock_ws:
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=24),
        ).run()

    assert mock_ws.call_count == 1  # 知る 1 回のみ (二重発火しない)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]


def test_reschedule_repushes_deferred_and_skips_done(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの",
         "status": "deferred", "defer_count": 1},
        {"start": "14:00", "kind": "休む", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "",
         "status": "done"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=10))
    # deferred は再 push 対象、done は対象外
    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID) == 1

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result()) as mock_ws:
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=10),
            end=BASE + timedelta(hours=11),
        ).run()

    # 開始時刻 (9:00) は過去 → 即時扱いで発火
    assert mock_ws.call_count == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


# ---------------------------------------------------------------------------
# 未登録 kind / ハンドラ失敗
# ---------------------------------------------------------------------------


def test_unregistered_kind_is_skipped_with_warning(manager, task_refs, caplog):
    # 「経験する」は本フェーズではハンドラ未登録
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "経験する", "ref": task_refs["task"],
         "facility": "park", "budget_rounds": 0, "note": "公園を歩く"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with caplog.at_level("WARNING", logger="saiverse.day_plan"):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert any("no handler registered" in r.message for r in caplog.records)
    # 未登録 kind では施設移動しない
    assert manager.occupancy_manager.moves == []


def test_handler_failure_leaves_slot_fired(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "作る", "ref": "none",
         "facility": "workshop", "budget_rounds": 3, "note": "下書き"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session", side_effect=RuntimeError("boom")):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    # fired のまま残す (再発火しない安全側)。watchdog の再 push 対象にもならない
    assert slots[0]["status"] == "fired"
    clock.disable_virtual()
    clock.enable_virtual(BASE + timedelta(hours=10))
    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID) == 0
