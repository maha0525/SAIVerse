"""ライフ v0.5「改修A」— ユーザー設定由来のライフ確定のテスト
(docs/intent/life.md v0.5 §3/§4/§6.2/§11.2)。

v0.4 までペルソナ (LLM) が起床判断でライフを宣言していたが、実機初日
(2026-07-13) の破綻を受けて全面改訂した。ライフは**ユーザーが設定する
起床・就寝の区間** (PersonaSchedule) と**予算** (PLAYBOOK_PARAMS の
daily_budget_pulses) からシステムが確定する。宣言まわりの永続化・検証・
台帳・keep-alive 連動そのもの (Phase 2/3) は ``test_life_phase2.py`` /
``test_life_phase3.py`` を参照——本ファイルは v0.5 で新設された確定ロジック
(``day_plan.confirm_life_for_today``) と、その呼び出し経路のテスト。

2026-10 (autonomous_behavior_v04_plan.md 段 1-2): 呼び出し経路は判断点
(fire_judgment_point の day_open/day_close) から、起床・就寝スケジュールの
機械の帳簿処理 (``day_plan.handle_scheduled_life_boundary``、LLM なし) へ
付け替わり、置き場も persona_day_plan.meta_json から persona_life へ独立した。

対象:

- :func:`saiverse.day_plan.confirm_life_for_today` の単体テスト:
  基本確定・最低予算の切り上げ (均等/自由)・ユーザー設定予算の尊重・
  冪等性・スケジュール未設定時の no-op・深夜跨ぎ窓
- ScheduleManager → 起床・就寝の帳簿処理: PersonaSchedule からのライフ確定、
  開始/終了の節目 (再発火での二重通知防止込み)、Playbook 未取り込みでも
  確定すること、自律 OFF では確定しないこと、判断点の LLM を呼ばないこと
- 判断点の別枠カウント (used_pulses 不変、起床・就寝は数えない)
- 遅発起床シナリオ: 21 時起動でもライフは窓どおり焼かれる
- watchdog: 当日のライフが無いときだけ起床の帳簿処理を撃ち直す
- 置き場の独立: 旧 meta_json.lives の互換読み (読み取り専用)・旧経路で
  節目を済ませた日の二重通知防止・壊れた persona_life の扱い
- 回復 tick: 旧 judgment.day_open / day_close / post_session の席は撃ち直さずに
  期限で閉じる (段 1-4 で判断点ごと退役)

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
from saiverse.execution_ledger import ExecutionLedger
from saiverse.execution_ledger_wiring import (
    TARGET_SAIMEMORY_APPEND,
    _make_saimemory_append_handler,
)
from saiverse.meta_layer import MetaLayer

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
    """SAIMemory adapter の最小スタブ (追記の記録のみ)。

    ライフ境界の通知は台帳の outbox (saimemory.append) を経て
    ``append_ledger_message`` で届く。``append_persona_message`` は本物の adapter
    が持つ口で、境界側はこれの有無で「届け先があるか」を判定する。
    """

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)

    def append_ledger_message(
        self, message, *, execution_id, outbox_id, building_id=None,
        thread_suffix=None,
    ):
        self.messages.append(message)
        return f"msg-{outbox_id}"


class FinalizingPulseController:
    """finalize 相当 (判断行の mark_applied) まで進めるメタレーンのフェイク。

    台帳のある判断点は「finalize が applied を刻んだ」証跡で成功を判定する
    (judgment_points.run_judgment_point)。args の judgment_context に同乗した
    execution_id を finalize と同じように applied へ進める。
    """

    def __init__(self, ledger):
        self._ledger = ledger

    def submit_meta_judgment(self, **kwargs):
        ctx = json.loads((kwargs.get("args") or {}).get("judgment_context") or "{}")
        eid = ctx.get("execution_id")
        if eid:
            self._ledger.mark_applied(eid, result={"finalized": True})


@pytest.fixture
def manager(session_factory):
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

    persona = SimpleNamespace(
        persona_id=PERSONA_ID,
        autonomy_enabled=True,
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=FakeAdapter(),
        model=None,
    )
    personas = {PERSONA_ID: persona}
    # 本番 manager は実行台帳を無条件に持つ。判断点の席とライフ境界の節目は
    # 台帳の下で決着し、境界通知は本番と同じ saimemory.append の実ハンドラで届く。
    ledger = ExecutionLedger(session_factory=session_factory)
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        event_scheduler=EventScheduler(),  # start() しない (同期検証)
        buildings=[],
        pulse_controller=FinalizingPulseController(ledger),
        sea_runtime=None,  # 本番 manager は無条件に持つ (未構築なら None)
        execution_ledger=ledger,
    )
    ledger.register_outbox_handler(
        TARGET_SAIMEMORY_APPEND, _make_saimemory_append_handler(mgr),
    )
    # 判断点の直列化 Lock は本番 manager が無条件に持つ MetaLayer から取る。
    mgr.meta_layer = MetaLayer(mgr)
    return mgr


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
    day_plan.record_judgment_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")

    second = day_plan.confirm_life_for_today(
        manager, PERSONA_ID, PLAN_DATE, "07:00", "22:00",
        requested_budget_pulses=99,  # 違う予算を渡しても無視される
    )
    assert second["budget_pulses"] == first["budget_pulses"] == 30
    assert second["judgment_pulses"] == 1  # 積算済みの帳簿は保持
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
# 起床・就寝スケジュールの発火 → 機械の帳簿処理 (v04 段 1-2)
#
# 本番の入口 ScheduleManager._execute_schedule を通す。起床・就寝の行は判断点
# ではなく day_plan.handle_scheduled_life_boundary (ライフの確定と節目、LLM
# なし) へ回る。
# ---------------------------------------------------------------------------


class RecordingPulseController:
    """判断点のメタレーンへの投入を記録する (起床・就寝では 0 回のはず)。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def submit_meta_judgment(self, **kwargs):
        self.calls.append(kwargs)


def _fire_schedule(manager, playbook_name, time_of_day, params=None):
    """ScheduleManager._execute_schedule でスケジュール行 1 本を発火させる。"""
    from saiverse.schedule_manager import ScheduleManager

    sm = ScheduleManager(saiverse_manager=manager)
    schedule = PersonaSchedule(
        SCHEDULE_ID=1,
        PERSONA_ID=PERSONA_ID,
        SCHEDULE_TYPE="periodic",
        META_PLAYBOOK=playbook_name,
        ENABLED=True,
        TIME_OF_DAY=time_of_day,
        PLAYBOOK_PARAMS=(
            json.dumps(params, ensure_ascii=False) if params is not None else None
        ),
    )
    return sm._execute_schedule(schedule, session=None)


def _boundary_rows(manager, kind):
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        rows = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == kind)
            .all()
        )
        return [(r.IDEMPOTENCY_KEY, r.STATUS) for r in rows]
    finally:
        db.close()


def _judgment_ledger_kinds(manager):
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        rows = (
            db.query(ExecutionLedgerEntry.KIND)
            .filter(ExecutionLedgerEntry.KIND.like("judgment.%"))
            .all()
        )
        return [r[0] for r in rows]
    finally:
        db.close()


def _persona_life_row(manager, plan_date):
    from database.models import PersonaLife

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaLife)
            .filter_by(PERSONA_ID=PERSONA_ID, PLAN_DATE=plan_date)
            .first()
        )
        return None if row is None else json.loads(row.LIVES_JSON)
    finally:
        db.close()


def _write_legacy_meta(manager, plan_date, meta):
    """旧置き場 persona_day_plan.meta_json を直接書く (旧データの再現)。

    時間割の書き手 (update_plan_meta 等) は段 1-4 で撤去したので、互換読みの
    検証用に行を直接作る / 上書きする。
    """
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=PERSONA_ID, plan_date=plan_date)
            .first()
        )
        if row is None:
            db.add(PersonaDayPlan(
                persona_id=PERSONA_ID, plan_date=plan_date, slots_json="[]",
                meta_json=json.dumps(meta, ensure_ascii=False),
                created_at=BASE, updated_at=BASE,
            ))
        else:
            row.meta_json = json.dumps(meta, ensure_ascii=False)
        db.commit()
    finally:
        db.close()


def _plan_meta_json(manager, plan_date):
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=PERSONA_ID, plan_date=plan_date)
            .first()
        )
        return None if row is None else row.meta_json
    finally:
        db.close()


