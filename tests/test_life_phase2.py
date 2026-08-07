"""ライフ Phase 2「ライフの器」のテスト (docs/intent/life.md v0.5 §4/§5/§7/§11.2)。

v0.4 まではペルソナ (LLM) が起床判断でライフを「宣言」していたが、実機初日
(2026-07-13) の破綻を受けてまはー裁定で責任分界を全面改訂した——ライフは
ユーザー設定 (PersonaSchedule の起床・就寝) からシステムが確定する。宣言口・
重なり検証・谷コマ検証・均等モード間隔検証は書ける口ごと廃止した (v0.5 改修A)。
本ファイルは生き残った「ライフの器」(永続化・台帳・予算ゲート) のテスト。
システムによるライフ確定 (confirm_life_for_today) や判断点発火まわりの新規
テストは ``tests/test_life_confirmation.py`` を参照。

一時 DB (in-memory SQLite) + 仮想クロックで検証する:

- lives の永続化 (get_lives/save_lives の round trip、bookkeeping フィールドの
  既定値・再宣言時の引き継ぎ)
- 検証: フォーマットのみ (v0.5 で重なり・谷コマ・均等モード間隔検証は廃止)
- mode の既定導出 (derive_default_life_mode): DEFAULT_MODEL の provider から
- 予算台帳のライフ世代交代: consume_life_pulse / consume_life_rounds の積算、
  get_budget_state のライフ由来導出、ライフ単位ゲート (二値・クランプしない)
- 判断点発火 (fire_judgment_point) は used_pulses でなく judgment_pulses を
  別枠で記帳する (v0.5 §5.3/§8.2)
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

from database.models import AI, Base, City, Playbook, User
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
    # 本物と同じ契約 (W7 柱5): 成功時に persona 属性を service 側で更新する。
    def __init__(self, personas):
        self.moves: List[tuple] = []
        self._personas = personas

    def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
        self.moves.append((entity_id, entity_type, from_id, to_id))
        persona = self._personas.get(entity_id)
        if persona is not None:
            persona.current_building_id = to_id
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


def _slot(start, *, kind="調べる", ref="task:1", facility="library",
          budget_rounds=5, note=""):
    return {"start": start, "kind": kind, "ref": ref,
            "facility": facility, "budget_rounds": budget_rounds, "note": note}


def _default_lives():
    # mode は両方 "free" にしておく (round trip / 重なり無しの共存だけが関心事)。
    return [
        {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
    ]


def _import_judgment_playbooks(session_factory):
    db = session_factory()
    try:
        for name in wiring.JUDGMENT_PLAYBOOK_NAMES:
            db.add(Playbook(name=name, schema_json="{}", nodes_json="{}"))
        db.commit()
    finally:
        db.close()


def _set_day_schedules(manager, session_factory, wake="07:00", close="23:00"):
    """起床・就寝の PersonaSchedule 行を作る (day_open のライフ確定に必要)。"""
    from database.models import PersonaSchedule

    db = session_factory()
    try:
        for playbook, tod in (
            ("judgment_day_open", wake), ("judgment_day_close", close),
        ):
            db.add(PersonaSchedule(
                PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
                META_PLAYBOOK=playbook, ENABLED=True, TIME_OF_DAY=tod,
            ))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 永続化: get_lives / save_lives round trip
# ---------------------------------------------------------------------------


def test_get_lives_empty_when_not_declared(manager):
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_save_and_get_lives_round_trip(manager, task_ref):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("09:00"), _slot("15:00", ref="none", kind="自室で過ごす", facility="own_room",
              budget_rounds=0),
    ])
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert [(l["start"], l["end"]) for l in saved] == [
        ("08:00", "12:00"), ("14:00", "20:00"),
    ]
    for life in saved:
        assert life["used_pulses"] == 0
        assert life["used_rounds"] == 0
        assert life["judgment_pulses"] == 0
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
    life = day_plan.record_judgment_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["judgment_pulses"] == 1

    # 起床判断のやり直し: 同じ (start, end) のライフを再宣言 → 消費は引き継がれる
    resaved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert resaved[0]["used_pulses"] == 1
    assert resaved[0]["used_rounds"] == 3
    assert resaved[0]["judgment_pulses"] == 1

    # 時刻が変わったライフは別物 (0 から)
    changed = [
        {"start": "08:00", "end": "13:00", "budget_pulses": 6, "mode": "free"},
    ]
    resaved2 = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, changed)
    assert resaved2[0]["used_pulses"] == 0
    assert resaved2[0]["used_rounds"] == 0
    assert resaved2[0]["judgment_pulses"] == 0


# ---------------------------------------------------------------------------
# 検証: フォーマットのみ (v0.5: 重なり・谷コマ・均等モード間隔検証は廃止)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda l: l.__setitem__(0, {**l[0], "start": "8:00"}), "HH:MM"),
        (lambda l: l.__setitem__(0, {**l[0], "start": "08:00", "end": "08:00"}),
         "start と end が同一"),
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


def test_save_lives_accepts_overlapping_lives(manager):
    """v0.5: ライフ同士の重なり検証は廃止 (システムが 1 日 1 窓を確定するため
    通常は起きないが、書ける口として禁止する理由も無くなった)。"""
    lives = [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
        {"start": "11:00", "end": "15:00", "budget_pulses": 4, "mode": "free"},
    ]
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, lives)
    assert len(saved) == 2


def test_save_lives_accepts_overnight_life(manager):
    """v0.5: 深夜跨ぎ (end <= start) は異常ではなく正常形 (life.md §4.1)。"""
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "01:00", "budget_pulses": 20, "mode": "even"},
    ])
    assert saved[0]["start"] == "07:00"
    assert saved[0]["end"] == "01:00"
    assert day_plan.get_life_for_time(saved, "23:30") == 0
    assert day_plan.get_life_for_time(saved, "03:00") is None
    assert day_plan.get_life_for_time(saved, "07:00") == 0
    assert day_plan.get_life_for_time(saved, "01:00") is None  # end は排他的


def test_save_lives_no_longer_inspects_slots(manager, task_ref):
    """v0.5: save_lives はもう day_plan の既存コマを見ない (谷コマ検証の廃止)。

    かつては save_day_plan で置いたコマ (13:00) がライフの外にあると
    save_lives 自体が拒否したが、今はライフの確定と時間割の整合は
    save_day_plan/replace_remaining_slots 側 (_check_slots_within_organized_range)
    だけが見る。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("13:00")])
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    assert len(saved) == 1


