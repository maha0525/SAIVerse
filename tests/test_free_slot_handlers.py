"""出かける / 自室で過ごす / 自由時間 コマの実行本体 (時間割改修 T3) のテスト。

T1 の honest stub (presence 記録のみ) を置き換えた「実移動 + 一回の軽い一手
Pulse」の検証。Pulse は schedule 型経路 (PulseDispatcher.dispatch_schedule_fire)
のスタブで受け、LLM は呼ばない:

- 出かける: facility 確定ならそこへ / 穴 (own_room) なら公共施設から決定論で
  選ぶ (own_room 除外・選んだ行き先の slot 永続化)。移動は実発生。Pulse が
  起動できなければ presence_only の正直記録に縮退する
- 自室で過ごす: own_room へ移動 + Pulse。文面は場所と状況の提示のみで義務形
  (「〜してください」) を含まない — 充填独白の禁忌 (v2 §2.1) の回帰検査
- 自由時間: 開始時に本人が軽量構造化出力で選び、選んだ種別のハンドラへ委譲。
  選択失敗は自室で過ごす相当へ縮退 (WARNING)。作業セッション系へ委譲したら
  実測ラウンドを予算台帳へ積算する

fixture の流儀は test_day_plan.py と同じ (in-memory SQLite + SimpleNamespace
manager スタブ)。
"""
from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, User
from saiverse import clock, day_plan
from saiverse.event_scheduler import EventScheduler
from saiverse.track_manager import TrackManager

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
OWN_ROOM = "alice_room"


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


class StubDispatcher:
    """PulseDispatcher.dispatch_schedule_fire の型付き戻り値スタブ。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.result: Dict[str, Any] = {
            "action": "execute", "runtime_outcome": "completed", "error": None,
        }

    def dispatch_schedule_fire(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


@pytest.fixture
def manager(session_factory):
    """day_plan の T3 ハンドラが触る実属性のみの SAIVerseManager スタブ。

    buildings にロールタグ付きの公共施設 2 つ (cafe / library) と、タグなしの
    自室を置く — candidate_buildings はタグ付きのみを返す (v2 §6.1)。
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
        persona_name="Alice",
        current_building_id=OWN_ROOM,
        private_room_id=OWN_ROOM,
    )

    class StubOccupancy:
        def __init__(self, personas: Dict[str, Any]):
            self.moves: List[tuple] = []
            self._personas = personas
            self.fail_with: str | None = None

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            if self.fail_with is not None:
                return False, self.fail_with
            target = self._personas.get(entity_id)
            if target is not None:
                target.current_building_id = to_id
            return True, "ok"

    personas = {PERSONA_ID: persona}
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        buildings=[
            SimpleNamespace(building_id="cafe", name="カフェ", facility_roles=["plaza"]),
            SimpleNamespace(building_id="library", name="図書館", facility_roles=["library"]),
            SimpleNamespace(building_id=OWN_ROOM, name="アリスの部屋", facility_roles=[]),
        ],
        occupancy_manager=StubOccupancy(personas),
        event_scheduler=EventScheduler(),  # start() しない
        track_manager=TrackManager(session_factory=session_factory),
        pulse_dispatcher=StubDispatcher(),
    )


def _save_single_slot(manager, kind: str, facility: str, **over) -> Dict[str, Any]:
    slot = {
        "start": "10:00", "kind": kind, "ref": "none",
        "facility": facility, "budget_rounds": 0, "note": "",
    }
    slot.update(over)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [slot])
    return day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]


def _pulse_text(manager, i: int = 0) -> str:
    return manager.pulse_dispatcher.calls[i]["user_input"]


# ---------------------------------------------------------------------------
# 出かける
# ---------------------------------------------------------------------------


def test_outing_fixed_facility_moves_and_pulses(manager):
    """facility 確定の出かけるコマ: 確定先へ実移動し、軽い一手 Pulse が一回走る。"""
    _save_single_slot(manager, "出かける", "cafe")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    # 実移動 (presence) は本物
    assert manager.occupancy_manager.moves == [(PERSONA_ID, "ai", OWN_ROOM, "cafe")]
    # Pulse は一回。文面は場所の提示のみ (表示名で書かれる)
    assert len(manager.pulse_dispatcher.calls) == 1
    text = _pulse_text(manager)
    assert "出かけて" in text
    assert "カフェ" in text
    assert "ください" not in text  # 行動・発話の義務を課さない (充填独白の禁忌)
    call = manager.pulse_dispatcher.calls[0]
    assert call["building_id"] == "cafe"  # 移動後の現在地で Pulse
    assert call["metadata"]["source"] == "day_plan_slot"

    # Pulse が走ったので presence_only は付かない (「実行済み」表示が正直)
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert "record_level" not in slot