def test_wake_schedule_confirms_life_from_persona_schedule(manager, session_factory):
    """起床スケジュールの発火で、ライフがユーザー設定 (起床・就寝・予算) から
    確定し、開始の節目 (「（活動開始）」通知) が走る。判断点の LLM は呼ばれない。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    manager.pulse_controller = RecordingPulseController()
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                      playbook_params={"daily_budget_pulses": 25})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    outcome = _fire_schedule(
        manager, "judgment_day_open", "07:00", {"daily_budget_pulses": 25},
    )
    assert outcome[0] == "executed"

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"
    assert lives[0]["budget_pulses"] == 25  # 07:00-22:00 の最低値 (18) より大きいので素通り
    assert lives[0]["mode"] == day_plan.LIFE_MODE_EVEN
    assert lives[0]["started"] is True
    # ライフ開始の節目処理 (tail 通知) も行われている。TTL override 設定は
    # manager が cache override メソッドを持つ場合のみ (このスタブは持たない
    # — _sync_cache_ttl_for_life_start は getattr で安全に no-op する)。
    assert any("活動開始" in t for t in _messages(manager))
    # 判断点の LLM は一度も呼ばれず、判断点の席も作られない
    assert manager.pulse_controller.calls == []
    assert _judgment_ledger_kinds(manager) == []


def test_wake_schedule_confirms_life_with_mode_override_end_to_end(manager, session_factory):
    """起床スケジュール → 帳簿処理 → confirm_life_for_today の全経路で
    life_mode_override (PersonaSchedule.PLAYBOOK_PARAMS 由来) が反映される。"""
    manager.personas[PERSONA_ID].model = "gemini-2.5-flash"  # 自動判定なら自由モード
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                      playbook_params={"life_mode_override": "even"})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    outcome = _fire_schedule(
        manager, "judgment_day_open", "07:00", {"life_mode_override": "even"},
    )
    assert outcome[0] == "executed"

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["mode"] == day_plan.LIFE_MODE_EVEN
    # 均等モードの最低値 (07:00-22:00 = 900分 → ceil(900/50)=18) が適用される
    assert lives[0]["budget_pulses"] == 18


def test_life_is_confirmed_and_started_even_with_no_playbooks_imported(
    manager, session_factory,
):
    """Playbook が一つも取り込まれていない世界でも、起床時刻にライフが確定し、
    開始の節目 (通知・台帳) が走る。

    旧経路は判断点の playbook_available 検査を通っていたため、判断点 Playbook
    が未取り込みだとライフの確定ごと黙って飛んでいた (v04 段 1-2 で治った欠陥)。
    """
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    outcome = _fire_schedule(manager, "judgment_day_open", "07:00")

    assert outcome[0] == "executed"
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1 and lives[0]["started"] is True
    assert sum("活動開始" in t for t in _messages(manager)) == 1
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_START) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]


def test_wake_life_start_fires_only_once_across_refires(manager, session_factory):
    """当日 2 回目以降の起床の発火では、ライフの再確定はするが (冪等・帳簿保持)、
    ライフ開始の節目処理 (tail 通知) は最初の 1 回だけ行う (二重通知の防止)。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    _fire_schedule(manager, "judgment_day_open", "07:00", {"daily_budget_pulses": 25})
    assert sum("活動開始" in t for t in _messages(manager)) == 1

    # サーバー再起動等で同日中に起床が再発火 (backoff 再試行・watchdog 相当)
    clock.advance_to(BASE + timedelta(hours=9))
    outcome = _fire_schedule(
        manager, "judgment_day_open", "07:00", {"daily_budget_pulses": 99},
    )
    assert outcome[0] == "executed"
    assert sum("活動開始" in t for t in _messages(manager)) == 1  # 増えない

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["budget_pulses"] == 25  # 帳簿は保持 (99 に焼き直されない)


def test_close_schedule_ends_life_and_cancels_keepalive(manager, session_factory):
    """就寝スケジュールの発火で終了の節目 (keep-alive 予約 cancel・TTL 遅延
    解除の予約・tail 通知) が走る — Playbook 未取り込みでも、LLM なしで。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    manager.pulse_controller = RecordingPulseController()
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    _fire_schedule(manager, "judgment_day_open", "07:00")

    # keep-alive の予約を人工的に立てておく (通常は SessionLifecycle が
    # (persona, model) 単位の key で張る)
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(hours=23), callback=lambda: None,
        key=f"ttl:{PERSONA_ID}:claude-sonnet-5",
    )

    clock.advance_to(BASE + timedelta(hours=22))
    outcome = _fire_schedule(manager, "judgment_day_close", "22:00")
    assert outcome[0] == "executed"

    assert not manager.event_scheduler.has_key(f"ttl:{PERSONA_ID}:claude-sonnet-5")
    assert any("活動終了" in t for t in _messages(manager))
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_END) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]
    assert manager.pulse_controller.calls == []
    assert _judgment_ledger_kinds(manager) == []


def test_autonomy_off_does_not_confirm_life(manager, session_factory):
    """自律 OFF のペルソナでは起床の発火でもライフを確定しない (節目も無い)。
    スケジュールの発火自体は決着扱い (再試行しない)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = False
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    outcome = _fire_schedule(manager, "judgment_day_open", "07:00")

    assert outcome[0] == "executed"
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    assert _persona_life_row(manager, PLAN_DATE) is None
    assert _messages(manager) == []
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_START) == []

    clock.advance_to(BASE + timedelta(hours=22))
    assert _fire_schedule(manager, "judgment_day_close", "22:00")[0] == "executed"
    assert _messages(manager) == []


