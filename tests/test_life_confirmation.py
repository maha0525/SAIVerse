"""ライフ v0.5「改修A」— ユーザー設定由来のライフ確定のテスト
(docs/intent/life.md v0.5 §3/§4/§6.2/§11.2)。

v0.4 までペルソナ (LLM) が起床判断でライフを宣言していたが、実機初日
(2026-07-13) の破綻を受けて全面改訂した。ライフは**ユーザーが設定する
起床・就寝の区間** (PersonaSchedule) と**予算** (PLAYBOOK_PARAMS の
daily_budget_pulses) からシステムが確定する。宣言まわりの永続化・検証・
台帳・keep-alive 連動そのもの (Phase 2/3) は ``test_life_phase2.py`` /
``test_life_phase3.py`` を参照——本ファイルは v0.5 で新設された確定ロジック
(``day_plan.confirm_life_for_today``) と、その呼び出し経路
(``autonomy_wiring.fire_judgment_point`` の day_open/day_close) のテスト。

対象:

- :func:`saiverse.day_plan.confirm_life_for_today` の単体テスト:
  基本確定・最低予算の切り上げ (均等/自由)・ユーザー設定予算の尊重・
  冪等性・スケジュール未設定時の no-op・深夜跨ぎ窓
- :func:`saiverse.autonomy_wiring.fire_judgment_point` の day_open/day_close
  経路: PersonaSchedule からのライフ確定、ライフ開始/終了処理の呼び出し
  (再発火での二重通知防止込み)、判断点の別枠カウント (used_pulses 不変)
- 遅発 day_open シナリオ: 21 時起動でもライフは窓どおり焼かれ、過去時刻の
  コマは保存時に拒否される
- watchdog の day_open 再発火経路でもライフが確定される

teardown で engine.dispose() + clock.disable_virtual() を必ず行う。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, PersonaSchedule, Playbook, User
from saiverse import autonomy_wiring as wiring
from saiverse import clock
from saiverse import day_plan
from saiverse.event_scheduler import EventScheduler
from saiverse.track_manager import TrackManager

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)


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


class FakeAdapter:
    """SAIMemory adapter の最小スタブ (append_persona_message の記録のみ)。"""

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
        activity_state="Active",
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=FakeAdapter(),
        model=None,
    )
    personas = {PERSONA_ID: persona}
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        event_scheduler=EventScheduler(),  # start() しない (同期検証)
        track_manager=TrackManager(session_factory=session_factory),
        buildings=[],
        pulse_controller=SimpleNamespace(submit_meta_judgment=lambda **kwargs: None),
    )


def _add_day_schedule(session_factory, playbook_name, time_of_day, *, playbook_params=None):
    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK=playbook_name, ENABLED=True, TIME_OF_DAY=time_of_day,
            PLAYBOOK_PARAMS=(
                json.dumps(playbook_params, ensure_ascii=False)
                if playbook_params is not None else None
            ),
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


def _messages(manager):
    return [m["content"] for m in manager.personas[PERSONA_ID].sai_memory.messages]


# ---------------------------------------------------------------------------
# confirm_life_for_today: 単体テスト
# ---------------------------------------------------------------------------


def test_confirm_life_for_today_basic(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モード
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=30,
    )
    assert life["start"] == "07:00"
    assert life["end"] == "22:00"
    assert life["mode"] == day_plan.LIFE_MODE_EVEN
    assert life["budget_pulses"] == 30
    assert life["used_pulses"] == 0
    assert life["judgment_pulses"] == 0
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == [life]


def test_confirm_life_for_today_clamps_to_minimum_even_mode(manager):
    """均等モード: 最低予算 = ceil(窓の長さ ÷ 50分) (life.md §4.2)。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    # 07:00-13:00 = 360 分 → ceil(360/50) = 8
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "13:00",
        requested_budget_pulses=3,  # 最低値未満
    )
    assert life["budget_pulses"] == 8


def test_confirm_life_for_today_no_budget_configured_uses_minimum(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "13:00",
        requested_budget_pulses=None,
    )
    assert life["budget_pulses"] == 8


