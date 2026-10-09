"""ライフ Phase 2「ライフの器」のテスト (docs/intent/life.md v0.5 §4/§5/§7/§11.2)。

v0.4 まではペルソナ (LLM) が起床判断でライフを「宣言」していたが、実機初日
(2026-07-13) の破綻を受けてまはー裁定で責任分界を全面改訂した——ライフは
ユーザー設定 (PersonaSchedule の起床・就寝) からシステムが確定する。宣言口・
重なり検証・谷コマ検証・均等モード間隔検証は書ける口ごと廃止した (v0.5 改修A)。
本ファイルは生き残った「ライフの器」(永続化・台帳・節目) のテスト。
システムによるライフ確定 (confirm_life_for_today) のテストは
``tests/test_life_confirmation.py`` を参照。

時間割 (コマの保存・予算ゲート・精算・ラウンド台帳と κ 減衰) と起床判断の
スキーマのテストは、時間割ごと撤去した v0.4 段 1-4 で消した。

一時 DB (in-memory SQLite) + 仮想クロックで検証する:

- lives の永続化 (get_lives/save_lives の round trip、bookkeeping フィールドの
  既定値・再宣言時の引き継ぎ)
- 検証: フォーマットのみ (v0.5 で重なり・谷コマ・均等モード間隔検証は廃止)
- mode の既定導出 (derive_default_life_mode): DEFAULT_MODEL の provider から
- 自発活動の記帳 (consume_life_pulse)
- 判断点発火 (fire_judgment_point) は used_pulses でなく judgment_pulses を
  別枠で記帳する (v0.5 §5.3/§8.2)
- 起床・就寝の節目の一度きり保証 (マーカー・台帳・単一 commit)
- 話しかけやすさ表示 (get_life_status_now) と keep-alive の営業日解決

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
from saiverse.event_scheduler import EventScheduler
from saiverse.meta_layer import MetaLayer
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
    """SAIMemory adapter の最小スタブ (append_persona_message の記録のみ)。

    ライフ境界の通知は台帳の outbox で届く (配送は _attach_boundary_ledger の
    記録ハンドラが受ける)。境界側は append_persona_message の有無で「届け先が
    あるか」を判定するので、口だけは本物に揃えておく。
    """

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
        sai_memory=FakeAdapter(),
        model=None,
    )
    personas = {PERSONA_ID: persona}
    mgr = SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        occupancy_manager=StubOccupancy(personas),
        event_scheduler=EventScheduler(),  # start() しない (シム/同期検証)
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
    )
    # 判断点の直列化 Lock (autonomy_wiring._judgment_lock) は本番 manager が
    # 無条件に持つ MetaLayer から取る — スタブも本物を積む。
    mgr.meta_layer = MetaLayer(mgr)
    return mgr


def _default_lives():
    # mode は両方 "free" にしておく (round trip / 重なり無しの共存だけが関心事)。
    return [
        {"start": "08:00", "end": "12:00", "budget_pulses": 6, "mode": "free"},
        {"start": "14:00", "end": "20:00", "budget_pulses": 8, "mode": "free"},
    ]


def _overwrite_lives(manager, plan_date, lives):
    """ライフの置き場 (persona_life) を検証なしで直接書き換える (使い切り・
    手編集・異常系の再現用)。旧置き場 meta_json.lives は v04 段 1-2 以降
    互換読みでしか読まれないので、そこを書いても正の置き場は変わらない。"""
    def _set(current):
        current[:] = [dict(life) for life in lives]
        return True

    day_plan._mutate_lives(manager, PERSONA_ID, plan_date, _set, context="test")


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


def test_save_and_get_lives_round_trip(manager):
    saved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert [(l["start"], l["end"]) for l in saved] == [
        ("08:00", "12:00"), ("14:00", "20:00"),
    ]
    for life in saved:
        assert life["used_pulses"] == 0
        assert "used_rounds" not in life  # 旧ラウンド台帳は段 1-4 で撤去
        assert life["judgment_pulses"] == 0
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == saved


def test_save_lives_empty_list_is_noop(manager):
    assert day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, []) == []
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_save_lives_redeclare_preserves_bookkeeping_by_start_end(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())

    life = day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["used_pulses"] == 1
    life = day_plan.record_judgment_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["judgment_pulses"] == 1

    # 起床判断のやり直し: 同じ (start, end) のライフを再宣言 → 消費は引き継がれる
    resaved = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, _default_lives())
    assert resaved[0]["used_pulses"] == 1
    assert resaved[0]["judgment_pulses"] == 1

    # 時刻が変わったライフは別物 (0 から)
    changed = [
        {"start": "08:00", "end": "13:00", "budget_pulses": 6, "mode": "free"},
    ]
    resaved2 = day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, changed)
    assert resaved2[0]["used_pulses"] == 0
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


def test_consume_life_pulse_accumulates(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 10, "mode": "free"},
    ])
    life = day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:00")
    assert life["used_pulses"] == 1
    life = day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="10:00")
    assert life["used_pulses"] == 2
    assert life["budget_pulses"] == 10


def test_consume_life_pulse_outside_any_life_is_noop(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "end": "12:00", "budget_pulses": 4, "mode": "free"},
    ])
    assert day_plan.consume_life_pulse(
        manager, PERSONA_ID, PLAN_DATE, at_time="20:00",
    ) is None
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_pulses"] == 0


def test_consume_life_pulse_noop_without_lives(manager):
    assert day_plan.consume_life_pulse(
        manager, PERSONA_ID, PLAN_DATE, at_time="09:00",
    ) is None




# ---------------------------------------------------------------------------
# fire_judgment_point: 判断点発火のライフ「別枠」記帳 (used_pulses は不変)
# ---------------------------------------------------------------------------


def _finalizing_controller(ledger):
    """finalize 相当 (判断行の mark_applied) まで進めるメタレーンのフェイク。

    台帳のある判断点は「finalize が applied を刻んだ」証跡で成功を判定する
    (judgment_points.run_judgment_point)。args の judgment_context に同乗した
    execution_id を finalize と同じように applied へ進める。
    """
    def _submit(**kwargs):
        ctx = json.loads((kwargs.get("args") or {}).get("judgment_context") or "{}")
        eid = ctx.get("execution_id")
        if eid:
            ledger.mark_applied(eid, result={"finalized": True})

    return SimpleNamespace(submit_meta_judgment=_submit)


def test_fire_judgment_point_records_judgment_pulse_separately(manager, session_factory):
    """判断点の発火は judgment_pulses だけを積む — used_pulses (予算) には
    触れない (life.md v0.5 §5.3/§8.2、実機初日の教訓)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    # 本番 manager は実行台帳を無条件に持つ (ライフ終了の節目は台帳の下で決着)。
    ledger, _delivered = _attach_boundary_ledger(manager)
    manager.pulse_controller = _finalizing_controller(ledger)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "end": "23:00", "budget_pulses": 5, "mode": "free"},
    ])

    result = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event",
        {"event_text": "来客", "stimulus_id": "test:stimulus:1"},
    )
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

    result = wiring.fire_judgment_point(
        manager, PERSONA_ID, "on_event",
        {"event_text": "来客", "stimulus_id": "test:stimulus:2"},
    )
    assert result["submitted"] is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