def test_life_boundary_failure_is_retried_by_the_schedule_backoff(manager, session_factory):
    """節目の失敗は failed に分類され、schedule の backoff 再試行に乗る。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(BASE + timedelta(hours=7))

    from unittest.mock import patch

    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        outcome = _fire_schedule(manager, "judgment_day_open", "07:00")
    assert outcome[0] == "failed"

    outcome = _fire_schedule(manager, "judgment_day_open", "07:00")
    assert outcome[0] == "executed"
    assert sum("活動開始" in t for t in _messages(manager)) == 1


def _schedule_row(session_factory, playbook_name):
    db = session_factory()
    try:
        row = (
            db.query(PersonaSchedule)
            .filter_by(PERSONA_ID=PERSONA_ID, META_PLAYBOOK=playbook_name)
            .one()
        )
        return row.SCHEDULE_ID, row.INSTANCE_TOKEN or "legacy", row.SYNC_GENERATION or 0
    finally:
        db.close()


def test_wake_retry_across_midnight_settles_the_occurrence_day_not_the_next(
    manager, session_factory,
):
    """23:59 の起床が失敗して 00:01 に backoff 再試行されても、営業日は発火予定
    時刻 (occurrence) の日 — 前日のライフを確定・開始し、翌日分は作らない。
    翌日の正規の起床は「開始済み」で省略されず、その日のライフを開始する
    (2026-10-10 Codex 敵対レビュー 2 巡目 修正 2)。

    本番の発火口 ScheduleManager._handle_fire を通す — occurrence トークン
    (closure・台帳 payload が運ぶ発火予定時刻の epoch) から営業日が決まる。
    """
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "23:59")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_open",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    wake_d0 = BASE.replace(hour=23, minute=59)  # 2026-07-04 23:59
    occurrence = str(int(wake_d0.timestamp()))

    # 23:59 の発火: 確定はしたが開始の節目が失敗 → failed + backoff 再試行。
    clock.enable_virtual(wake_d0)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(sid, instance_token, occurrence, generation, 0)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("started") \
        is not True
    assert _messages(manager) == []

    # 00:01 (暦日は翌日) に同じ occurrence の再試行。
    clock.advance_to(wake_d0 + timedelta(minutes=2))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)

    lives_d0 = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives_d0) == 1 and lives_d0[0]["started"] is True
    assert day_plan.get_lives(manager, PERSONA_ID, "2026-07-05") == []
    assert _persona_life_row(manager, "2026-07-05") is None
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_START) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]
    assert sum("活動開始" in t for t in _messages(manager)) == 1

    # 翌日の正規の起床 (2026-07-05 23:59) は省略されず、その日のライフを開始する。
    wake_d1 = wake_d0 + timedelta(days=1)
    clock.advance_to(wake_d1)
    sm._handle_fire(
        sid, instance_token, str(int(wake_d1.timestamp())), generation, 0,
    )
    lives_d1 = day_plan.get_lives(manager, PERSONA_ID, "2026-07-05")
    assert len(lives_d1) == 1 and lives_d1[0]["started"] is True
    assert sorted(_boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_START)) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
        (f"{PERSONA_ID}:2026-07-05", "completed"),
    ]
    assert sum("活動開始" in t for t in _messages(manager)) == 2


def test_close_retry_uses_the_occurrence_business_day(manager, session_factory):
    """就寝も同じ — 22:00 の就寝が失敗し、日付を跨いだ 00:30 の再試行でも、
    営業日は発火予定時刻の日 (跨ぎでないリズムでは 00:30 の暦日でも前日でもなく
    発火予定の 22:00 の日) のライフを終える。"""
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(BASE + timedelta(hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"

    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_d0 = BASE + timedelta(hours=22)
    occurrence = str(int(close_d0.timestamp()))

    clock.advance_to(close_d0)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(sid, instance_token, occurrence, generation, 0)
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")

    # 翌 00:30 の再試行。実行時の時刻から営業日を取ると 07:00〜22:00 の
    # リズムでは 2026-07-05 になり、ライフの無い日として「決着」してしまう。
    clock.advance_to(close_d0 + timedelta(hours=2, minutes=30))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_END) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]


def _dispatch_payloads(manager):
    from database.models import ExecutionLedgerEntry
    from saiverse.schedule_manager import SCHEDULE_DISPATCH_LEDGER_KIND

    db = manager.SessionLocal()
    try:
        rows = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == SCHEDULE_DISPATCH_LEDGER_KIND)
            .order_by(ExecutionLedgerEntry.CREATED_AT.asc())
            .all()
        )
        return [(r.STATUS, json.loads(r.PAYLOAD_JSON or "{}")) for r in rows]
    finally:
        db.close()


def test_close_retry_keeps_the_frozen_business_day_after_the_wake_setting_changes(
    manager, session_factory,
):
    """就寝が失敗し、再試行までの間に起床の予約行だけが変わっても、同じ
    occurrence の再試行は最初に決めた営業日のライフを終える
    (2026-10-10 Codex 敵対レビュー 3 巡目 修正 2)。

    D 日 23:00〜翌 06:00 のライフ。06:00 の就寝の営業日は、その時点の設定
    (深夜跨ぎ) では D。再試行の前に起床を 05:00 へ変える (跨ぎでなくなる) と、
    設定から引き直す実装は D+1 を選び「その日のライフ無し」で成功扱いにして、
    D のライフが終了の節目を永遠に失っていた。営業日は発火の実行台帳の payload
    (``plan_date``) に凍結され、failed 行の退避を跨いで新しい行へ引き継がれる。
    """
    from unittest.mock import patch

    from database.models import PersonaSchedule as _PS
    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "23:00")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    clock.enable_virtual(BASE.replace(hour=23))
    assert _fire_schedule(manager, "judgment_day_open", "23:00")[0] == "executed"
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True

    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_at = BASE + timedelta(days=1, hours=6)  # 2026-07-05 06:00
    occurrence = str(int(close_at.timestamp()))

    clock.advance_to(close_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(sid, instance_token, occurrence, generation, 0)
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)] == [
        ("failed", PLAN_DATE),
    ]

    # 再試行の前に、起床の予約行だけが変わる (就寝の行の世代は変わらない)。
    db = session_factory()
    try:
        db.query(_PS).filter_by(
            PERSONA_ID=PERSONA_ID, META_PLAYBOOK="judgment_day_open",
        ).update({_PS.TIME_OF_DAY: "05:00"})
        db.commit()
    finally:
        db.close()
    # 設定から引き直すと翌日を指す — それを凍結が防いでいることの前提確認。
    assert wiring._day_close_plan_date(manager, PERSONA_ID, close_at) == "2026-07-05"

    clock.advance_to(close_at + timedelta(minutes=2))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_END) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]
    assert sum("活動終了" in t for t in _messages(manager)) == 1
    # 退避された failed 行と、再試行の行の両方が同じ営業日を持つ。
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)] == [
        ("failed", PLAN_DATE), ("completed", PLAN_DATE),
    ]


def _move_wake_to_0500(session_factory):
    from database.models import PersonaSchedule as _PS

    db = session_factory()
    try:
        db.query(_PS).filter_by(
            PERSONA_ID=PERSONA_ID, META_PLAYBOOK="judgment_day_open",
        ).update({_PS.TIME_OF_DAY: "05:00"})
        db.commit()
    finally:
        db.close()


def test_legacy_prepared_close_row_persists_the_frozen_day_before_running(
    manager, session_factory,
):
    """凍結の仕組みが入る前に作られた prepared 行 (payload に plan_date が無い)
    を回収すると、実行の前にその行へ営業日が永続化され、設定変更後の再試行でも
    対象日が変わらない (2026-10-10 Codex 敵対レビュー 4 巡目 修正 1)。

    claim は既存 prepared 行の payload を上書きしないので、計算した値を claim に
    渡すだけでは永続しない — 再試行のたびに現在の設定で引き直していた。
    """
    from unittest.mock import patch

    from saiverse.schedule_manager import (
        SCHEDULE_DISPATCH_LEDGER_KIND,
        ScheduleManager,
        _occurrence_key,
    )

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "23:00")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    clock.enable_virtual(BASE.replace(hour=23))
    assert _fire_schedule(manager, "judgment_day_open", "23:00")[0] == "executed"

    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_close",
    )
    close_at = BASE + timedelta(days=1, hours=6)  # 2026-07-05 06:00
    occurrence = str(int(close_at.timestamp()))
    # 旧予約: claim 後 (prepared) で落ちた、plan_date を持たない行。
    legacy_payload = {
        "schedule_id": sid, "persona_id": PERSONA_ID,
        "schedule_type": "periodic", "instance_token": instance_token,
        "occurrence": occurrence, "generation": generation,
        "meta_playbook": "judgment_day_close", "attempt": 0,
    }
    _eid, runnable, _status = manager.execution_ledger.claim_execution(
        SCHEDULE_DISPATCH_LEDGER_KIND,
        _occurrence_key(sid, instance_token, generation, occurrence),
        persona_id=PERSONA_ID, payload=legacy_payload,
    )
    assert runnable
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)] == [
        ("prepared", None),
    ]

    # 回収の発火: 帳簿処理は失敗するが、実行前に営業日 (いまの設定 = 深夜跨ぎ
    # で D) がその行へ永続化されている。
    sm = ScheduleManager(saiverse_manager=manager)
    clock.advance_to(close_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(sid, instance_token, occurrence, generation, 0)
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)] == [
        ("failed", PLAN_DATE),
    ]

    # 再試行の前に起床だけを変える — 引き直すと翌日を指す。
    _move_wake_to_0500(session_factory)
    assert wiring._day_close_plan_date(manager, PERSONA_ID, close_at) == "2026-07-05"

    clock.advance_to(close_at + timedelta(minutes=2))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_END) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)] == [
        ("failed", PLAN_DATE), ("completed", PLAN_DATE),
    ]


@pytest.mark.parametrize("failure", ["compute", "persist"])
def test_close_without_a_frozen_day_does_not_run_and_is_retried(
    manager, session_factory, failure,
):
    """凍結値が得られない回 (営業日の計算失敗 / 台帳への永続化失敗) は、帳簿
    処理を実行せず、発火を failed にして backoff 再試行へ回す。次の試行で凍結
    できれば実行される (2026-10-10 Codex 敵対レビュー 4 巡目 修正 1 — 以前は
    None のまま実行し、帳簿処理側がその場の設定で計算していた)。"""
    from unittest.mock import MagicMock, patch

    from saiverse.schedule_manager import ScheduleManager, _schedule_key

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(BASE + timedelta(hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"

    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_at = BASE + timedelta(hours=22)
    occurrence = str(int(close_at.timestamp()))
    clock.advance_to(close_at)

    bookkeeping = MagicMock(return_value=True)
    with patch.object(day_plan, "handle_scheduled_life_boundary", bookkeeping):
        if failure == "compute":
            with patch.object(
                day_plan, "life_boundary_plan_date",
                side_effect=RuntimeError("schedules unreadable"),
            ):
                sm._handle_fire(sid, instance_token, occurrence, generation, 0)
        else:
            with patch.object(
                manager.execution_ledger, "fill_missing_payload_fields",
                side_effect=RuntimeError("db locked"),
            ):
                sm._handle_fire(sid, instance_token, occurrence, generation, 0)

    bookkeeping.assert_not_called()
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")
    assert [s for s, _p in _dispatch_payloads(manager)] == ["failed"]
    assert manager.event_scheduler.has_key(_schedule_key(sid))  # backoff 再試行

    # 次の試行 (凍結できる) は実行され、その日のライフを終える。
    clock.advance_to(close_at + timedelta(minutes=2))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert [(s, p.get("plan_date")) for s, p in _dispatch_payloads(manager)][-1] == (
        "completed", PLAN_DATE,
    )


# ---------------------------------------------------------------------------
# 遅れた節目は「現在の世代」のライフにだけ現在状態を触る
# (2026-10-10 Codex 敵対レビュー 3 巡目 修正 3)
# ---------------------------------------------------------------------------

TTL_CLEAR_KEY = f"life_ttl_clear:{PERSONA_ID}"
LIFE_SET_OVERRIDE = {"enabled": True, "ttl": "1h"}


def _install_cache_override(manager) -> Dict[str, Any]:
    """manager に cache override の口 (本番 SAIVerseManager と同じ名前) を生やす。"""
    overrides: Dict[str, Any] = {}

    def _set(persona_id, *, enabled, ttl):
        overrides[persona_id] = {"enabled": enabled, "ttl": ttl}

    manager.get_persona_cache_override = lambda pid: overrides.get(pid)
    manager.set_persona_cache_override = _set
    manager.clear_persona_cache_override = lambda pid: overrides.pop(pid, None)
    return overrides


def _boundary_result(manager, kind, plan_date):
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        row = (
            db.query(ExecutionLedgerEntry)
            .filter(
                ExecutionLedgerEntry.KIND == kind,
                ExecutionLedgerEntry.IDEMPOTENCY_KEY == f"{PERSONA_ID}:{plan_date}",
            )
            .one()
        )
        return row.STATUS, json.loads(row.RESULT_JSON or "{}")
    finally:
        db.close()


def test_wake_retry_after_the_close_is_not_treated_as_a_start(
    manager, session_factory,
):
    """23:59 の起床の節目が失敗 → 00:00 の就寝は成功 → 00:01 の起床の再試行は、
    もう終わったライフを「開始」しない: 開始通知なし (「（活動終了）→（活動開始）」の
    逆順にならない)・就寝が予約した TTL 解除を取り消さない・1h を設定しない・
    started マーカーを書かない。台帳は見送りとして決着し、再試行は止まる。"""
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モード
    overrides = _install_cache_override(manager)
    _add_day_schedule(session_factory, "judgment_day_open", "23:59")
    _add_day_schedule(session_factory, "judgment_day_close", "00:00")
    wake_sid, wake_tok, wake_gen = _schedule_row(session_factory, "judgment_day_open")
    close_sid, close_tok, close_gen = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    wake_at = BASE.replace(hour=23, minute=59)  # 2026-07-04 23:59
    wake_occ = str(int(wake_at.timestamp()))
    close_at = BASE + timedelta(days=1)  # 2026-07-05 00:00

    # 23:59: ライフは確定したが、開始の節目が失敗。
    clock.enable_virtual(wake_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(wake_sid, wake_tok, wake_occ, wake_gen, 0)
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1 and not lives[0].get("started")

    # 00:00: 就寝は成功 (このライフは現在の世代 — 通常どおり終える)。
    clock.advance_to(close_at)
    sm._handle_fire(
        close_sid, close_tok, str(int(close_at.timestamp())), close_gen, 0,
    )
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    assert sum("活動終了" in t for t in _messages(manager)) == 1

    # 00:01: 起床の再試行 (同じ occurrence)。
    clock.advance_to(close_at + timedelta(minutes=1))
    sm._handle_fire(wake_sid, wake_tok, wake_occ, wake_gen, 1)

    life = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]
    assert not life.get("started")
    assert life["ended"] is True
    assert not any("活動開始" in t for t in _messages(manager))
    assert sum("活動終了" in t for t in _messages(manager)) == 1
    assert manager.event_scheduler.has_key(TTL_CLEAR_KEY)  # 取り消されない
    assert overrides.get(PERSONA_ID) is None  # 1h が残留しない
    status, result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_START, PLAN_DATE,
    )
    assert status == "completed"
    assert result.get("stale") == "ended"
    # 発火は決着した (failed → completed)。もう一度撃っても何もしない。
    assert [s for s, _p in _dispatch_payloads(manager)][-1] == "completed"
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START, plan_date=PLAN_DATE,
    ) is True
    assert not any("活動開始" in t for t in _messages(manager))


def test_recovered_close_of_the_previous_day_does_not_touch_the_running_life(
    manager, session_factory,
):
    """翌日のライフが開始した後に前日の就寝を回収しても、現在のキャッシュ維持
    (keep-alive の予約) と TTL (1h の設定・解除の予約) に触らず、終了通知も
    出さない。前日のライフの ended マーカーと台帳は決着する。"""
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モード
    overrides = _install_cache_override(manager)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    next_date = "2026-07-05"

    clock.enable_virtual(BASE + timedelta(hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"
    assert overrides.get(PERSONA_ID) == LIFE_SET_OVERRIDE

    # D 22:00 の就寝が失敗 (再試行は翌日の起床の後まで届かなかった)。
    close_sid, close_tok, close_gen = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_at = BASE + timedelta(hours=22)
    close_occ = str(int(close_at.timestamp()))
    clock.advance_to(close_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 0)

    # D+1 07:00 の起床: 新しい世代のライフが確定・開始する。
    clock.advance_to(BASE + timedelta(days=1, hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"
    assert day_plan.get_lives(manager, PERSONA_ID, next_date)[0]["started"] is True
    keepalive_key = f"ttl:{PERSONA_ID}:claude-sonnet-5"
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(days=1, hours=8), callback=lambda: None,
        key=keepalive_key,
    )

    # 前日の就寝を回収 (同じ occurrence の再試行)。
    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=5))
    sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 1)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert not day_plan.get_lives(manager, PERSONA_ID, next_date)[0].get("ended")
    assert manager.event_scheduler.has_key(keepalive_key)  # 稼働中を温め続ける
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)  # 解除を予約しない
    assert overrides.get(PERSONA_ID) == LIFE_SET_OVERRIDE
    assert not any("活動終了" in t for t in _messages(manager))
    assert sum("活動開始" in t for t in _messages(manager)) == 2
    status, result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_END, PLAN_DATE,
    )
    assert status == "completed"
    assert result.get("stale") == f"superseded_by:{next_date}"
    assert [s for s, _p in _dispatch_payloads(manager)][-1] == "completed"


def test_recovered_close_sees_the_new_day_kept_only_in_the_legacy_place(
    manager, session_factory,
):
    """当日のライフが旧置き場 (persona_day_plan.meta_json.lives) にだけある
    (開始済み) 状態で前日の就寝を回収しても、世代の検査が当日を「新しい日」と
    見る — キャッシュ維持・TTL・終了通知に触らず、前日の ended だけ付く
    (2026-10-10 Codex 敵対レビュー 4 巡目 修正 2 — 以前は persona_life だけを
    見て「新しい日は無い」と誤判定していた)。"""
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モード
    overrides = _install_cache_override(manager)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    next_date = "2026-07-05"

    clock.enable_virtual(BASE + timedelta(hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"
    assert overrides.get(PERSONA_ID) == LIFE_SET_OVERRIDE

    # D 22:00 の就寝が失敗。
    close_sid, close_tok, close_gen = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_at = BASE + timedelta(hours=22)
    close_occ = str(int(close_at.timestamp()))
    clock.advance_to(close_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 0)

    # D+1 のライフは旧置き場にだけある (persona_life に行が無い)。
    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=5))
    _write_legacy_meta(manager, next_date, {day_plan.META_LIVES: [{
        "start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even",
        "used_pulses": 0, "judgment_pulses": 0, "started": True,
    }]})
    assert _persona_life_row(manager, next_date) is None
    assert day_plan.get_lives(manager, PERSONA_ID, next_date)[0]["started"] is True
    keepalive_key = f"ttl:{PERSONA_ID}:claude-sonnet-5"
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(days=1, hours=8), callback=lambda: None,
        key=keepalive_key,
    )

    # 前日の就寝を回収 (同じ occurrence の再試行)。
    sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 1)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert manager.event_scheduler.has_key(keepalive_key)
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    assert overrides.get(PERSONA_ID) == LIFE_SET_OVERRIDE
    assert not any("活動終了" in t for t in _messages(manager))
    status, result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_END, PLAN_DATE,
    )
    assert status == "completed"
    assert result.get("stale") == f"superseded_by:{next_date}"
    # 旧置き場へは書き戻さず、persona_life にも D+1 の行を作らない。
    assert _persona_life_row(manager, next_date) is None


def test_newer_life_lookup_prefers_persona_life_over_the_legacy_place(manager):
    """新しい営業日の検索は get_lives と同じ優先順位: persona_life に行のある
    日付はその内容が正 (空でも旧置き場を見ない)、行の無い日付だけ旧置き場。"""
    legacy_life = {"start": "07:00", "end": "22:00", "budget_pulses": 18,
                   "mode": "even", "started": True}
    # 07-05: 旧置き場にライフがあるが、persona_life の行 (空) が正 → 数えない。
    _write_legacy_meta(manager, "2026-07-05", {day_plan.META_LIVES: [legacy_life]})
    from database.models import PersonaLife

    db = manager.SessionLocal()
    try:
        db.add(PersonaLife(
            PERSONA_ID=PERSONA_ID, PLAN_DATE="2026-07-05", LIVES_JSON="[]",
            CREATED_AT=BASE, UPDATED_AT=BASE,
        ))
        db.commit()
    finally:
        db.close()
    assert day_plan._newer_confirmed_life_date(manager, PERSONA_ID, PLAN_DATE) is None

    # 07-06: 旧置き場にだけある → 数える。
    _write_legacy_meta(manager, "2026-07-06", {day_plan.META_LIVES: [legacy_life]})
    assert day_plan._newer_confirmed_life_date(
        manager, PERSONA_ID, PLAN_DATE,
    ) == "2026-07-06"


# ---------------------------------------------------------------------------
# 設定の読み取り失敗を「未設定」と混ぜない
# (2026-10-10 Codex 敵対レビュー 4 巡目 修正 3)
# ---------------------------------------------------------------------------


def _schedules_read_fails(real):
    """strict の読みだけを失敗させる (非 strict は従来の縮退 = 空の答え)。

    本物の DB 例外では非 strict は WARNING を出して空の答えへ縮退する —
    それを「未設定」と読むのが直す対象の欠陥。
    """

    def _fake(manager, persona_id, *, strict=False):
        if strict:
            raise RuntimeError("database is locked")
        return {"wake": None, "close": None, "wake_days": None,
                "day_open_params": None}

    return _fake


def test_wake_with_unreadable_schedules_is_not_settled_and_is_retried(
    manager, session_factory,
):
    """起床の帳簿処理中に設定の読み取りが一時失敗しても、「ライフ無しの日」で
    成功扱いにせず、失敗として backoff 再試行へ回す。復旧した再試行でライフが
    確定・開始する。"""
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager, _schedule_key

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    sid, instance_token, generation = _schedule_row(
        session_factory, "judgment_day_open",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    wake_at = BASE + timedelta(hours=7)
    occurrence = str(int(wake_at.timestamp()))

    clock.enable_virtual(wake_at)
    with patch.object(
        wiring, "_find_day_schedules",
        new=_schedules_read_fails(wiring._find_day_schedules),
    ):
        sm._handle_fire(sid, instance_token, occurrence, generation, 0)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    assert _persona_life_row(manager, PLAN_DATE) is None
    assert [s for s, _p in _dispatch_payloads(manager)] == ["failed"]
    assert manager.event_scheduler.has_key(_schedule_key(sid))  # backoff 再試行

    clock.advance_to(wake_at + timedelta(minutes=2))
    sm._handle_fire(sid, instance_token, occurrence, generation, 1)
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1 and lives[0]["started"] is True
    assert [s for s, _p in _dispatch_payloads(manager)][-1] == "completed"


def test_wake_with_truly_unset_schedules_still_settles_as_no_life(manager):
    """正常に読めて空 (本当に未設定) なら、従来どおり「ライフ無しの日」で決着。"""
    clock.enable_virtual(BASE + timedelta(hours=7))
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START,
        occurrence_at=BASE + timedelta(hours=7),
    ) is True
    assert _persona_life_row(manager, PLAN_DATE) is None


def test_business_day_resolver_returns_none_when_schedules_are_unreadable(
    manager, session_factory,
):
    """解決器は設定の読み取り失敗で None (決められない) を返し、スルースの
    手入れの提示は見送られる。以前は空設定へ縮退して暦日を返し、深夜跨ぎ
    リズムの深夜帯に翌日を「業務日」として渡していた。"""
    from unittest.mock import patch

    from sea import sluice

    _add_day_schedule(session_factory, "judgment_day_open", "23:00")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    # 07-05 00:30 — 跨ぎリズムの深夜帯 (営業日は 07-04)。ライフはまだ無い。
    clock.enable_virtual(BASE + timedelta(days=1, minutes=30))
    basis = day_plan.resolve_business_day(manager, PERSONA_ID)
    assert basis is not None and basis.plan_date == PLAN_DATE

    with patch.object(
        wiring, "_find_day_schedules",
        new=_schedules_read_fails(wiring._find_day_schedules),
    ):
        assert day_plan.resolve_business_day(manager, PERSONA_ID) is None
        lifecycle = SimpleNamespace(manager=manager)
        assert sluice._curation_business_day(lifecycle, PERSONA_ID) is None


def test_business_day_resolver_with_truly_unset_schedules_is_the_calendar_day(
    manager,
):
    """正常に読めて空 (本当に未設定) なら None ではなく暦日の ``schedule``。"""
    clock.enable_virtual(BASE + timedelta(days=1, minutes=30))
    basis = day_plan.resolve_business_day(manager, PERSONA_ID)
    assert basis is not None
    assert (basis.plan_date, basis.source) == ("2026-07-05", "schedule")


def test_life_start_after_the_window_is_settled_without_starting(manager):
    """窓 (終了時刻) が過ぎてから届いた開始の節目は「開始」しない — started
    マーカーも通知も TTL 同期も無く、台帳だけ見送りで決着する。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    overrides = _install_cache_override(manager)
    clock.enable_virtual(BASE + timedelta(hours=7))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even"},
    ])
    life = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]

    clock.advance_to(BASE + timedelta(hours=23))
    assert day_plan.apply_life_boundary(
        manager, PERSONA_ID, PLAN_DATE, life, boundary=day_plan.LIFE_BOUNDARY_START,
    ) is True

    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("started")
    assert _messages(manager) == []
    assert overrides.get(PERSONA_ID) is None
    status, result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_START, PLAN_DATE,
    )
    assert (status, result.get("stale")) == ("completed", "window_passed")


