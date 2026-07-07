"""P5: コマ参照の任意階層化 (track:N コマ) のテスト — life_concept_map.md §3.1。

コマは目的ノードを任意階層で指せる: task:N (具体) / desire:N (候補=お試し採用、
既存経路) に加えて track:N (大枝)。大枝コマは発火時に Track の title・机メモ・
配下の生存タスクから指示書を組んでセッションを回し、**中身が空の Track は
presence 相当に縮退**する。

- enum / スキーマ: collect_slot_ref_enum に track:N が載る
- 検証: sanitize_timetable が生きた track を通し、未知・終了済みを棄却する
- 保存: day_plan の保存バリデーションが track:N を受ける
- 実行: 指示書に Track 文脈が入り task_ref でなく track_id が渡る /
  post_session 判断に task_ref が漏れない / 空 Track は縮退して判断も予算も無し

fixtures は tests/test_day_plan.py の流儀 (in-memory SQLite + 仮想クロック)。
"""
from __future__ import annotations

from datetime import datetime
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
from saiverse import judgment_points as jp
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PARENT_TRACK, PersonaTaskManager
from saiverse.track_manager import TrackManager

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 9, 0, 0)


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
def _virtual_clock():
    clock.enable_virtual(BASE)
    yield
    clock.disable_virtual()


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
    )

    class StubOccupancy:
        def __init__(self):
            self.moves: List[tuple] = []

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            return True, "ok"

    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        occupancy_manager=StubOccupancy(),
        event_scheduler=EventScheduler(),
        track_manager=TrackManager(session_factory=session_factory),
        buildings=[SimpleNamespace(building_id="library", name="図書館")],
    )


@pytest.fixture
def running_track(manager):
    """track:1 (running・机メモとタスク付き = 中身のある大枝) を返す。"""
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous",
        title="言葉の標本集", intent="気に入った言い回しを集めて綴じる",
        initial_status="running",
    )
    ptm = PersonaTaskManager(manager.SessionLocal)
    ptm.create_task(
        persona_id=PERSONA_ID, title="序文の下書き", goal="書き出しを決める",
        parent_kind=PARENT_TRACK, track_id=track_id, auto_activate=False,
    )
    jp.save_desk_memo(manager, track_id, {
        "text": "第2章の途中まで書いた", "status": "continue",
        "task_ref": "task:1", "updated_at": "2026-07-03T22:00:00",
    })
    return track_id


@pytest.fixture
def empty_track(manager):
    """track:1 (running・タスクも机メモも無い = 中身が空の大枝) を返す。"""
    return manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous",
        title="なんとなく気になること", initial_status="running",
    )


def _track_slot(ref="track:1", **over):
    slot = {
        "start": "10:00", "kind": "作る", "ref": ref,
        "facility": "own_room", "budget_rounds": 4,
        "title": "標本集を進める", "note": "続きから",
    }
    slot.update(over)
    return slot


def _mock_ws_result(**over):
    base = dict(
        digest="d", artifacts=[], rounds_used=2, ended_reason="finished",
        task_ref=None, track_id=None, episode_ref=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# enum / 検証 / 保存
# ---------------------------------------------------------------------------


def test_collect_slot_ref_enum_includes_track_refs(manager, running_track):
    refs = jp.collect_slot_ref_enum(manager, PERSONA_ID)
    assert "track:1" in refs
    assert "task:1" in refs  # Track 配下の生存タスクもバックログとして載る
    assert refs[-1] == "none"


def test_sanitize_timetable_accepts_live_track_ref(manager, running_track):
    slots, warnings = jp.sanitize_timetable(manager, PERSONA_ID, [_track_slot()])
    assert len(slots) == 1
    assert slots[0]["ref"] == "track:1"
    assert warnings == []


def test_sanitize_timetable_rejects_unknown_track_ref(manager, running_track):
    slots, warnings = jp.sanitize_timetable(
        manager, PERSONA_ID, [_track_slot(ref="track:9")],
    )
    assert slots == []
    assert any("track:9" in w for w in warnings)


def test_sanitize_timetable_rejects_terminal_track_ref(manager, running_track):
    manager.track_manager.complete(running_track)
    slots, warnings = jp.sanitize_timetable(manager, PERSONA_ID, [_track_slot()])
    assert slots == []
    assert any("completed" in w for w in warnings)


def test_save_day_plan_accepts_track_ref(manager, running_track):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_track_slot()])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["ref"] == "track:1"