# ---------------------------------------------------------------------------
# 就寝の帳簿処理 (day_plan.handle_scheduled_life_boundary の end):
# 境界副作用の冪等ガード (Codex W3 第二陣 P1)。2026-10 (v04 段 1-2) に判断点の
# 前段 (旧 autonomy_wiring._apply_life_end_at_day_close) から機械の帳簿処理へ
# 切り出された — 契約 (マーカー・台帳・単一 commit) はそのまま。
# ---------------------------------------------------------------------------


def _end_test_lives(manager):
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "end": "23:00", "budget_pulses": 5, "mode": "free"},
    ])


def _settle_end(manager):
    """就寝時刻の帳簿処理を 1 回走らせる (自律 ON のペルソナとして)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    return day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_END,
    )


def _settle_start(manager, params=None):
    """起床時刻の帳簿処理を 1 回走らせる (自律 ON のペルソナとして)。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    return day_plan.handle_scheduled_life_boundary(
        manager, PERSONA_ID, day_plan.LIFE_BOUNDARY_START, params,
    )


def test_life_end_bookkeeping_applies_once_per_business_day(manager):
    """day_close の節目処理 (非冪等) は (persona, 営業日) につき一度だけ。

    判断 runtime の失敗で claim 行が prepared のまま残ると、schedule 側の
    backoff 再試行が fire_judgment_point を再突入させ、境界副作用 (keep-alive
    cancel + TTL 同期 + 「（活動終了）」通知) が再適用されていた (Codex W3
    第二陣 P1 の再現固定)。lives[0].ended マーカーで 2 回目以降は skip。
    """
    _ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    assert _settle_end(manager) is True
    assert _settle_end(manager) is True

    # 節目の実行は一度きり (2 回目はマーカーで台帳に触れる前に skip) で、
    # 通知も一通だけ
    assert [s for _eid, s in _boundary_end_rows(manager)] == ["completed"]
    assert len(delivered) == 1
    # マーカーは persona_life に永続化される — プロセスを跨いだ別インスタンス
    # 経由の再呼び出しでも DB 読み (get_lives) で skip される
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["ended"] is True