# ---------------------------------------------------------------------------
# 判断点の別枠カウント: used_pulses 不変・judgment_pulses だけ積む
# ---------------------------------------------------------------------------


def test_judgment_pulses_accumulate_separately_from_budget(manager, session_factory):
    """判断点の発火は judgment_pulses だけを積む (used_pulses は不変)。

    起床・就寝は v04 段 1-2 で判断点でなくなった — ライフ確定・終了の帳簿処理は
    判断点として数えない。数えるのは on_event 等の本物の判断点だけ。
    """
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _import_judgment_playbooks(session_factory)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                      playbook_params={"daily_budget_pulses": 10})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=7))
    _fire_schedule(manager, "judgment_day_open", "07:00")

    clock.advance_to(BASE + timedelta(hours=12))
    wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "掲示板の告知"},
    )

    clock.advance_to(BASE + timedelta(hours=21, minutes=59))
    _fire_schedule(manager, "judgment_day_close", "22:00")

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["judgment_pulses"] == 1  # on_event の 1 回だけ
    assert lives[0]["used_pulses"] == 0  # 実パルスを撃つ実体はまだ無い


# ---------------------------------------------------------------------------
# 遅発起床シナリオ (life.md v0.5 §11.2、実機初日の破綻点)
# ---------------------------------------------------------------------------