def test_save_day_plan_excludes_slot_outside_life_window_when_sole_slot(manager, task_ref):
    """save_day_plan は既存ライフの外 (谷) のコマを丸めようが無ければ除外する。

    v0.5 追補 (2026-07-14): 唯一のコマが除外されると生き残りが 0 件になり、
    「編成できる範囲に収まるコマが無かった」として raise する (旧「どのライフ
    区間にも属していません」相当。3 分のズレで全滅させない設計であって、
    範囲外そのものを許すわけではない)。"""
    clock.enable_virtual(BASE + timedelta(hours=8))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    with pytest.raises(ValueError, match="活動時間の外"):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            _slot("13:00", ref="none", kind="自室で過ごす", facility="own_room",
                  budget_rounds=0),
        ])
    # save_lives が meta 行 (slots_json="[]") を既に作っているため空配列
    # (行そのものが無い None ではない — save_day_plan は失敗して何も書かない)。
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == []


def test_save_day_plan_rounds_slot_before_current_time_to_now(manager, task_ref):
    """v0.5 追補 (2026-07-14、実機初日の破綻の再現): 遅発 day_open でライフの
    中でも「今より前」のコマは、拒否するのでなく現在時刻へ丸めて保存する
    (life.md §3「不正な値は弾くのでなく解釈で正規化する」)。旧挙動 (raise) は
    3 分のズレで一日の時間割を全滅させていた。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 10, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=21))  # 21 時起動 (遅発)
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("08:00", ref="none", kind="自室で過ごす", facility="own_room",
              budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["21:00"]  # 現在時刻へ丸め
    assert notes == ["（1番目の予定は開始時刻を21:00に調整しました）"]

    # 現在時刻以降のコマはそのまま通る (無調整)
    notes2 = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("21:30", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["21:30"]
    assert notes2 == []


def test_save_day_plan_rounds_multiple_past_slots_keeping_ascending_order(manager, task_ref):
    """実機ケースの再現: ライフ 01:00〜02:00・現在 01:03・slot[0]=01:00 が
    01:03 へ丸められる。複数コマが丸めで同時刻に競合する場合は元の順序を
    保ったまま 1 分ずつ後ろへずらし、厳密昇順を保つ (life.md §3 追補)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "01:00", "end": "02:00", "budget_pulses": 4, "mode": "even"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=1, minutes=3))  # 01:03
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("01:00", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
        _slot("01:02", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["01:03", "01:04"]
    assert notes == [
        "（1番目の予定は開始時刻を01:03に調整しました）",
        "（2番目の予定は開始時刻を01:04に調整しました）",
    ]


def test_save_day_plan_partial_rescue_excludes_unroundable_slot(manager, task_ref):
    """部分救済: 丸めても範囲に入らないコマ (就寝後) だけを個別に除外し、
    残りは保存する (life.md §3 追補)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 8, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=8, minutes=5))  # 08:05
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("08:00", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
        _slot("10:00"),
        _slot("13:00", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    # 1番目は丸め、2番目はそのまま、3番目 (就寝後) だけ除外される
    assert [s["start"] for s in slots] == ["08:05", "10:00"]
    assert notes == [
        "（1番目の予定は開始時刻を08:05に調整しました）",
        "（3番目の予定は活動時間の外のため外しました）",
    ]


def test_save_day_plan_rounds_across_overnight_life(manager, task_ref):
    """深夜跨ぎライフ (07:00〜01:00) での丸め判定: 深夜帯の過去コマも現在時刻
    へ丸められる (life.md §3 追補)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "01:00", "budget_pulses": 20, "mode": "even"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=24, minutes=10))  # 翌日 00:10 (深夜帯)
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("23:50", ref="none", kind="自室で過ごす", facility="own_room", budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["00:10"]
    assert notes == ["（1番目の予定は開始時刻を00:10に調整しました）"]


def test_day_order_minutes_puts_bedtime_last_in_overnight_life(manager):
    """深夜跨ぎライフでは「一日の流れ」順が暦の時刻順と一致しない。

    2026-07-14 実機事故の核: 就寝 "00:30" は朝 "07:30" より数字が小さいが、
    一日の流れでは**後**に来る。
    """
    lives = [{"start": "07:00", "end": "01:00", "budget_pulses": 20, "mode": "even"}]
    assert day_plan.day_order_minutes(lives, "07:00") == 0
    assert day_plan.day_order_minutes(lives, "07:30") == 30
    assert day_plan.day_order_minutes(lives, "00:30") == 1050
    # 暦順では逆転するが、一日の流れ順では就寝が最後になる。
    assert "00:30" < "07:30"
    assert day_plan.day_order_minutes(lives, "00:30") > day_plan.day_order_minutes(lives, "07:30")
    # ライフ未宣言の日は暦の時刻順へ退化する (後方互換)。
    assert day_plan.day_order_minutes([], "00:30") == 30
    assert day_plan.day_order_minutes([], "07:30") == 450


def test_save_day_plan_keeps_overnight_timetable_intact(manager, task_ref):
    """2026-07-14 実機事故の再現防止 (air_city_a, ライフ 07:00〜01:00)。

    起床直後 (07:00) に「朝 → 日中 → 深夜の就寝」という一日の流れ順の時間割を
    保存する。事故当時は暦の時刻順を強制していたため、就寝 "00:30" を先頭に
    置かないと保存が通らず、先頭に置くと丸めが後続コマを 1 分刻みに押し込んで
    一日が 00:30〜00:35 に潰れた。修正後は流れ順のまま素通りし、**一切丸め
    られない**こと。
    """
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "01:00", "budget_pulses": 20, "mode": "even"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=7))  # 07:00 = 起床判断の時刻
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("07:30", kind="出かける", ref="none", facility="own_room"),
        _slot("09:00"),
        _slot("13:00"),
        _slot("00:30", kind="自室で過ごす", ref="none", facility="own_room", budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["07:30", "09:00", "13:00", "00:30"]
    assert notes == []


def test_sanitize_timetable_sorts_bedtime_last_in_overnight_life(manager, task_ref):
    """事故の入口: LLM が就寝を暦順で先頭に置いて返しても、一日の流れ順に直す。

    2026-07-14 実機では sanitize_timetable が時刻の文字列でソートしていたため、
    就寝 "00:30" が先頭に固定され、後段の丸めが一日を潰した。
    """
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "01:00", "budget_pulses": 20, "mode": "even"},
    ])
    raw = [
        {"start": "00:30", "kind": "自室で過ごす", "ref": "none", "facility": "own_room",
         "budget_rounds": 0, "title": "眠る"},
        {"start": "07:30", "kind": "出かける", "ref": "none", "facility": "own_room",
         "budget_rounds": 1, "title": "朝のルーティン"},
        {"start": "13:00", "kind": "調べる", "ref": "task:1", "facility": "own_room",
         "budget_rounds": 3, "title": "調べる"},
    ]
    slots, warnings = jp.sanitize_timetable(manager, PERSONA_ID, raw, PLAN_DATE)
    assert [s["start"] for s in slots] == ["07:30", "13:00", "00:30"]
    assert warnings == []
    # plan_date を渡さない場合は暦順に退化する (ライフ未宣言の日と同じ後方互換)。
    legacy, _ = jp.sanitize_timetable(manager, PERSONA_ID, raw)
    assert [s["start"] for s in legacy] == ["00:30", "07:30", "13:00"]