def test_life_end_bookkeeping_noop_without_lives(manager):
    """lives が無い日は従来どおり no-op (マーカーも書かない・行も作らない)。"""
    _ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))

    assert _settle_end(manager) is True

    assert _boundary_end_rows(manager) == []  # 節目の実行を作らない
    assert delivered == []
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE) == []


def test_apply_life_end_marker_not_written_when_steps_fail(manager):
    """順序は「確認 → 適用 → マーク」— 適用失敗ではマークしない (マーク先行
    だと適用されないまま封印される)。次の再試行が適用をやり直し、成功して
    はじめてマークされる (台帳は failed キーを退避して新しい実行を取る)。"""
    _ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    # 1 回目: 冪等段 (keep-alive 予約の cancel) が失敗 → 境界 False・マークなし
    with patch.object(
        day_plan, "_cancel_keepalive_reservation", return_value=False,
    ) as cancel:
        assert _settle_end(manager) is False
    assert cancel.call_count == 1
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")
    assert delivered == []
    assert [s for _eid, s in _boundary_end_rows(manager)] == ["failed"]

    # 再試行: 適用成功 → マーク → 以後は skip (通知は一通だけ)
    assert _settle_end(manager) is True
    assert _settle_end(manager) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert len(delivered) == 1
    assert sorted(s for _eid, s in _boundary_end_rows(manager)) == [
        "completed", "failed",
    ]


def test_life_end_notify_skipped_when_ttl_sync_fails(manager):
    """順序契約: 冪等段 (TTL 解除予約) の失敗は非冪等な通知の**前**に打ち切る —
    通知は outbox に積まれず、再試行で冪等段が再実行されても通知は重複ゼロ
    (Codex W3 第四陣 P2)。"""
    _ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(
        day_plan, "_sync_cache_ttl_for_life_end", return_value=False,
    ):
        assert _settle_end(manager) is False

    assert delivered == []
    assert _boundary_outbox_count(manager) == 0
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")


def _boundary_outbox_count(manager):
    from database.models import ExecutionOutboxItem

    db = manager.SessionLocal()
    try:
        return db.query(ExecutionOutboxItem).count()
    finally:
        db.close()


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
    """W5 正常系: ended マーカー + applied + 「（活動終了）」通知 outbox が
    単一 commit で確定し、即時配送される (実行は全配送済みで completed)。"""
    ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    assert _settle_end(manager) is True
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
        assert _settle_end(manager) is False
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")
    assert delivered == []  # マーカーも通知も残らない (単一 tx)
    statuses = sorted(status for _eid, status in _boundary_end_rows(manager))
    assert statuses == ["failed"]

    # 再試行: 回復 → マーカー + 通知が一度だけ
    assert _settle_end(manager) is True
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

    assert _settle_end(manager) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert delivered == []  # 一回目の即時配送は失敗 → pending 保持

    assert ledger.flush_pending_for_persona(PERSONA_ID) is True
    assert len(delivered) == 1
    assert "（活動終了）" in delivered[0]["content"]
    # 追加の flush でも二重追記しない (delivered 済み)
    ledger.flush_pending_for_persona(PERSONA_ID)
    assert len(delivered) == 1