def test_delayed_wake_confirms_full_window(manager, session_factory):
    """21 時起動の遅発でも、ライフは設定どおりの窓 (07:00〜22:00) で焼かれる
    (今からの窓ではない)。旧版はこの後の時間割の過去コマの丸めも検査して
    いたが、時間割は段 1-4 で撤去した。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                      playbook_params={"daily_budget_pulses": 20})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=21))  # サーバー障害明けの遅発起動
    outcome = _fire_schedule(
        manager, "judgment_day_open", "07:00", {"daily_budget_pulses": 20},
    )
    assert outcome[0] == "executed"

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"  # 起床からの窓そのまま (今からではない)


# ---------------------------------------------------------------------------
# watchdog: 当日のライフが無いときだけ起床の帳簿処理を撃ち直す
# ---------------------------------------------------------------------------


def test_watchdog_refires_life_start_only_while_today_has_no_life(manager, session_factory):
    """watchdog は「当日のライフ無し」で起床の帳簿処理を撃ち直し、
    PersonaSchedule.PLAYBOOK_PARAMS を :func:`_find_day_schedules` 経由で自己
    解決する (07:00-22:00 窓の最低予算 18 より大きい 30 が素通り)。確定済みの
    次の tick では撃たない。判断点の LLM は一度も呼ばれない。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    manager.pulse_controller = RecordingPulseController()
    _add_day_schedule(session_factory, "judgment_day_open", "07:00",
                      playbook_params={"daily_budget_pulses": 30})
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")

    clock.enable_virtual(BASE + timedelta(hours=9))  # 起床時間帯・ライフ未確定
    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out == {"action": "life_start_refire", "settled": True}

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1
    assert lives[0]["start"] == "07:00"
    assert lives[0]["end"] == "22:00"
    assert lives[0]["budget_pulses"] == 30
    assert lives[0]["started"] is True

    clock.advance_to(BASE + timedelta(hours=10))
    out2 = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out2["action"] == "none"  # 確定済み — 撃ち直さない
    assert sum("活動開始" in t for t in _messages(manager)) == 1
    assert manager.pulse_controller.calls == []
    assert _judgment_ledger_kinds(manager) == []