def test_save_day_plan_allows_slot_when_no_lives_declared(manager, task_ref):
    """ライフ宣言の無い日は谷の概念自体が無い (後方互換)。"""
    notes = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("23:50")])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["start"] == "23:50"
    assert notes == []


def test_replace_remaining_slots_excludes_slot_outside_life_window_when_sole_slot(
    manager, task_ref,
):
    """日中の残り時間割の全置換 (post_conversation 等) も編成できる範囲を守る。
    唯一の新コマが除外されると raise する (旧「どのライフ区間にも属していません」相当)。"""
    clock.enable_virtual(BASE + timedelta(hours=8))
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00")])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    with pytest.raises(ValueError, match="活動時間の外"):
        day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
            _slot("15:00", ref="none", kind="自室で過ごす", facility="own_room",
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
    # 発火後、そのライフのラウンド消費が積算される (v0.5: コマ発火自体は
    # 標準パルスを消費しない — used_pulses は不変)
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 0
    assert lives[0]["used_rounds"] == 20


def test_life_budget_gate_allows_through_when_slot_outside_any_life(manager, task_ref):
    """save_lives の谷検証を経ずに直接台帳を触った防御的経路 (通す + WARN)。

    NOTE: 予約の暦日補正は当日確定ライフの開始を基準にする (Codex 七巡目、
    _resolve_wake_for_plan)。この契約外状態 (コマ 09:00 / ライフ 13:00〜) では
    09:00 は「営業日の深夜帯明け = 翌暦日の朝」と解釈されて翌日 09:00 に発火
    する — 並び・検証と同じ物差しの帰結。防御 (ゲートは通す + WARN) 自体は
    変わらない。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=5)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    # ライフを直接組み替える (異常系の再現 — 発火時に slot がどのライフにも
    # 属さない状態を作る)。
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {
        day_plan.META_LIVES: [
            {"start": "13:00", "end": "18:00", "budget_pulses": 4, "mode": "free",
             "used_pulses": 0, "used_rounds": 0, "judgment_pulses": 0},
        ],
    })
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session") as mock_ws:
        mock_ws.return_value = _mock_result(rounds_used=5)
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(days=1, hours=10),
        ).run()
    mock_ws.assert_called_once()
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


# ---------------------------------------------------------------------------
# fire_judgment_point: 判断点発火のライフ「別枠」記帳 (used_pulses は不変)
# ---------------------------------------------------------------------------


def test_fire_judgment_point_records_judgment_pulse_separately(manager, session_factory):
    """判断点の発火は judgment_pulses だけを積む — used_pulses (予算) には
    触れない (life.md v0.5 §5.3/§8.2、実機初日の教訓)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
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
    assert lives[0]["judgment_pulses"] == 1
    assert lives[0]["used_pulses"] == 0


def test_fire_judgment_point_noop_life_bookkeeping_without_lives(manager, session_factory):
    """lives が無い日は判断点発火してもライフ台帳に触らない (後方互換)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))

    result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


# ---------------------------------------------------------------------------
# _apply_life_end_at_day_close: 境界副作用の冪等ガード (Codex W3 第二陣 P1)
# ---------------------------------------------------------------------------


def _end_test_lives(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "end": "23:00", "budget_pulses": 5, "mode": "free"},
    ])


def test_apply_life_end_at_day_close_applies_once_per_business_day(manager):
    """day_close の節目処理 (非冪等) は (persona, 営業日) につき一度だけ。

    判断 runtime の失敗で claim 行が prepared のまま残ると、schedule 側の
    backoff 再試行が fire_judgment_point を再突入させ、境界副作用 (keep-alive
    cancel + TTL 同期 + 「（活動終了）」通知) が再適用されていた (Codex W3
    第二陣 P1 の再現固定)。lives[0].ended マーカーで 2 回目以降は skip。
    """
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(day_plan, "_handle_life_end") as spy:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)

    assert spy.call_count == 1
    # マーカーは meta_json に永続化される — プロセスを跨いだ別インスタンス
    # 経由の再呼び出しでも DB 読み (get_lives) で skip される
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["ended"] is True


def test_apply_life_end_at_day_close_noop_without_lives(manager):
    """lives が無い日は従来どおり no-op (マーカーも書かない・行も作らない)。"""
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))

    with patch.object(day_plan, "_handle_life_end") as spy:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)

    assert spy.call_count == 0
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_apply_life_end_marker_not_written_when_handler_fails(manager):
    """順序は「確認 → 適用 → マーク」— 適用失敗ではマークしない (マーク先行
    だと適用されないまま封印される)。次の再試行が適用をやり直し、成功して
    はじめてマークされる。"""
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(
        day_plan, "_handle_life_end", side_effect=RuntimeError("boom"),
    ) as spy:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
    assert spy.call_count == 1
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")

    # 再試行: 適用成功 → マーク → 以後は skip
    with patch.object(day_plan, "_handle_life_end") as spy2:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
    assert spy2.call_count == 1
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True


def test_apply_life_end_marker_not_written_on_partial_failure(manager):
    """部分失敗 (通知の追記失敗など) では ended マークしない (Codex W3 第四陣
    P2 の再現固定)。下請け各段は例外を握るため、_handle_life_end の bool 戻りが
    False なら「適用済み」封印をせず、再試行で回復できる状態を残す。通知は
    最後段なので、再試行しても重複しない (失敗した回は追記されていない)。"""
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    # 1 回目: 通知の追記が失敗 (False) → マークされない
    with patch.object(
        day_plan, "_notify_life_boundary", return_value=False,
    ) as notify_fail:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
    assert notify_fail.call_count == 1
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")

    # 再試行: 通知成功 → マーク → 以後 skip (通知の総成功回数は 1)
    with patch.object(
        day_plan, "_notify_life_boundary", return_value=True,
    ) as notify_ok:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)
    assert notify_ok.call_count == 1
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True


def test_life_end_notify_skipped_when_ttl_sync_fails(manager):
    """順序契約: 冪等段 (TTL 解除予約) の失敗は非冪等な通知の**前**に打ち切る —
    再試行で冪等段が再実行されても通知は重複ゼロ (Codex W3 第四陣 P2)。"""
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(
        day_plan, "_sync_cache_ttl_for_life_end", return_value=False,
    ), patch.object(day_plan, "_notify_life_boundary") as notify:
        wiring._apply_life_end_at_day_close(manager, PERSONA_ID)

    assert notify.call_count == 0
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")


def _attach_boundary_ledger(manager, handler=None):
    """W5 境界テスト用: 台帳を付け、saimemory.append の配送ハンドラを登録する。"""
    from saiverse import execution_ledger as XL

    manager.execution_ledger = XL.ExecutionLedger(manager.SessionLocal)
    delivered: List[Dict[str, Any]] = []
    if handler is None:
        def handler(item):
            delivered.append(item["payload"]["message"])
    manager.execution_ledger.register_outbox_handler("saimemory.append", handler)
    return manager.execution_ledger, delivered


def _boundary_end_rows(manager):
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        rows = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == day_plan.LIFE_BOUNDARY_KIND_END)
            .all()
        )
        return [(r.EXECUTION_ID, r.STATUS) for r in rows]
    finally:
        db.close()


def test_life_boundary_marker_and_notice_single_commit(manager):
    """W5 正常系: 台帳経路では直接 append (_notify_life_boundary) は使われず、
    ended マーカー + applied + 「（活動終了）」通知 outbox が単一 commit で
    確定し、即時配送される (実行は全配送済みで completed)。"""
    ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(day_plan, "_notify_life_boundary") as legacy_notify:
        assert wiring._apply_life_end_at_day_close(manager, PERSONA_ID) is True
    assert legacy_notify.call_count == 0
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert len(delivered) == 1
    msg = delivered[0]
    assert "（活動終了）" in msg["content"]
    assert "day_plan" in msg["metadata"]["tags"]
    assert msg.get("timestamp")  # enqueue 時に時刻を凍結 (配送遅延でずれない)
    assert [s for _eid, s in _boundary_end_rows(manager)] == ["completed"]


def test_life_end_marker_tx_failure_fails_boundary_then_recovers(manager):
    """W5: マーカー tx (マーカー + applied + outbox の単一 commit) の失敗は
    境界 False + 台帳 failed — 旧「即時リトライ 1 回 + 無条件 True」(W3 第六陣
    暫定) は撤去。通知はマーカーと同一 tx なので失敗時はどちらも残らず、
    再試行 (claim の failed キー退避) で一度だけ適用される。"""
    ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(
        ledger, "mark_applied", side_effect=RuntimeError("tx boom"),
    ):
        assert wiring._apply_life_end_at_day_close(manager, PERSONA_ID) is False
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")
    assert delivered == []  # マーカーも通知も残らない (単一 tx)
    statuses = sorted(status for _eid, status in _boundary_end_rows(manager))
    assert statuses == ["failed"]

    # 再試行: 回復 → マーカー + 通知が一度だけ
    assert wiring._apply_life_end_at_day_close(manager, PERSONA_ID) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert len(delivered) == 1
    statuses = sorted(status for _eid, status in _boundary_end_rows(manager))
    assert statuses == ["completed", "failed"]


def test_life_end_notice_delivered_once_via_outbox(manager):
    """W5: 配送ハンドラの一時失敗では通知は pending に残り (境界は True で
    決着済み)、次の flush で一度だけ追記される — 「配送失敗 → 再 flush で
    一度だけ」の固定。"""
    fail_once = {"n": 1}
    delivered: List[Dict[str, Any]] = []

    def flaky(item):
        if fail_once["n"] > 0:
            fail_once["n"] -= 1
            raise RuntimeError("delivery down")
        delivered.append(item["payload"]["message"])

    ledger, _ = _attach_boundary_ledger(manager, handler=flaky)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    assert wiring._apply_life_end_at_day_close(manager, PERSONA_ID) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert delivered == []  # 一回目の即時配送は失敗 → pending 保持

    assert ledger.flush_pending_for_persona(PERSONA_ID) is True
    assert len(delivered) == 1
    assert "（活動終了）" in delivered[0]["content"]
    # 追加の flush でも二重追記しない (delivered 済み)
    ledger.flush_pending_for_persona(PERSONA_ID)
    assert len(delivered) == 1


def test_day_close_aborts_judgment_when_life_end_boundary_fails(manager, session_factory):
    """境界失敗は判断の**前**に submitted=False で打ち切る (Codex W3 第五陣 P2
    の再現固定)。マークせず戻るだけでは、判断が成功して schedule と判断台帳が
    completed になり、同一営業日の再試行が来ずに節目 (活動終了通知) が永久に
    欠落していた。判断行は failed (副作用ゼロ) に落ち、backoff 再試行の claim
    がキーを退避して新しい prepared を取れる。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    submissions: List[Dict[str, Any]] = []
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: submissions.append(kwargs),
    )
    ledger = _attach_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    # W5: 台帳経路の境界失敗は冪等段 (TTL 同期等) で注入する — 通知は
    # マーカーと同一 tx の outbox になり、直接 append は縮退経路のみ。
    with patch.object(day_plan, "_sync_cache_ttl_for_life_end", return_value=False):
        result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")

    assert result["submitted"] is False
    assert result["reason"] == "life-end boundary failed"
    assert submissions == []  # 判断は走っていない
    assert ledger.get_execution(result["execution_id"])["status"] == "failed"
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")

    # 再試行 (境界回復後): claim が failed キーを退避し、境界適用 → 判断起動
    # まで進む。スタブ controller は finalize 証跡を作らないため submitted は
    # W1 の証跡ベース判定で False (unknown) になる — ここで固定したいのは
    # 「打ち切りが解け、境界が適用され、判断がメタレーンへ到達する」こと。
    result2 = wiring.fire_judgment_point(manager, PERSONA_ID, "day_close")
    assert len(submissions) == 1  # 判断が起動した
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert result2["execution_id"] != result["execution_id"]  # 新しい claim