def test_confirm_life_for_today_free_mode_minimum_is_one(manager):
    manager.personas[PERSONA_ID].model = "gemini-2.5-flash"  # 自由モード
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=None,
    )
    assert life["mode"] == day_plan.LIFE_MODE_FREE
    assert life["budget_pulses"] == 1


def test_confirm_life_for_today_respects_valid_user_budget(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "13:00",
        requested_budget_pulses=50,  # 最低値 (8) より十分大きい
    )
    assert life["budget_pulses"] == 50


def test_confirm_life_for_today_is_idempotent(manager):
    """当日すでに確定済みなら何もしない — 再起動での二重確定・帳簿リセットを防ぐ。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    first = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=30,
    )
    day_plan.consume_life_rounds(manager, PERSONA_ID, PLAN_DATE, 5, at_time="09:00")

    second = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=99,  # 違う予算を渡しても無視される
    )
    assert second["budget_pulses"] == first["budget_pulses"] == 30
    assert second["used_rounds"] == 5  # 消費済み帳簿は保持
    assert len(day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)) == 1


def test_confirm_life_for_today_none_without_close_schedule(manager):
    """就寝スケジュール未設定は「ライフ無し日」(従来動作) — 起床のみでは
    活動区間が定義できない。"""
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", None,
    )
    assert life is None
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_confirm_life_for_today_none_without_wake_schedule(manager):
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, None, "22:00",
    )
    assert life is None


def test_confirm_life_for_today_overnight_window(manager):
    """深夜跨ぎ (close < wake) は正常形 (life.md §4.1)。均等モードの最低予算は
    窓の長さ (跨ぎ込み) から計算する。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "01:00",
        requested_budget_pulses=None,
    )
    assert life["start"] == "07:00"
    assert life["end"] == "01:00"
    # 07:00〜01:00 = 18h = 1080分 → ceil(1080/50) = 22
    assert life["budget_pulses"] == 22
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert day_plan.get_life_for_time(lives, "23:30") == 0
    assert day_plan.get_life_for_time(lives, "03:00") is None


def test_confirm_life_for_today_mode_override_wins_over_provider(manager):
    """life.md v0.5 §5.1: mode_override (ユーザー設定) は provider 自動判定より優先。"""
    manager.personas[PERSONA_ID].model = "gemini-2.5-flash"  # 自由モードのはず
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "13:00",
        requested_budget_pulses=None, mode_override="even",
    )
    assert life["mode"] == day_plan.LIFE_MODE_EVEN
    # 均等モードの最低値 (ceil(360/50)=8) が適用される (自由モードの1でない)
    assert life["budget_pulses"] == 8