# ---------------------------------------------------------------------------
# 実行: 大枝コマ = Track 文脈の指示書でセッションを回す
# ---------------------------------------------------------------------------


def test_track_slot_builds_instruction_from_track_context(manager, running_track):
    calls: List[Dict[str, Any]] = []

    def fake_ws(persona_id, instruction, budget_rounds, task_ref=None,
                metadata=None, *, manager=None, track_id=None, title=None):
        calls.append({
            "instruction": instruction, "task_ref": task_ref,
            "track_id": track_id, "budget_rounds": budget_rounds,
        })
        return _mock_ws_result(track_id=track_id)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        result = day_plan.run_worker_slot_session(
            manager, PERSONA_ID, PLAN_DATE, _track_slot(), 0,
        )

    assert result is not None
    assert len(calls) == 1
    call = calls[0]
    # 対象タスクは無い (Track 単位の取り組み)。track_id で判断点へ繋がる
    assert call["task_ref"] is None
    assert call["track_id"] == running_track
    # 指示書に Track 文脈: title・意図・机メモ・配下の生存タスク・接地文言
    inst = call["instruction"]
    assert "言葉の標本集" in inst
    assert "気に入った言い回しを集めて綴じる" in inst
    assert "第2章の途中まで書いた" in inst
    assert "序文の下書き" in inst
    assert "実際にやったこと以外を「やった」と書かないこと" in inst


def test_track_slot_post_session_context_has_no_task_ref(manager, running_track):
    """post_session 判断に track 参照が task_ref として漏れない。"""
    judged: List[Dict[str, Any]] = []

    def fake_fire(mgr, persona_id, kind, context=None, **kw):
        judged.append({"kind": kind, "context": context})
        return {"submitted": True}

    with patch("sea.work_session.run_work_session",
               return_value=_mock_ws_result(track_id=running_track)), \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=fake_fire):
        used = day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE, _track_slot(), 0,
        )

    assert used == 2  # rounds_used が予算台帳へ返る
    assert len(judged) == 1
    ctx = judged[0]["context"]
    assert "task_ref" not in ctx
    assert getattr(ctx["session_result"], "track_id", None) == running_track


def test_empty_track_slot_degrades_to_presence(manager, empty_track):
    """中身が空の Track コマ: セッションも判断も走らず presence 縮退する。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_track_slot()])
    judged: List[Any] = []

    with patch("sea.work_session.run_work_session") as mock_ws, \
         patch("saiverse.autonomy_wiring.fire_judgment_point",
               side_effect=lambda *a, **k: judged.append(a)):
        used = day_plan._handle_worker_slot(
            manager, PERSONA_ID, PLAN_DATE,
            day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0], 0,
        )

    assert used == 0                      # 予算を消費しない
    assert not mock_ws.called             # セッションは回さない
    assert judged == []                   # 偽前提の判断を撃たない
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0].get("record_level") == day_plan.RECORD_LEVEL_PRESENCE_ONLY


def test_task_ref_slot_still_uses_template_path(manager, running_track):
    """task:N コマは従来の型別テンプレート経路のまま (回帰)。"""
    calls: List[Dict[str, Any]] = []

    def fake_ws(persona_id, instruction, budget_rounds, task_ref=None,
                metadata=None, *, manager=None, track_id=None, title=None):
        calls.append({"instruction": instruction, "task_ref": task_ref,
                      "track_id": track_id})
        return _mock_ws_result(task_ref=task_ref)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        day_plan.run_worker_slot_session(
            manager, PERSONA_ID, PLAN_DATE, _track_slot(ref="task:1"), 0,
        )

    assert calls[0]["task_ref"] == "task:1"
    assert calls[0]["track_id"] is None
    assert "document_create で実際に作成すること" in calls[0]["instruction"]
