"""saiverse/day_plan.py (ライフの帳簿と営業日の解決) のテスト。

v2 の時間割 (コマの保存・発火・繰り下げ・予算ゲート・精算) のテストは、時間割の
撤去 (autonomous_behavior_v04_plan.md 段 1-4) と一緒に消した。残るのは
生き残った側 — 営業日の解決 (resolve_business_day)・ライフ台帳の互換読み・
判断点の別枠記帳の並走耐性。

一時 DB (in-memory SQLite; Windows のファイルロック問題を構造的に回避) +
仮想クロックで検証する。teardown で engine.dispose() + clock.disable_virtual()
を必ず行う。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, PersonaDayPlan, User
from saiverse import clock
from saiverse import day_plan
from saiverse.event_scheduler import EventScheduler

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 0, 0, 0)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def session_factory():
    """In-memory SQLite session factory (thread 跨ぎ共有可)。"""
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
    """SAIVerseManager の最小スタブ (day_plan が触る実属性のみ)。"""
    from saiverse.execution_ledger import ExecutionLedger

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
        current_building_id="alice_room",
        private_room_id="alice_room",
    )
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない
        execution_ledger=ExecutionLedger(session_factory),
    )


def _add_day_open_schedule(session_factory, wake: str, close: str = None) -> None:
    """現行 PersonaSchedule (起床 / 就寝) を登録する。

    「日中に起床設定を変えた」状況の再現に使う — 確定ライフはその日の朝の値で
    保存済み、PersonaSchedule だけが新しい値、という分裂を作る。
    """
    from database.models import PersonaSchedule

    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK="judgment_day_open", TIME_OF_DAY=wake, ENABLED=True,
        ))
        if close is not None:
            db.add(PersonaSchedule(
                PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
                META_PLAYBOOK="judgment_day_close", TIME_OF_DAY=close,
                ENABLED=True,
            ))
        db.commit()
    finally:
        db.close()


def _write_legacy_plan_meta(session_factory, meta_json: str) -> None:
    """旧置き場 persona_day_plan.meta_json を直接書く (旧データの再現)。

    書き手 (時間割の保存口) は段 1-4 で撤去したので、互換読みの検証用に行を
    直接作る。
    """
    now = datetime(2026, 7, 4, 6, 0, 0)
    db = session_factory()
    try:
        db.add(PersonaDayPlan(
            persona_id=PERSONA_ID, plan_date=PLAN_DATE, slots_json="[]",
            meta_json=meta_json, created_at=now, updated_at=now,
        ))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 営業日の解決 (resolve_business_day) — 起床設定の日中変更
# ---------------------------------------------------------------------------


def _seed_overnight_life(manager, session_factory):
    """営業日 7/4 の確定ライフ 23:00〜06:00 を作り、その後に起床設定だけを
    07:00〜22:00 (跨がないリズム) へ変えた状況を作る。"""
    clock.enable_virtual(BASE + timedelta(hours=23, minutes=10))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "23:00", "end": "06:00", "budget_pulses": 20, "mode": "free"},
    ])
    # 日中に起床設定を変更 (跨ぎリズムをやめた)
    _add_day_open_schedule(session_factory, "07:00", close="22:00")


def test_business_day_follows_confirmed_life_not_current_schedule(
    manager, session_factory,
):
    """営業日の選択は当日確定ライフ基準 (Codex八巡目 #1)。

    確定ライフ 23:00〜06:00 の営業日 7/4 に対し、現行スケジュールを 07:00〜22:00
    へ変えた後の 7/5 00:30。現行スケジュールで営業日を選ぶと「7/5」と読む。
    """
    _seed_overnight_life(manager, session_factory)
    clock.enable_virtual(BASE + timedelta(days=1, minutes=30))  # 7/5 00:30

    basis = day_plan.resolve_business_day(manager, PERSONA_ID)

    assert basis.plan_date == PLAN_DATE   # 7/4 (現行スケジュールなら 7/5)
    assert basis.wake == "23:00"          # 起点も同じ解決器から
    assert basis.source == "life"


def test_business_day_falls_back_to_schedule_without_lives(
    manager, session_factory,
):
    """ライフの無い日は従来どおり現行 PersonaSchedule の営業日 (後方互換)。"""
    _add_day_open_schedule(session_factory, "07:00", close="01:00")
    clock.enable_virtual(BASE + timedelta(days=1, minutes=30))  # 7/5 00:30

    basis = day_plan.resolve_business_day(manager, PERSONA_ID)

    assert basis.plan_date == PLAN_DATE   # 跨ぎリズムの深夜帯 = 前日が営業日
    assert basis.wake == "07:00"
    assert basis.source == "schedule"


def test_zero_length_life_is_not_a_business_day_candidate(manager, session_factory):
    """区間として成立しないライフは営業日を名乗れない (Codex十巡目 #2)。

    書き手 (save_lives) は start == end を「長さ 0 のライフ」として拒否する。
    手編集・旧データで残った行を、読み手が跨ぎ規約で 24 時間ライフと読み替えると、
    その日が一日中「走行中のライフ」を名乗り、watchdog の窓・曜日ゲートまで外す。
    """
    with pytest.raises(ValueError):   # 書ける口は閉じている
        day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "07:00", "end": "07:00", "budget_pulses": 20, "mode": "free"},
        ])
    # 旧置き場の台帳を直接壊す (手編集・旧データの再現 — 互換読みが拾う)
    _write_legacy_plan_meta(session_factory, json.dumps({
        day_plan.META_LIVES: [
            {"start": "07:00", "end": "07:00", "budget_pulses": 20, "mode": "free",
             "used_pulses": 0, "judgment_pulses": 0},
        ],
    }))
    _add_day_open_schedule(session_factory, "09:00", close="18:00")
    clock.enable_virtual(BASE + timedelta(hours=12))

    basis = day_plan.resolve_business_day(manager, PERSONA_ID)

    assert basis.source == "schedule"   # 24 時間ライフを名乗らせない
    assert basis.plan_date == PLAN_DATE


def test_corrupt_legacy_meta_json_is_unreadable_not_missing(manager, session_factory):
    """壊れた旧台帳を「ライフ未宣言の日」と読まない (Codex九巡目 #2)。

    既定の読み口は不正 JSON を空へ縮退させるため、営業日の選択が例外経路しか
    見ていないと壊れた行が「ライフなし」に化け、現行スケジュール基準で別の
    営業日を駆動してしまう。
    """
    _write_legacy_plan_meta(session_factory, '{"lives": [{"start": "07:00"')
    clock.enable_virtual(BASE + timedelta(hours=12))

    # 寛容な既定の読み口は従来どおり縮退する (表示・ゲート系の後方互換)
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []
    # 営業日の選択だけは厳格に読み、壊れていることを検出する
    with pytest.raises(ValueError):
        day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE, strict=True)
    assert day_plan.resolve_business_day(manager, PERSONA_ID) is None


def test_business_day_is_none_when_lives_are_unreadable(manager):
    """ライフ読取の一時失敗は「ライフなし」に畳まず None (呼び出し元は待つ)。"""
    clock.enable_virtual(BASE + timedelta(hours=8))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    assert day_plan.resolve_business_day(manager, PERSONA_ID) is not None
    with patch.object(day_plan, "get_lives", side_effect=RuntimeError("db locked")):
        assert day_plan.resolve_business_day(manager, PERSONA_ID) is None


# ---------------------------------------------------------------------------
# ライフ台帳の並走耐性
# ---------------------------------------------------------------------------


def test_record_judgment_pulse_concurrent_increments_both_count(manager):
    """第七陣 P1 (Sol 再現): record_judgment_pulse を並走させても増分が失われない。

    旧実装は外で読んだ lives に +1 した完成値を書いていたため、並走 2 本で
    judgment_pulses が 2 でなく 1 になった。増分計算を CAS の再試行の内側へ
    移したことで、競合のたびに最新の lives の上へ積み直される。
    """
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    real_load = day_plan._load_life_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            # 読みの後・書きの前に、並走のもう 1 本が commit する
            day_plan.record_judgment_pulse(
                manager, PERSONA_ID, PLAN_DATE, at_time="10:00",
            )
        return row

    with patch.object(day_plan, "_load_life_row", side_effect=hooked):
        result = day_plan.record_judgment_pulse(
            manager, PERSONA_ID, PLAN_DATE, at_time="10:00",
        )

    assert result is not None and result["judgment_pulses"] == 2
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["judgment_pulses"] == 2  # 2 本とも数えられている


def test_save_lives_no_longer_writes_the_round_ledger(manager):
    """旧ラウンド台帳 (used_rounds) は段 1-4 で書き手ごと消えた。"""
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    assert "used_rounds" not in saved[0]
    assert saved[0]["used_pulses"] == 0
    assert saved[0]["judgment_pulses"] == 0
