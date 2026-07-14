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
            _slot("13:00", ref="none", kind="休む", facility="own_room",
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
        _slot("08:00", ref="none", kind="休む", facility="own_room",
              budget_rounds=0),
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["21:00"]  # 現在時刻へ丸め
    assert notes == ["（1番目の予定は開始時刻を21:00に調整しました）"]

    # 現在時刻以降のコマはそのまま通る (無調整)
    notes2 = day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        _slot("21:30", ref="none", kind="休む", facility="own_room", budget_rounds=0),
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
        _slot("01:00", ref="none", kind="休む", facility="own_room", budget_rounds=0),
        _slot("01:02", ref="none", kind="休む", facility="own_room", budget_rounds=0),
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
        _slot("08:00", ref="none", kind="休む", facility="own_room", budget_rounds=0),
        _slot("10:00"),
        _slot("13:00", ref="none", kind="休む", facility="own_room", budget_rounds=0),
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
        _slot("23:50", ref="none", kind="休む", facility="own_room", budget_rounds=0),
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
        _slot("07:30", kind="暮らし", ref="none", facility="own_room"),
        _slot("09:00"),
        _slot("13:00"),
        _slot("00:30", kind="休む", ref="none", facility="own_room", budget_rounds=0),
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
        {"start": "00:30", "kind": "休む", "ref": "none", "facility": "own_room",
         "budget_rounds": 0, "title": "眠る"},
        {"start": "07:30", "kind": "暮らし", "ref": "none", "facility": "own_room",
         "budget_rounds": 1, "title": "朝のルーティン"},
        {"start": "13:00", "kind": "知る", "ref": "task:1", "facility": "own_room",
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
    # 発火後、そのライフのラウンド消費が積算される (v0.5: コマ発火自体は
    # 標準パルスを消費しない — used_pulses は不変)
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 0
    assert lives[0]["used_rounds"] == 20


def test_life_budget_gate_allows_through_when_slot_outside_any_life(manager, task_ref):
    """save_lives の谷検証を経ずに直接台帳を触った防御的経路 (通す + WARN)。"""
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
            start=BASE + timedelta(hours=8), end=BASE + timedelta(hours=10),
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
    assert lives[0]["judgment_pulses"] == 1
    assert lives[0]["used_pulses"] == 0


def test_fire_judgment_point_noop_life_bookkeeping_without_lives(manager, session_factory):
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
        "timetable": [_slot("08:30", ref="none", kind="休む", facility="own_room",
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