def test_confirm_life_for_today_invalid_mode_override_falls_back_to_auto(manager):
    """LIFE_MODES 外の値は無視して自動判定にフォールバックする (書ける口をなくす)。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モードのはず
    life = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=None, mode_override="bogus",
    )
    assert life["mode"] == day_plan.LIFE_MODE_EVEN


# ---------------------------------------------------------------------------
# life_mode_and_min_budget: ライフ設定 API 向けプレビュー計算 (副作用なし)
# ---------------------------------------------------------------------------


def test_life_mode_and_min_budget_even_mode(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    info = day_plan.life_mode_and_min_budget(manager, PERSONA_ID, "07:00", "13:00")
    assert info["derived_mode"] == day_plan.LIFE_MODE_EVEN
    assert info["effective_mode"] == day_plan.LIFE_MODE_EVEN
    assert info["window_minutes"] == 360
    assert info["min_budget_pulses"] == 8  # ceil(360/50)


def test_life_mode_and_min_budget_override(manager):
    manager.personas[PERSONA_ID].model = "gemini-2.5-flash"  # 自由が自動判定
    info = day_plan.life_mode_and_min_budget(
        manager, PERSONA_ID, "07:00", "13:00", mode_override="even",
    )
    assert info["derived_mode"] == day_plan.LIFE_MODE_FREE
    assert info["effective_mode"] == day_plan.LIFE_MODE_EVEN
    assert info["min_budget_pulses"] == 8


def test_life_mode_and_min_budget_no_window_without_wake_or_close(manager):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    info = day_plan.life_mode_and_min_budget(manager, PERSONA_ID, "07:00", None)
    assert info["window_minutes"] is None
    assert info["min_budget_pulses"] is None


# ---------------------------------------------------------------------------
# is_valid_hhmm
# ---------------------------------------------------------------------------


def test_is_valid_hhmm():
    assert day_plan.is_valid_hhmm("07:00") is True
    assert day_plan.is_valid_hhmm("23:59") is True
    assert day_plan.is_valid_hhmm("24:00") is False
    assert day_plan.is_valid_hhmm("7:00") is False
    assert day_plan.is_valid_hhmm(None) is False
    assert day_plan.is_valid_hhmm(123) is False


# ---------------------------------------------------------------------------
# fire_judgment_point (day_open): PersonaSchedule からのライフ確定
# ---------------------------------------------------------------------------


def test_day_open_confirms_life_from_persona_schedule(manager, session_factory):
    """handle_scheduled_judgment 経由 (本番の day_open 発火経路) でライフが
    ユーザー設定 (起床・就寝・予算) から確定する。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    # PLAYBOOK_PARAMS は DB 上の実体として保存しておく (現実に即した形)。
    # handle_scheduled_judgment 自身は呼び出し元 (本番では ScheduleManager) が
    # 解析済みの params を渡す契約なので、ここでも明示的に渡す
    # (watchdog 経路は _find_day_schedules 経由で自己解決するので不要 —
    # test_watchdog_day_open_refire_confirms_life を参照)。
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                       playbook_params={"daily_budget_pulses": 25})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    result = wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open",
        params={"daily_budget_pulses": 25},
    )
    assert result["submitted"] is True

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"
    assert lives[0]["budget_pulses"] == 25  # 07:00-22:00 の最低値 (18) より大きいので素通り
    assert lives[0]["mode"] == day_plan.LIFE_MODE_EVEN
    # ライフ開始の節目処理 (tail 通知) も行われている。TTL override 設定は
    # manager が cache override メソッドを持つ場合のみ (このスタブは持たない
    # — _sync_cache_ttl_for_life_start は getattr で安全に no-op する)。
    assert any("活動開始" in t for t in _messages(manager))


def test_day_open_confirms_life_with_mode_override_end_to_end(manager, session_factory):
    """handle_scheduled_judgment → fire_judgment_point → confirm_life_for_today の
    全経路で life_mode_override (PersonaSchedule.PLAYBOOK_PARAMS 由来) が反映される。"""
    manager.personas[PERSONA_ID].model = "gemini-2.5-flash"  # 自動判定なら自由モード
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                       playbook_params={"life_mode_override": "even"})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    result = wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open",
        params={"life_mode_override": "even"},
    )
    assert result["submitted"] is True

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["mode"] == day_plan.LIFE_MODE_EVEN
    # 均等モードの最低値 (07:00-22:00 = 900分 → ceil(900/50)=18) が適用される
    assert lives[0]["budget_pulses"] == 18


def test_day_open_life_start_fires_only_once_across_refires(manager, session_factory):
    """当日 2 回目以降の day_open 発火では、ライフの再確定はするが (冪等・
    帳簿保持)、ライフ開始の節目処理 (tail 通知) は最初の 1 回だけ行う
    (二重通知の防止)。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open", params={"daily_budget_pulses": 25},
    )
    assert sum("活動開始" in t for t in _messages(manager)) == 1

    # サーバー再起動等で同日中に day_open が再発火 (watchdog 経路相当)
    clock.advance_to(BASE + timedelta(hours=9))
    wiring.handle_scheduled_judgment(
        manager, PERSONA_ID, "judgment_day_open", params={"daily_budget_pulses": 99},
    )
    assert sum("活動開始" in t for t in _messages(manager)) == 1  # 増えない

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["budget_pulses"] == 25  # 帳簿は保持 (99 に焼き直されない)


def test_day_close_ends_life_and_cancels_keepalive(manager, session_factory):
    """day_close 発火経路でライフ終了処理 (keep-alive 予約 cancel・TTL 遅延
    解除の予約・tail 通知) が走る。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    wiring.handle_scheduled_judgment(manager, PERSONA_ID, "judgment_day_open")

    # keep-alive の予約を人工的に立てておく (通常は SessionLifecycle が張る)
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(hours=10), callback=lambda: None,
        key=f"ttl:{PERSONA_ID}",
    )

    clock.advance_to(BASE + timedelta(hours=22))
    result = wiring.handle_scheduled_judgment(manager, PERSONA_ID, "judgment_day_close")
    assert result["submitted"] is True

    assert not manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}")
    assert any("活動終了" in t for t in _messages(manager))