def test_watchdog_skips_life_start_when_autonomy_off(manager, session_factory):
    """自律 OFF では watchdog も起床の帳簿処理を撃たない (ライフは無いまま)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = False
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    clock.enable_virtual(BASE + timedelta(hours=9))

    out = wiring.watchdog_tick(manager, PERSONA_ID)
    assert out["action"] == "skip"
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


# ---------------------------------------------------------------------------
# 置き場の独立: persona_life と旧 meta_json.lives の互換読み
# ---------------------------------------------------------------------------


def test_get_lives_falls_back_to_legacy_meta_only_when_no_persona_life_row(manager):
    """persona_life に行が無い日付だけ、旧 meta_json.lives を読み取り専用で読む。
    読みは旧置き場を書き換えない。persona_life に行がある日付では旧置き場を
    見ない (空配列の行でも)。"""
    legacy = [{"start": "23:00", "end": "06:00", "budget_pulses": 5, "mode": "free",
               "used_pulses": 0, "used_rounds": 0, "judgment_pulses": 0,
               "started": True}]
    _write_legacy_meta(manager, PLAN_DATE, {
        day_plan.META_LIVES: legacy, "tomorrow_memo": "残す",
    })
    before = _plan_meta_json(manager, PLAN_DATE)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == legacy
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True) == legacy
    assert _persona_life_row(manager, PLAN_DATE) is None  # 読みは何も書かない
    assert _plan_meta_json(manager, PLAN_DATE) == before

    # 別の日付に persona_life の行があれば、その日付は旧置き場を見ない
    other = "2026-07-05"
    _write_legacy_meta(manager, other, {day_plan.META_LIVES: legacy})
    day_plan.save_lives(manager, PERSONA_ID, other, [])
    assert _persona_life_row(manager, other) == []
    assert day_plan.get_lives(manager, PERSONA_ID, other) == []


def test_write_on_a_legacy_only_day_seeds_persona_life_and_leaves_meta_untouched(manager):
    """互換読みの日付に書き手 (記帳・マーカー) が来たら、旧ライフを種にして
    persona_life に新しい行を作る。旧置き場 meta_json へは書き戻さない。"""
    legacy = [{"start": "07:00", "end": "22:00", "budget_pulses": 5, "mode": "free",
               "used_pulses": 0, "used_rounds": 2, "judgment_pulses": 0,
               "started": True}]
    _write_legacy_meta(manager, PLAN_DATE, {day_plan.META_LIVES: legacy})
    before = _plan_meta_json(manager, PLAN_DATE)

    day_plan.record_judgment_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="10:00")

    row = _persona_life_row(manager, PLAN_DATE)
    assert row is not None and len(row) == 1
    assert row[0]["judgment_pulses"] == 1
    assert row[0]["used_rounds"] == 2      # 旧帳簿を引き継ぐ
    assert row[0]["started"] is True       # 旧マーカーも引き継ぐ
    assert _plan_meta_json(manager, PLAN_DATE) == before  # 旧置き場は無傷


def test_legacy_started_day_does_not_double_notify_via_ledger_key(manager, session_factory):
    """切り替え前の旧経路で開始の節目を済ませた日 (旧置き場にライフ・実行台帳
    life.boundary_start の冪等キーが completed) に、新経路の起床が来ても
    「（活動開始）」を二重に出さない。

    マーカー (started) が旧置き場から読めない最悪の形でも、台帳の冪等キー
    ``{persona}:{plan_date}`` が二重適用を止める。"""
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    # 旧経路が残した状態: 旧置き場のライフ (マーカー欠落) + 台帳の済み行
    _write_legacy_meta(manager, PLAN_DATE, {day_plan.META_LIVES: [
        {"start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even",
         "used_pulses": 0, "used_rounds": 0, "judgment_pulses": 0},
    ]})
    ledger = manager.execution_ledger
    eid, runnable, _ = ledger.claim_execution(
        day_plan.LIFE_BOUNDARY_KIND_START,
        idempotency_key=f"{PERSONA_ID}:{PLAN_DATE}", persona_id=PERSONA_ID,
    )
    assert runnable and ledger.try_mark_running(eid)
    ledger.mark_applied(eid, result={"boundary": "start", "legacy": True})
    ledger.mark_completed(eid)

    clock.enable_virtual(BASE + timedelta(hours=8))
    outcome = _fire_schedule(manager, "judgment_day_open", "07:00")

    assert outcome[0] == "executed"
    assert _messages(manager) == []  # 二重の「（活動開始）」は出ない
    assert _boundary_rows(manager, day_plan.LIFE_BOUNDARY_KIND_START) == [
        (f"{PERSONA_ID}:{PLAN_DATE}", "completed"),
    ]
    # 旧置き場のライフがそのまま今日のライフとして読まれる (確定し直さない)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["budget_pulses"] == 18


def test_legacy_overnight_life_still_resolves_the_business_day_after_switch(manager, session_factory):
    """切り替えた当日の深夜、前日の跨ぎライフ (旧置き場) が営業日を決め続ける。"""
    _add_day_schedule(session_factory, "judgment_day_open", "23:00")
    _add_day_schedule(session_factory, "judgment_day_close", "06:00")
    _write_legacy_meta(manager, "2026-07-03", {day_plan.META_LIVES: [
        {"start": "23:00", "end": "06:00", "budget_pulses": 5, "mode": "free",
         "used_pulses": 0, "used_rounds": 0, "judgment_pulses": 0, "started": True},
    ]})
    clock.enable_virtual(BASE + timedelta(hours=2))  # 07-04 02:00

    basis = day_plan.resolve_business_day(manager, PERSONA_ID)
    assert basis.plan_date == "2026-07-03"
    assert basis.source == "life"
    assert day_plan.get_life_status_now(manager, PERSONA_ID)["in_life"] is True

    # 就寝 (06:00) の節目は前日の営業日に付き、旧ライフを種に persona_life へ
    clock.advance_to(BASE + timedelta(hours=6))
    assert _fire_schedule(manager, "judgment_day_close", "06:00")[0] == "executed"
    assert _persona_life_row(manager, "2026-07-03")[0]["ended"] is True
    assert any("活動終了" in t for t in _messages(manager))


def test_corrupt_persona_life_is_unreadable_not_missing(manager):
    """persona_life の LIVES_JSON が壊れていたら、寛容な読み口は空へ縮退し、
    営業日の選択 (strict) は「読めない」として止まる — 旧置き場へ落ちない。"""
    from database.models import PersonaLife

    _write_legacy_meta(manager, PLAN_DATE, {day_plan.META_LIVES: [
        {"start": "07:00", "end": "22:00", "budget_pulses": 5, "mode": "free"},
    ]})
    db = manager.SessionLocal()
    try:
        db.add(PersonaLife(
            PERSONA_ID=PERSONA_ID, PLAN_DATE=PLAN_DATE,
            LIVES_JSON='[{"start": "07:00"', CREATED_AT=BASE, UPDATED_AT=BASE,
        ))
        db.commit()
    finally:
        db.close()
    clock.enable_virtual(BASE + timedelta(hours=12))

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    with pytest.raises(ValueError):
        day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True)
    assert day_plan.resolve_business_day(manager, PERSONA_ID) is None


# ---------------------------------------------------------------------------
# 破損したライフを「無い」と同じに扱わない
# (2026-10-10 Codex 敵対レビュー 5 巡目)
# ---------------------------------------------------------------------------

CORRUPT_LIVES_JSON = '[{"start": "07:00"'
CORRUPT_META_JSON = '{"lives": [{"start": "07:00"'


def _corrupt_life(**overrides):
    life = {
        "start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even",
        "used_pulses": 0, "judgment_pulses": 0,
    }
    life.update(overrides)
    return [life]


def _corrupt_pair(lives):
    """同じ壊れ方を persona_life の LIVES_JSON と旧 meta_json の両方の形で作る。"""
    return json.dumps(lives), json.dumps({day_plan.META_LIVES: lives})


#: 壊れ方 → (persona_life.LIVES_JSON の生文字列, 旧 meta_json の生文字列)。
#: 構文の破損 (5 巡目) に加え、構文は正しいのに中身が読み手を誤動作させる形
#: (2026-10-10 Codex 敵対レビュー 6 巡目): 記録があるのに空文字列・null /
#: 境界マーカーが bool でない ("false" は truthy で「処理済み」に読まれる) /
#: 時刻が "HH:MM" でない。
CORRUPTIONS = {
    "invalid_json": (CORRUPT_LIVES_JSON, CORRUPT_META_JSON),
    "null": ("null", '{"lives": null}'),
    "empty_string": ("", '{"lives": ""}'),
    "marker_not_bool": _corrupt_pair(
        _corrupt_life(started="false", ended="false"),
    ),
    "bad_time": _corrupt_pair(_corrupt_life(start="25:00")),
}


def _life_raw(manager, plan_date):
    """persona_life の LIVES_JSON を生のまま返す (行が無ければ None)。"""
    from database.models import PersonaLife

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaLife)
            .filter_by(PERSONA_ID=PERSONA_ID, PLAN_DATE=plan_date)
            .first()
        )
        return None if row is None else row.LIVES_JSON
    finally:
        db.close()


def _set_life_raw(manager, plan_date, raw):
    """persona_life の LIVES_JSON を生の文字列で上書き / 作成する (破損の再現)。"""
    from database.models import PersonaLife

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaLife)
            .filter_by(PERSONA_ID=PERSONA_ID, PLAN_DATE=plan_date)
            .first()
        )
        if row is None:
            db.add(PersonaLife(
                PERSONA_ID=PERSONA_ID, PLAN_DATE=plan_date, LIVES_JSON=raw,
                CREATED_AT=BASE, UPDATED_AT=BASE,
            ))
        else:
            row.LIVES_JSON = raw
        db.commit()
    finally:
        db.close()


def _set_legacy_meta_raw(manager, plan_date, raw):
    """旧置き場 persona_day_plan.meta_json を生の文字列で書く (破損の再現)。"""
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=PERSONA_ID, plan_date=plan_date)
            .first()
        )
        if row is None:
            db.add(PersonaDayPlan(
                persona_id=PERSONA_ID, plan_date=plan_date, slots_json="[]",
                meta_json=raw, created_at=BASE, updated_at=BASE,
            ))
        else:
            row.meta_json = raw
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("place", ["persona_life", "legacy"])
def test_recovered_close_with_a_corrupt_newer_life_changes_nothing_and_retries(
    manager, session_factory, place, corruption,
):
    """新しい日のライフの記録が壊れているとき、前日の就寝の回収は「新しい日は
    無い」と読まない — 現在のキャッシュ維持・TTL・終了通知・前日の記録・壊れた
    記録のどれも変えずに失敗し、記録を直した後の再試行で正しく世代検査される
    (新しい日が確定済み = 古い終了として帳簿だけ決着)。"""
    bad_life_raw, bad_meta_raw = CORRUPTIONS[corruption]
    from unittest.mock import patch

    from saiverse.schedule_manager import ScheduleManager, _schedule_key

    manager.personas[PERSONA_ID].model = "claude-sonnet-5"  # 均等モード
    overrides = _install_cache_override(manager)
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    next_date = "2026-07-05"

    clock.enable_virtual(BASE + timedelta(hours=7))
    assert _fire_schedule(manager, "judgment_day_open", "07:00")[0] == "executed"

    # D 22:00 の就寝が失敗 (再試行は翌日の起床の後まで届かなかった)。
    close_sid, close_tok, close_gen = _schedule_row(
        session_factory, "judgment_day_close",
    )
    sm = ScheduleManager(saiverse_manager=manager)
    close_at = BASE + timedelta(hours=22)
    close_occ = str(int(close_at.timestamp()))
    clock.advance_to(close_at)
    with patch.object(day_plan, "apply_life_boundary", return_value=False):
        sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 0)

    if place == "persona_life":
        # D+1 のライフが確定・開始した後、その記録が両置き場で壊れる。
        clock.advance_to(BASE + timedelta(days=1, hours=7))
        assert _fire_schedule(
            manager, "judgment_day_open", "07:00",
        )[0] == "executed"
        good_raw = _life_raw(manager, next_date)
        assert json.loads(good_raw)[0]["started"] is True
        _set_life_raw(manager, next_date, bad_life_raw)
        _set_legacy_meta_raw(manager, next_date, bad_meta_raw)
    else:
        # D+1 のライフは旧置き場にだけあり (開始済み)、その meta_json が壊れる。
        _set_legacy_meta_raw(manager, next_date, bad_meta_raw)
        assert _life_raw(manager, next_date) is None
    keepalive_key = f"ttl:{PERSONA_ID}:claude-sonnet-5"
    manager.event_scheduler.schedule(
        fire_at=BASE + timedelta(days=1, hours=8), callback=lambda: None,
        key=keepalive_key,
    )
    overrides_before = dict(overrides)
    messages_before = list(_messages(manager))
    d_raw_before = _life_raw(manager, PLAN_DATE)

    # 前日の就寝を回収 — 新しい日の世代が読めないので失敗する。
    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=5))
    sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 1)

    assert _life_raw(manager, PLAN_DATE) == d_raw_before  # ended を付けない
    assert manager.event_scheduler.has_key(keepalive_key)
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    assert overrides == overrides_before
    assert list(_messages(manager)) == messages_before  # 終了通知なし
    if place == "persona_life":
        assert _life_raw(manager, next_date) == bad_life_raw
    else:
        assert _life_raw(manager, next_date) is None
    assert _plan_meta_json(manager, next_date) == bad_meta_raw
    status, _result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_END, PLAN_DATE,
    )
    assert status == "failed"
    assert [s for s, _p in _dispatch_payloads(manager)][-1] == "failed"
    assert manager.event_scheduler.has_key(_schedule_key(close_sid))  # backoff

    # 記録を直す → 再試行で「新しい日が確定済み」と正しく判定される。
    if place == "persona_life":
        _set_life_raw(manager, next_date, good_raw)
        _set_legacy_meta_raw(manager, next_date, "{}")
    else:
        _write_legacy_meta(manager, next_date, {day_plan.META_LIVES: [{
            "start": "07:00", "end": "22:00", "budget_pulses": 18,
            "mode": "even", "used_pulses": 0, "judgment_pulses": 0,
            "started": True,
        }]})
    clock.advance_to(BASE + timedelta(days=1, hours=7, minutes=10))
    sm._handle_fire(close_sid, close_tok, close_occ, close_gen, 2)

    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert manager.event_scheduler.has_key(keepalive_key)
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    assert overrides == overrides_before
    assert not any("活動終了" in t for t in _messages(manager))
    status, result = _boundary_result(
        manager, day_plan.LIFE_BOUNDARY_KIND_END, PLAN_DATE,
    )
    assert status == "completed"
    assert result.get("stale") == f"superseded_by:{next_date}"
    assert [s for s, _p in _dispatch_payloads(manager)][-1] == "completed"


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("place", ["persona_life", "legacy"])
def test_close_of_a_day_with_a_corrupt_life_is_not_settled_and_is_retried(
    manager, place, corruption,
):
    """終了対象の日のライフの記録が壊れているとき、就寝は「ライフの無い日」と
    して成功扱いにならず失敗する (記録は変えない)。直した後の再試行で終わる。"""
    bad_life_raw, bad_meta_raw = CORRUPTIONS[corruption]
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _install_cache_override(manager)
    good_lives = [{
        "start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even",
        "used_pulses": 0, "judgment_pulses": 0, "started": True,
    }]
    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, bad_life_raw)
    else:
        _set_legacy_meta_raw(manager, PLAN_DATE, bad_meta_raw)
    clock.enable_virtual(BASE + timedelta(hours=22))

    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_END, plan_date=PLAN_DATE,
    ) is False
    if place == "persona_life":
        assert _life_raw(manager, PLAN_DATE) == bad_life_raw
    else:
        assert _life_raw(manager, PLAN_DATE) is None
        assert _plan_meta_json(manager, PLAN_DATE) == bad_meta_raw
    assert not manager.event_scheduler.has_key(TTL_CLEAR_KEY)
    assert not any("活動終了" in t for t in _messages(manager))

    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, json.dumps(good_lives))
    else:
        _write_legacy_meta(manager, PLAN_DATE, {day_plan.META_LIVES: good_lives})
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_END, plan_date=PLAN_DATE,
    ) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert sum("活動終了" in t for t in _messages(manager)) == 1


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("place", ["persona_life", "legacy"])
def test_wake_on_a_day_with_a_corrupt_life_does_not_overwrite_it(
    manager, session_factory, place, corruption,
):
    """起床の帳簿処理は、壊れたライフの記録を「未確定」と読んで新しいライフで
    上書きしない (破損を隠さない)・「ライフ無しの日」としても決着しない —
    失敗して再試行に回り、記録が直れば確定・開始する。"""
    bad_life_raw, bad_meta_raw = CORRUPTIONS[corruption]
    manager.personas[PERSONA_ID].model = "claude-sonnet-5"
    _add_day_schedule(session_factory, "judgment_day_open", "07:00")
    _add_day_schedule(session_factory, "judgment_day_close", "22:00")
    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, bad_life_raw)
    else:
        _set_legacy_meta_raw(manager, PLAN_DATE, bad_meta_raw)
    wake_at = BASE + timedelta(hours=7)
    clock.enable_virtual(wake_at)

    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START, occurrence_at=wake_at,
    ) is False
    if place == "persona_life":
        assert _life_raw(manager, PLAN_DATE) == bad_life_raw
    else:
        assert _life_raw(manager, PLAN_DATE) is None  # 新しい行で覆わない
        assert _plan_meta_json(manager, PLAN_DATE) == bad_meta_raw
    assert not any("活動開始" in t for t in _messages(manager))

    # 正常に空の記録へ直す → 再試行で確定・開始する。
    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, "[]")
    else:
        _set_legacy_meta_raw(manager, PLAN_DATE, "{}")
    assert day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START, occurrence_at=wake_at,
    ) is True
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert len(lives) == 1 and lives[0]["started"] is True


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
@pytest.mark.parametrize("place", ["persona_life", "legacy"])
def test_life_writer_refuses_to_write_over_a_corrupt_record(manager, place, corruption):
    """ライフの唯一の書き手 (_mutate_lives) は壊れた記録の上に書かない — 書き手が
    寛容に読むと、壊れた記録を空と数えて新しい内容で覆い、破損を隠す。"""
    bad_life_raw, bad_meta_raw = CORRUPTIONS[corruption]
    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, bad_life_raw)
    else:
        _set_legacy_meta_raw(manager, PLAN_DATE, bad_meta_raw)

    with pytest.raises(ValueError):
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "07:00", "end": "22:00", "budget_pulses": 5, "mode": "free"},
        ])
    if place == "persona_life":
        assert _life_raw(manager, PLAN_DATE) == bad_life_raw
    else:
        assert _life_raw(manager, PLAN_DATE) is None
        assert _plan_meta_json(manager, PLAN_DATE) == bad_meta_raw


@pytest.mark.parametrize("place", ["persona_life", "legacy"])
def test_strict_read_keeps_absent_empty_and_v03_shapes_readable(manager, place):
    """strict の中身の検査は「読み手が誤動作する形」だけを弾く (2026-10-10 Codex
    敵対レビュー 6 巡目)。記録が無い (行が無い・旧置き場に lives キーが無い)・
    正常な空配列・v0.3 の実データが持ちうる形 (マーカーの欠落・null、旧欄
    ``used_rounds`` などの余分な欄、未知の mode、予算の欠落) は従来どおり読める。
    寛容な読み口の挙動も変えない。"""
    # 記録が無い
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True) == []
    if place == "legacy":
        _set_legacy_meta_raw(manager, PLAN_DATE, '{"other": 1}')
        assert day_plan.get_lives(
            manager, PERSONA_ID, PLAN_DATE, strict=True,
        ) == []

    def _put(lives):
        if place == "persona_life":
            _set_life_raw(manager, PLAN_DATE, json.dumps(lives))
        else:
            _set_legacy_meta_raw(
                manager, PLAN_DATE, json.dumps({day_plan.META_LIVES: lives}),
            )

    # 正常な空配列 = ライフ無しの日
    _put([])
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True) == []

    tolerated = [
        {"start": "07:00", "end": "22:00", "budget_pulses": 18, "mode": "even"},
        {"start": "23:00", "end": "01:00", "mode": "unknown", "used_rounds": 3,
         "started": None, "ended": False, "future_field": {"x": 1}},
        {"start": "05:00", "end": "05:00", "budget_pulses": 1, "mode": "free",
         "started": True},
    ]
    _put(tolerated)
    assert day_plan.get_lives(
        manager, PERSONA_ID, PLAN_DATE, strict=True,
    ) == tolerated

    # 寛容な読み口は中身の破損を従来どおり縮退して読む (表示系の挙動は不変)。
    bad_life_raw, bad_meta_raw = CORRUPTIONS["marker_not_bool"]
    if place == "persona_life":
        _set_life_raw(manager, PLAN_DATE, bad_life_raw)
    else:
        _set_legacy_meta_raw(manager, PLAN_DATE, bad_meta_raw)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] == "false"
    for kind in ("null", "empty_string"):
        bad_life_raw, bad_meta_raw = CORRUPTIONS[kind]
        if place == "persona_life":
            _set_life_raw(manager, PLAN_DATE, bad_life_raw)
        else:
            _set_legacy_meta_raw(manager, PLAN_DATE, bad_meta_raw)
        assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
        with pytest.raises(ValueError):
            day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True)


# ---------------------------------------------------------------------------
# 回復 tick: 旧 judgment.day_open / day_close / post_session の席は撃ち直さずに閉じる
# ---------------------------------------------------------------------------


def test_recovery_expires_legacy_judgment_seats_without_refire(manager, session_factory):
    """回復 tick が切り替え前の judgment.day_open / day_close / post_session の
    prepared を拾っても、LLM の判断として撃ち直さず、期限で閉じる (起床・就寝は
    機械の帳簿処理になり、セッション終了判断は作業セッションごと撤去された)。"""
    from saiverse import execution_ledger as XL
    from saiverse import execution_ledger_wiring as xlw

    manager.pulse_controller = RecordingPulseController()
    _import_judgment_playbooks(session_factory)
    ledger = manager.execution_ledger
    eids = []
    for kind in ("day_open", "day_close", "post_session"):
        eid, runnable, _ = ledger.claim_execution(
            f"judgment.{kind}", idempotency_key=f"{PERSONA_ID}:{PLAN_DATE}",
            persona_id=PERSONA_ID,
        )
        assert runnable
        eids.append(eid)

    for row in ledger.list_prepared("judgment."):
        xlw._collect_one_prepared(
            manager, row,
            row["created_at"] + int(xlw.PREPARED_EXPIRE_AFTER_SECONDS) + 1,
        )

    for eid in eids:
        entry = ledger.get_execution(eid)
        assert entry["status"] == XL.STATUS_FAILED
        assert "expired" in entry["error"]
    assert manager.pulse_controller.calls == []
    assert ledger.list_prepared("judgment.") == []