# ---------------------------------------------------------------------------
# day_open スキーマ: v0.5 では lives フィールドが無い (LLM 宣言口の巻き戻し)
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


def test_day_open_schema_has_no_lives_field(manager):
    """v0.5: lives は LLM の response_schema から消えている (宣言口の廃止)。"""
    manager.pulse_controller = FakePulseController()
    clock.enable_virtual(datetime(2026, 7, 4, 7, 0, 0))
    result = jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    assert result["submitted"] is True

    args = manager.pulse_controller.submissions[0]["args"]
    schema = args["response_schema"]
    assert "lives" not in schema["properties"]
    assert "lives" not in schema["required"]
    assert set(schema["required"]) == {"monologue", "timetable"}


def test_day_open_situation_text_shows_current_time_without_life(manager):
    """ライフ未確定の日は現在時刻だけを示す (活動時間の案内は出さない)。"""
    manager.pulse_controller = FakePulseController()
    clock.enable_virtual(datetime(2026, 7, 4, 21, 0, 0))
    jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    text = manager.pulse_controller.submissions[0]["args"]["situation_text"]
    assert "現在 21:00" in text
    assert "活動時間" not in text


def test_day_open_situation_text_shows_confirmed_life_window(manager):
    """遅発 day_open 対策 (life.md v0.5 §11.2): 確定済みライフがあれば
    現在時刻と編成範囲を確定情報として明記する。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "23:00", "budget_pulses": 20, "mode": "free"},
    ])
    manager.pulse_controller = FakePulseController()
    clock.enable_virtual(datetime(2026, 7, 4, 21, 0, 0))  # 21 時起動 (遅発)
    jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    text = manager.pulse_controller.submissions[0]["args"]["situation_text"]
    assert "07:00〜23:00" in text
    assert "現在 21:00" in text
    assert "今この時刻から就寝までの範囲で" in text


# ---------------------------------------------------------------------------
# day_open finalize: LLM 出力に紛れ込んだ lives は無視する (巻き戻しの回帰確認)
# ---------------------------------------------------------------------------


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def test_day_open_finalize_ignores_llm_provided_lives(manager, finalize_mod, tmp_path):
    """v0.5: LLM が (不正な口を通じて、または旧クライアントが) "lives" を出力
    に含めても、finalize はそれを一切処理しない — 宣言口は構造ごと無い。"""
    output = {
        "monologue": "……",
        "lives": [
            {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        ],
        "timetable": [_slot("08:30", ref="none", kind="自室で過ごす", facility="own_room",
                             budget_rounds=0)],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )
    assert "applied=True" in summary  # timetable の保存自体は適用される
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


# ---------------------------------------------------------------------------
# ライフ台帳・表示の営業日も予約と同じ解決器から (resolve_business_day)
# ---------------------------------------------------------------------------


def _life_running_past_a_changed_wake(manager, session_factory):
    """確定ライフ 7/4 23:00〜7/5 06:00 の最中に起床設定を 07:00〜22:00 へ変えた状況。

    現行スケジュールで営業日を決めると 7/5 を読み、ライフの真っ最中なのに
    「ライフ未宣言の日」に見える。
    """
    clock.enable_virtual(BASE + timedelta(hours=23, minutes=10))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "23:00", "end": "06:00", "budget_pulses": 20, "mode": "free"},
    ])
    _set_day_schedules(manager, session_factory, wake="07:00", close="22:00")
    clock.enable_virtual(BASE + timedelta(days=1, minutes=30))   # 7/5 00:30


def test_life_status_now_follows_the_running_life_after_wake_change(
    manager, session_factory,
):
    """走行中の確定ライフを「未宣言」と表示しない (話しかけやすさの誤表示)。"""
    _life_running_past_a_changed_wake(manager, session_factory)

    status = day_plan.get_life_status_now(manager, PERSONA_ID)

    assert status["plan_date"] == PLAN_DATE   # 現行スケジュールなら 7/5
    assert status["lives_declared"] is True
    assert status["in_life"] is True
    assert status["life"]["start"] == "23:00"


def test_life_status_now_undeclared_when_lives_are_unreadable(manager):
    """読めない = 判定不能。嘘の「話しかけやすい」を出さない側へ倒す。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))

    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        status = day_plan.get_life_status_now(manager, PERSONA_ID)

    assert status["lives_declared"] is False
    assert status["in_life"] is False
    assert status["plan_date"] is None