def test_outing_hole_picks_public_facility_excluding_own_room(manager):
    """穴 (own_room のまま) の出かけるコマ: 公共施設から決定論で選び、自室は除外。"""
    saved = _save_single_slot(manager, "出かける", "own_room")

    # 決定論: (persona, 日付, コマ id) の種で毎回同じ行き先に落ちる
    expected = random.Random(
        f"{PERSONA_ID}:{PLAN_DATE}:{saved['id']}"
    ).choice(["cafe", "library"])

    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert manager.occupancy_manager.moves == [(PERSONA_ID, "ai", OWN_ROOM, expected)]
    # 選んだ行き先は slot に永続化される (帳簿が実際の行き先を読める)
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["facility"] == expected
    assert slot["status"] == "done"
    assert "record_level" not in slot
    assert len(manager.pulse_dispatcher.calls) == 1


def test_outing_destination_resolution_is_deterministic(manager):
    """同じ (persona, 日付, コマ id) なら行き先解決は何度呼んでも同じ。"""
    saved = _save_single_slot(manager, "出かける", "own_room")
    first = day_plan._resolve_outing_destination(
        manager, PERSONA_ID, PLAN_DATE, 0, dict(saved))
    second = day_plan._resolve_outing_destination(
        manager, PERSONA_ID, PLAN_DATE, 0, dict(saved))
    assert first["facility"] == second["facility"]
    assert first["facility"] in ("cafe", "library")  # own_room は選ばれない