# ---------------------------------------------------------------------------
# 判断点の別枠カウント: used_pulses 不変・judgment_pulses だけ積む
# ---------------------------------------------------------------------------


def test_judgment_pulses_accumulate_separately_from_budget(manager, session_factory):
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                       playbook_params={"daily_budget_pulses": 10})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    wiring.handle_scheduled_judgment(manager, PERSONA_ID, "judgment_day_open")

    clock.advance_to(BASE + timedelta(hours=12))
    wiring.fire_judgment_point(manager, PERSONA_ID, "post_conversation")

    # ライフ窓 (07:00-22:00) の終了は排他的境界 (get_life_for_time は
    # [start, end) 判定) — ちょうど 22:00 だと記帳対象外になるため、
    # 窓の中の時刻で day_close を発火させる。
    clock.advance_to(BASE + timedelta(hours=21, minutes=59))
    wiring.handle_scheduled_judgment(manager, PERSONA_ID, "judgment_day_close")

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    # 判断点の発火 3 回 (day_open・post_conversation・day_close) が
    # fire_judgment_point 共通末尾の record_judgment_pulse でそれぞれ 1 ずつ
    # 記帳される — day_open/day_close もライフ確定・終了処理とは別枠で
    # 判断点として数えられる。
    assert lives[0]["judgment_pulses"] == 3
    assert lives[0]["used_pulses"] == 0  # 実パルスを撃つ実体はまだ無い


# ---------------------------------------------------------------------------
# 遅発 day_open シナリオ (life.md v0.5 §11.2、実機初日の破綻点)
# ---------------------------------------------------------------------------


def test_delayed_day_open_confirms_full_window_and_rejects_past_slots(manager, session_factory):
    """21 時起動の遅発 day_open でも、ライフは設定どおりの窓 (07:00〜22:00) で
    焼かれる。過去時刻 (08:00) のコマは保存時に拒否され、現在時刻以降
    (21:30) のコマは通る——「編成直後の過去コマ即時発火」が構造ごと消える。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                       playbook_params={"daily_budget_pulses": 20})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=21))  # サーバー障害明けの遅発起動
    result = wiring.handle_scheduled_judgment(manager, PERSONA_ID, "judgment_day_open")
    assert result["submitted"] is True

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"  # 起床からの窓そのまま (今からではない)

    with pytest.raises(ValueError, match="現在時刻"):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "08:00", "kind": "休む", "ref": "none", "facility": "own_room",
             "budget_rounds": 0, "title": "", "note": ""},
        ])

    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "21:30", "kind": "休む", "ref": "none", "facility": "own_room",
         "budget_rounds": 0, "title": "", "note": ""},
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["21:30"]


# ---------------------------------------------------------------------------
# watchdog: day_open 再発火経路でもライフが確定される
# ---------------------------------------------------------------------------


def test_watchdog_day_open_refire_confirms_life(manager, session_factory):
    """watchdog の day_open 再発火は PersonaSchedule.PLAYBOOK_PARAMS を
    :func:`_find_day_schedules` 経由で自己解決する (呼び出し元から params を
    渡してもらう必要が無い) — 07:00-22:00 窓の最低予算 (18) より大きい 30 を
    設定し、クランプされず素通りすることを確認する。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                       playbook_params={"daily_budget_pulses": 30})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=9))  # 起床時間帯・day_plan 未編成
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "day_open_refire"
    assert out["result"]["submitted"] is True

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"
    assert lives[0]["budget_pulses"] == 30