def test_judgment_pulse_lands_on_the_running_lifes_day(manager, session_factory):
    """判断点の記帳先も同じ営業日 — 別の日へ積むと台帳が空振りする。"""
    _life_running_past_a_changed_wake(manager, session_factory)

    result = day_plan.record_judgment_pulse(manager, PERSONA_ID)

    assert result is not None
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["judgment_pulses"] == 1


def test_judgment_pulse_is_not_recorded_when_lives_are_unreadable(manager):
    """どの日か分からないまま積まない (他人の帳簿に乗った数字は追えない)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))

    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        assert day_plan.record_judgment_pulse(manager, PERSONA_ID) is None

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["judgment_pulses"] == 0


def test_keepalive_stops_in_the_valley_after_switching_to_an_overnight_rhythm(
    manager, session_factory,
):
    """ライフが終わったら温めるのを止める — 課金の出る経路なので契約を直接見る。

    7/4 のライフは 07:00〜10:00 で確定済み。その後ユーザーが起床設定を跨ぎリズム
    (23:00〜06:00) へ変えると、現行設定だけで営業日を決める旧実装は 7/4 12:00 を
    「7/3 の深夜帯」と読む。7/3 にライフは無いので「未宣言の日」に落ち、終わった
    ライフを温め続けていた。
    """
    clock.enable_virtual(BASE + timedelta(hours=8))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "10:00", "budget_pulses": 4, "mode": "free"},
    ])
    _set_day_schedules(manager, session_factory, wake="23:00", close="06:00")

    clock.enable_virtual(BASE + timedelta(hours=9))    # ライフ中
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is True

    clock.enable_virtual(BASE + timedelta(hours=12))   # ライフ終了後 (谷)
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is False
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["plan_date"] == PLAN_DATE     # 終わったライフの日を指したまま
    assert status["lives_declared"] is True     # 「未宣言」ではない
    assert status["in_life"] is False


def test_keepalive_allows_when_lives_are_unreadable(manager):
    """読めないときは温め続ける側 (延命を止める方に倒さない — docstring の方針)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=12))   # 谷 (通常なら False)
    assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is False

    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        assert day_plan.is_keepalive_allowed(manager, PERSONA_ID) is True