def test_outing_without_pulse_dispatcher_degrades_to_presence_only(manager):
    """Pulse が起動できない環境 (dispatcher 無し) は presence_only の正直記録。"""
    del manager.pulse_dispatcher
    _save_single_slot(manager, "出かける", "cafe")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    # 移動 (presence) だけは本物のまま
    assert manager.occupancy_manager.moves == [(PERSONA_ID, "ai", OWN_ROOM, "cafe")]
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert slot["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY


def test_outing_pulse_not_started_degrades_to_presence_only(manager):
    """dispatch は返ったが Pulse が起動していない (関所閉鎖等) → presence_only。"""
    manager.pulse_dispatcher.result = {
        "action": "skipped", "runtime_outcome": None, "error": None,
    }
    _save_single_slot(manager, "出かける", "cafe")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY


def test_outing_move_failure_text_stays_honest(manager):
    """移動失敗で自室に留まったら「出かけた」と偽らない文面になる。"""
    manager.occupancy_manager.fail_with = "満員です"
    _save_single_slot(manager, "出かける", "cafe")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    text = _pulse_text(manager)
    assert "出かけて、" not in text
    assert "移動できず" in text
    assert "アリスの部屋" in text  # 実際の現在地


def test_outing_move_failure_away_from_home_stays_honest(manager):
    """自室以外での移動失敗も「出かけた」体にしない (現在地の事実だけを提示)。"""
    manager.personas[PERSONA_ID].current_building_id = "cafe"
    manager.occupancy_manager.fail_with = "満員です"
    _save_single_slot(manager, "出かける", "library")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    text = _pulse_text(manager)
    assert "出かけて、" not in text
    assert "移動できず" in text
    assert "カフェ" in text  # 実際の現在地 (自室ではない)


def test_outing_no_candidates_does_not_move_backwards(manager):
    """行き先候補ゼロ: own_room を「自室へ移動」と誤読して逆移動しない。

    ペルソナが外出中 (cafe) で公共施設が一つも無い City の場合、以前は
    facility=own_room のまま _move_to_facility に渡って自室へ連れ戻された。
    移動ゼロ + 正直な文面 (「行ける場所が見つからない」) が正しい挙動。
    """
    manager.personas[PERSONA_ID].current_building_id = "cafe"
    manager.buildings = [
        SimpleNamespace(building_id=OWN_ROOM, name="アリスの部屋", facility_roles=[]),
    ]
    _save_single_slot(manager, "出かける", "own_room")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert manager.occupancy_manager.moves == []  # 逆移動しない
    text = _pulse_text(manager)
    assert "出かけて、" not in text
    assert "行ける場所が見つからない" in text
    # 実際の現在地 (cafe は buildings から消してあるので表示名は素の id に落ちる)
    assert "cafe" in text


def test_outing_no_candidates_without_private_room_no_fabrication(manager):
    """行き先候補ゼロ + 自室なし: 移動ゼロなのに「出かけて来ました」と書かない。"""
    manager.personas[PERSONA_ID].private_room_id = None
    manager.buildings = [
        SimpleNamespace(building_id="somewhere", name="どこか", facility_roles=[]),
    ]
    manager.personas[PERSONA_ID].current_building_id = "somewhere"
    _save_single_slot(manager, "出かける", "own_room")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert manager.occupancy_manager.moves == []
    text = _pulse_text(manager)
    assert "出かけて、" not in text
    assert "行ける場所が見つからない" in text


# ---------------------------------------------------------------------------
# 自室で過ごす
# ---------------------------------------------------------------------------


def test_stay_home_moves_home_and_pulses_with_permissive_wording(manager):
    """自室へ実移動し、文面は許可形の desire 一文つき・義務形なし。"""
    manager.personas[PERSONA_ID].current_building_id = "cafe"
    _save_single_slot(manager, "自室で過ごす", "own_room")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert manager.occupancy_manager.moves == [(PERSONA_ID, "ai", "cafe", OWN_ROOM)]
    assert len(manager.pulse_dispatcher.calls) == 1
    text = _pulse_text(manager)
    assert "自室で過ごす" in text
    # desire への積み込みは許可形 (intent §5.5「積んでいい」)。義務形は禁忌
    assert "やりたいこと候補に積んでおいてもいい" in text
    assert "してください" not in text
    assert "ください" not in text

    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert "record_level" not in slot


def test_stay_home_without_pulse_dispatcher_degrades_to_presence_only(manager):
    del manager.pulse_dispatcher
    _save_single_slot(manager, "自室で過ごす", "own_room")
    day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert slot["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY


# ---------------------------------------------------------------------------
# 自由時間
# ---------------------------------------------------------------------------


class _FakeStructuredClient:
    """自由時間の種別選択スタブ (構造化出力一発)。"""

    def __init__(self, kind: str):
        self.kind = kind
        self.calls: List[Dict[str, Any]] = []

    def generate(self, messages, tools=None, response_schema=None, **_):
        self.calls.append({"messages": messages, "response_schema": response_schema})
        return {"kind": self.kind}


def test_free_choice_delegates_to_chosen_outing(manager):
    """選択→委譲: 「出かける」を選んだら行き先解決 + 移動 + 出かける Pulse。"""
    client = _FakeStructuredClient("出かける")
    _save_single_slot(manager, "自由時間", "own_room", note="好きに過ごす")
    with patch.object(
        day_plan, "_resolve_free_choice_client", return_value=(client, "lite-model"),
    ):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    # 選択肢の enum はカタログから動的供給され、自由時間自身は含まれない
    assert len(client.calls) == 1
    enum = client.calls[0]["response_schema"]["properties"]["kind"]["enum"]
    assert "自由時間" not in enum
    assert "出かける" in enum and "自室で過ごす" in enum and "調べる" in enum
    # 方針メモは選択の材料として渡る
    prompt = client.calls[0]["messages"][0]["content"]
    assert "好きに過ごす" in prompt

    # 委譲: 行き先が解決されて実移動 + 出かける文面の Pulse
    assert len(manager.occupancy_manager.moves) == 1
    dest = manager.occupancy_manager.moves[0][3]
    assert dest in ("cafe", "library")
    assert "出かけて" in _pulse_text(manager)

    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert slot["kind"] == "自由時間"  # 帳簿上のコマ種別は変えない
    assert "record_level" not in slot


def test_free_choice_selection_failure_degrades_to_stay_home(manager, caplog):
    """選択 LLM の失敗は WARNING + 自室で過ごす相当へ縮退する。"""
    _save_single_slot(manager, "自由時間", "own_room")
    manager.personas[PERSONA_ID].current_building_id = "cafe"
    with patch.object(
        day_plan, "_choose_free_time_kind", return_value=None,
    ), caplog.at_level("WARNING", logger="saiverse.day_plan"):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert any(
        "free-choice slot" in r.message and "degrading to stay-home" in r.message
        for r in caplog.records
    )
    # 縮退でも own_room への実移動 + 自室 Pulse は走る
    assert manager.occupancy_manager.moves[-1][3] == OWN_ROOM
    assert "自室で過ごす" in _pulse_text(manager)
    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    assert "record_level" not in slot  # Pulse 自体は走った


def test_free_choice_delegated_work_session_consumes_budget(manager):
    """作業セッション系へ委譲したら実測ラウンドが予算台帳へ積算される。

    自由時間コマ自身は予算ゲート非対象 (_fire_slot の精算では数えられない)
    ため、委譲側の実測積算が唯一の記帳経路であることの検査。
    """
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 10)
    _save_single_slot(manager, "自由時間", "own_room")

    session_result = SimpleNamespace(
        digest="d", artifacts=[], rounds_used=3, ended_reason="finished",
    )
    with patch.object(
        day_plan, "_choose_free_time_kind", return_value="調べる",
    ), patch(
        "sea.work_session.run_work_session", return_value=session_result,
    ) as run_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert run_ws.call_count == 1
    # 指示書はカタログの「調べる」テンプレートから組まれる
    instruction = run_ws.call_args[0][1]
    assert "実際に読んで得られた内容だけ" in instruction

    state = day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)
    assert state == {"total": 10, "used": 3, "remaining": 7}

    slot = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]
    assert slot["status"] == "done"
    # 作業セッションが実際に走ったので presence_only ではない
    assert "record_level" not in slot


def test_free_choice_budget_exhausted_excludes_work_kinds(manager):
    """日次予算の残高 0 のとき、選択肢から作業セッション系が外れる。"""
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 5)
    day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 5)
    choices = day_plan._free_time_choices(manager, PERSONA_ID, PLAN_DATE)
    assert "調べる" not in choices
    assert "出かける" in choices and "自室で過ごす" in choices