def test_life_end_failure_returns_false_without_any_judgment_then_recovers(
    manager, session_factory,
):
    """就寝の帳簿処理の失敗は False で返り (呼び出し元の schedule が backoff
    再試行に乗せる)、判断点の席も LLM の起動も一切作らない — 起床・就寝は
    機械の帳簿処理だけ (v3 §6、v04 段 1-2)。再試行で節目が一度だけ適用される。

    旧形 (Codex W3 第五陣 P2): 判断点の前段で境界が失敗したら判断を打ち切って
    submitted=False を返していた。判断そのものが無くなったので、失敗の伝播先は
    schedule の再試行だけになった。"""
    _import_judgment_playbooks(session_factory)
    submissions: List[Dict[str, Any]] = []
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: submissions.append(kwargs),
    )
    ledger = _attach_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 22, 0, 0))
    _end_test_lives(manager)

    with patch.object(day_plan, "_sync_cache_ttl_for_life_end", return_value=False):
        assert _settle_end(manager) is False

    assert submissions == []  # LLM の判断は走らない
    assert _judgment_ledger_kinds(manager) == []  # 判断点の席も作らない
    assert not day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0].get("ended")

    assert _settle_end(manager) is True
    assert submissions == []
    assert _judgment_ledger_kinds(manager) == []
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["ended"] is True
    assert ledger is manager.execution_ledger


def _judgment_ledger_kinds(manager):
    """実行台帳に刻まれた判断点 (judgment.*) の KIND 一覧。"""
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
    """予算値 (budget_pulses/used_pulses) がライフ状態にそのまま乗る。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "end": "11:00", "budget_pulses": 4, "mode": "free"},
    ])
    day_plan.consume_life_pulse(manager, PERSONA_ID, PLAN_DATE, at_time="09:30")
    clock.enable_virtual(BASE + timedelta(hours=9, minutes=45))
    status = day_plan.get_life_status_now(manager, PERSONA_ID)
    assert status["life"]["used_pulses"] == 1
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
# 起床の帳簿処理: 節目の一度きり保証
# ---------------------------------------------------------------------------


def _attach_ledger(manager):
    from saiverse import execution_ledger as XL

    manager.execution_ledger = XL.ExecutionLedger(manager.SessionLocal)
    return manager.execution_ledger


def test_life_start_failure_keeps_life_unmarked_and_retry_applies_once(
    manager, session_factory,
):
    """起床の帳簿処理で節目が失敗 (TTL override 等) したら False を返し、
    started マーカーを書かない — ライフの確定は済む。再試行で節目が一度だけ
    適用され、判断点 (LLM) は最初から最後まで一度も起動しない (v04 段 1-2)。

    旧形 (Codex W3 第八陣): 判断点 day_open の前段で境界が失敗したら判断を
    打ち切っていた。判断そのものが無くなった。"""
    _import_judgment_playbooks(session_factory)
    submissions: List[Dict[str, Any]] = []
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: submissions.append(kwargs),
    )
    _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    _set_day_schedules(manager, session_factory)

    # 境界失敗は冪等段 (TTL override) で注入する — 通知はマーカーと同一 tx の
    # outbox なので、冪等段の失敗では積まれない。
    with patch.object(day_plan, "_sync_cache_ttl_for_life_start", return_value=False):
        assert _settle_start(manager) is False

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives and not lives[0].get("started")  # 確定は済むがマークされない
    assert submissions == []
    assert _judgment_ledger_kinds(manager) == []

    assert _settle_start(manager) is True
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True
    assert submissions == []
    assert _judgment_ledger_kinds(manager) == []


def test_day_open_boundary_applies_once_via_started_marker(manager, session_factory):
    """境界成功後の再突入 (force 等) でも started マーカーが節目の再適用を防ぐ。"""
    manager.personas[PERSONA_ID].autonomy_enabled = True
    _import_judgment_playbooks(session_factory)
    manager.pulse_controller = SimpleNamespace(
        submit_meta_judgment=lambda **kwargs: None,
    )
    _ledger, delivered = _attach_boundary_ledger(manager)
    clock.enable_virtual(datetime(2026, 7, 4, 8, 0, 0))
    _set_day_schedules(manager, session_factory)

    with patch.object(
        day_plan, "apply_life_boundary", wraps=day_plan.apply_life_boundary,
    ) as spy:
        assert _settle_start(manager) is True
        assert _settle_start(manager) is True
    assert spy.call_count == 1
    assert day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)[0]["started"] is True
    assert len(delivered) == 1  # 「（活動開始）」通知は一通だけ
    assert "（活動開始）" in delivered[0]["content"]