def test_life_pulse_lands_on_the_running_lifes_day(manager, session_factory):
    """暗黙の営業日で積むパルスも同じ解決器を通る (record_judgment_pulse と対)。"""
    _life_running_past_a_changed_wake(manager, session_factory)

    result = day_plan.consume_life_pulse(manager, PERSONA_ID)

    assert result is not None
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 1


def test_life_pulse_is_not_recorded_when_lives_are_unreadable(manager):
    """どの日か分からないまま予算を減らさない。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=30))

    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        assert day_plan.consume_life_pulse(manager, PERSONA_ID) is None

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 0




# ---------------------------------------------------------------------------
# 実行台帳で包む発火の予算精算 (W2 Chunk B, A5) — ライフのある日は
# lives[].used_rounds 正典に一本化し、旧 budget_used_rounds を書かない。
# ---------------------------------------------------------------------------


def _attach_ledger(manager):
    from saiverse import execution_ledger as XL

    manager.execution_ledger = XL.ExecutionLedger(manager.SessionLocal)
    return manager.execution_ledger


def _slot_exec_id(manager):
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        row = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == "slot.fire")
            .first()
        )
        return row.EXECUTION_ID if row is not None else None
    finally:
        db.close()


def test_lives_day_settlement_writes_only_lives_canon(manager, task_ref):
    """A5: ライフのある日は精算で lives[].used_rounds のみ更新し、旧 budget_used_rounds
    (二重台帳) を書かない。予約 5 → 実測 3 の返金精算で used_rounds == 実測 3。"""
    ledger = _attach_ledger(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=5)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_rounds"] == 3           # 予約 5 → 精算 -2 で実測 3
    assert lives[0]["used_pulses"] == 0           # コマ発火は標準パルスを食わない
    # 旧日次台帳 (二重書き) は一切書かれない
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert day_plan.META_BUDGET_USED not in meta
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"


def test_lives_day_settlement_failure_retains_reserved_rounds(manager, task_ref):
    """A5: ライフのある日で精算 tx が転けたら、予約額 (5) が lives.used_rounds に残り、
    実測 (3) への返金は適用されない (後続コマが未消費扱いで超過実行しない安全側)。"""
    ledger = _attach_ledger(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_slot("09:00", budget_rounds=5)])
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_result(rounds_used=3)), \
            patch("saiverse.episodes.close_episode", side_effect=RuntimeError("commit fail")):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "fired"          # done に進まない
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_rounds"] == 5           # 予約額のまま (返金なし)
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "running"


def test_day_open_aborts_judgment_when_life_start_boundary_fails(manager, session_factory):
    """day_open の境界失敗 (活動開始通知の追記失敗等) は判断の**前**に
    submitted=False で打ち切り、started マーカーも書かない (Codex W3 第八陣 —
    day_close の失敗伝播の鏡像)。再試行で境界が一度だけ適用され、判断が
    メタレーンへ到達する。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    submissions: List[Dict[str, Any]] = []
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: submissions.append(kwargs),
    )
    ledger = _attach_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    _set_day_schedules(manager, session_factory)

    # W5: 台帳経路の境界失敗は冪等段 (TTL override) で注入する — 通知は
    # マーカーと同一 tx の outbox になり、直接 append は縮退経路のみ。
    with patch.object(day_plan, "_sync_cache_ttl_for_life_start", return_value=False):
        result = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")

    assert result["submitted"] is False
    assert result["reason"] == "life-start boundary failed"
    assert submissions == []
    assert ledger.get_execution(result["execution_id"])["status"] == "failed"
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives and not lives[0].get("started")  # 確定は済むがマークされない

    # 再試行: 境界回復 → 節目一度だけ → started マーク → 判断起動
    result2 = wiring.fire_judgment_point(manager, PERSONA_ID, "day_open")
    assert len(submissions) == 1
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True
    assert result2["execution_id"] != result["execution_id"]


def test_day_open_boundary_applies_once_via_started_marker(manager, session_factory):
    """境界成功後の再突入 (force 等) でも started マーカーが節目の再適用を防ぐ。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    _set_day_schedules(manager, session_factory)

    with patch.object(day_plan, "_handle_life_start", return_value=True) as spy:
        wiring._confirm_life_at_day_open(manager, PERSONA_ID, {})
        wiring._confirm_life_at_day_open(manager, PERSONA_ID, {})
    assert spy.call_count == 1
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True
