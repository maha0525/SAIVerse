"""時間割 (saiverse/day_plan.py) のテスト — 保存・コマ発火配線 (自律行動 v2 §4.2)。

一時 DB (in-memory SQLite; Windows のファイルロック問題を構造的に回避) +
mock run_work_session + DaySimulator (仮想クロック) で検証する:

- 3 コマ (9:00 調べる / 14:00 随筆を書く / 20:00 自室で過ごす) が仮想時刻順に
  発火し、作業セッション系コマが mock run_work_session を正しい指示書引数で呼ぶ
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
from saiverse.persona_task_manager import STAGE_CANDIDATE, PersonaTaskManager
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
        """本物の OccupancyManager.move_entity と同じ契約 (W7 柱5)。

        成功時に persona.current_building_id を service 側で更新する
        (canonical sync)。失敗時は触らない。
        """

        def __init__(self, personas: Dict[str, Any]):
            self.moves: List[tuple] = []
            self._personas = personas
            self.fail_with: str | None = None  # 拒否理由を入れると移動失敗を再現

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            if self.fail_with is not None:
                return False, self.fail_with
            persona = self._personas.get(entity_id)
            if persona is not None:
                persona.current_building_id = to_id
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
    """task:1 (調べる用) と desire:2 (随筆用: desire ノート内の候補) を用意する。"""
    task_manager = PersonaTaskManager(manager.SessionLocal)
    t1 = task_manager.create_task(
        persona_id=PERSONA_ID,
        title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする",
        auto_activate=False,
    )
    t2 = task_manager.create_task(
        persona_id=PERSONA_ID,
        title="言葉の標本集",
        goal="気に入った言い回しを document にまとめる",
        origin="autonomous",
        auto_activate=False,
        desire_source="test-seed",
        stage=STAGE_CANDIDATE,
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
            "start": "09:00", "kind": "調べる", "ref": task_refs["task"],
            "facility": "library", "budget_rounds": 5,
            "note": "記事の続きを調べる",
        },
        {
            "start": "14:00", "kind": "随筆を書く", "ref": task_refs["desire"],
            "facility": "workshop", "budget_rounds": 12,
            "note": "標本集の下書きを作る",
        },
        {
            "start": "20:00", "kind": "自室で過ごす", "ref": "none",
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
        # 自室で過ごす に ref が付いている (kind/ref 不整合)
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


@pytest.mark.parametrize("legacy_kind", list(day_plan.LEGACY_KINDS))
def test_save_rejects_legacy_kinds(manager, legacy_kind):
    """封印済みの旧 kind (六型 + 暮らし/休む) は新規時間割の検証で拒否される。

    封印は「カタログに存在しない」で実現する (timetable_redesign.md §5.5/§9-2)。
    """
    with pytest.raises(ValueError, match="kind"):
        day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "09:00", "kind": legacy_kind, "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": ""},
        ])


def test_legacy_kind_history_survives_remaining_slot_replacement(manager):
    """消化済みの旧 kind コマ (帳簿) は残りコマ全置換の再検証を素通しする。

    封印前に保存された時間割 (旧語彙) の日中組み替えを、歴史の語彙を理由に
    全却下しない — 帳簿区間は kind 語彙の検証対象外 (時間割改修 T1 の移行互換)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "10:00", "kind": "調べる", "ref": "none",
         "facility": "library", "budget_rounds": 4, "note": "調べもの"},
    ])

    # 帳簿を旧語彙へ書き換えて「封印前に消化された日」を再現する
    def _make_legacy_done(slots):
        slots[0]["kind"] = "知る"
        slots[0]["status"] = day_plan.STATUS_DONE
        return True

    assert day_plan._mutate_slots_cas(
        manager, PERSONA_ID, PLAN_DATE, _make_legacy_done, context="test",
    )

    day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["kind"] for s in slots] == ["知る", "自室で過ごす"]
    assert slots[0]["status"] == day_plan.STATUS_DONE
    # 表示経路 (実績ラベル) も旧 kind の帳簿で壊れない
    assert day_plan.slot_result_label(slots[0]) == "実行済み"


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
                              metadata=None, *, manager=None, track_id=None,
                              title=None, close_hook=None):
        calls.append({
            "at": clock.now(),
            "persona_id": persona_id,
            "instruction": instruction,
            "budget_rounds": budget_rounds,
            "task_ref": task_ref,
            "metadata": metadata,
            "title": title,
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
    assert len(calls) == 2  # 自室で過ごす は run_work_session を呼ばない

    # 調べる (9:00): 仮想時刻・指示書・予算・task_ref
    learn = calls[0]
    assert learn["at"] == BASE + timedelta(hours=9)
    assert learn["persona_id"] == PERSONA_ID
    assert learn["budget_rounds"] == 5
    assert learn["task_ref"] == "task:1"
    assert "記事の続きを調べる" in learn["instruction"]          # slot.note
    assert "蒸留記事の続きを読む" in learn["instruction"]        # ref のタイトル
    assert "要点を覚え書きにする" in learn["instruction"]        # ref の goal
    assert "実際に読んで得られた内容だけ" in learn["instruction"]  # 接地文言

    # 随筆を書く (14:00)
    create = calls[1]
    assert create["at"] == BASE + timedelta(hours=14)
    assert create["budget_rounds"] == 12
    assert create["task_ref"] == "desire:2"
    assert "標本集の下書きを作る" in create["instruction"]
    assert "言葉の標本集" in create["instruction"]  # desire:2 のタイトル
    assert "document_create で実際に書き残すこと" in create["instruction"]
    assert "完成条件: 随筆が実在し" in create["instruction"]

    # 施設移動: 自室→図書館→工房→自室 (own_room は private_room_id に解決)
    assert manager.occupancy_manager.moves == [
        (PERSONA_ID, "ai", "alice_room", "library"),
        (PERSONA_ID, "ai", "library", "workshop"),
        (PERSONA_ID, "ai", "workshop", "alice_room"),
    ]

    # status 更新: 全コマ done。自室で過ごす (スタブ) には「詳細な実行記録なし」の
    # マーカーが付き、セッションを実際に運転した作業コマには付かない
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done", "done"]
    assert "record_level" not in slots[0]
    assert "record_level" not in slots[1]
    assert slots[2]["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY


# ---------------------------------------------------------------------------
# ユーザー会話中の繰り下げ
# ---------------------------------------------------------------------------


def _start_user_conversation(manager) -> str:
    """対ユーザー会話 Track を running で作り、会話の出来事も開く (= 会話中の状態)。

    「ユーザー会話中」の判定は開いている kind='conversation' の出来事の有無
    (life.md §7 案 Y、``day_plan.is_in_user_conversation``)。Track の running
    状態だけでは「会話中」と判定されなくなったため、出来事も明示的に開く。
    """
    from saiverse import episodes

    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID,
        track_type="user_conversation",
        title="対 tester 会話",
        is_persistent=True,
        output_target="building:current",
        metadata=json.dumps({"user_id": "1"}),
        initial_status=STATUS_RUNNING,
    )
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="alice_room", participants=[PERSONA_ID, "1"],
    )
    return track_id


def _end_user_conversation(manager) -> None:
    """会話終了 (wait_response タイムアウト相当): 会話の出来事を閉じる。

    life.md §7 案 Y 以降、タイムアウトは Track を pause しない (running のまま)。
    「会話中」判定に効くのは出来事の close だけなので、ここでも Track の pause
    ではなく出来事の close で会話終了を再現する。
    """
    from saiverse import episodes

    episodes.close_conversation_episode(manager, PERSONA_ID)


def test_slot_deferred_three_times_then_skipped(manager, task_refs):
    _start_user_conversation(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
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
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_DEFERRAL_LIMIT
    assert slots[0]["defer_count"] == 3
    # 会話中は施設移動もしない
    assert manager.occupancy_manager.moves == []
    # キューに残イベントが無い (skipped 後は再 push しない)
    assert manager.event_scheduler.pending_count() == 0


def test_deferred_slot_fires_after_conversation_ends(manager, task_refs):
    _start_user_conversation(manager)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
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

        # 会話終了 (wait_response timeout 相当: Track は running のまま、
        # 会話の出来事だけが閉じる — life.md §7 案 Y)
        _end_user_conversation(manager)

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
# facility='own_room' の移動処理 (自律行動 v2 §6.1)
# ---------------------------------------------------------------------------


def test_own_room_slot_skips_move_when_already_home(manager):
    """own_room は private_room_id に解決され、既に自室なら移動しない。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    DaySimulator(
        manager.event_scheduler,
        start=BASE + timedelta(hours=8),
        end=BASE + timedelta(hours=10),
    ).run()

    assert manager.occupancy_manager.moves == []  # 自室 → 自室は移動なし
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


def test_own_room_without_private_room_warns_and_continues(manager, caplog):
    """private_room_id の無いペルソナの own_room コマは移動スキップ (WARN) + ハンドラ実行。"""
    manager.personas[PERSONA_ID].private_room_id = None
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "随筆を書く", "ref": "none",
         "facility": "own_room", "budget_rounds": 3, "note": "下書き"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with caplog.at_level("WARNING", logger="saiverse.day_plan"):
        with patch("sea.work_session.run_work_session",
                   return_value=_mock_work_session_result()) as mock_ws:
            DaySimulator(
                manager.event_scheduler,
                start=BASE + timedelta(hours=8),
                end=BASE + timedelta(hours=10),
            ).run()

    assert manager.occupancy_manager.moves == []
    assert any("no private_room_id" in r.message for r in caplog.records)
    assert mock_ws.call_count == 1  # 移動できなくてもコマ自体は実行される
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


# ---------------------------------------------------------------------------
# 施設移動と persona.current_building_id (2026-07-05 実 LLM シム 異常 #1 回帰)
# ---------------------------------------------------------------------------


def test_move_updates_persona_current_building(manager, task_refs):
    """移動成功時に persona.current_building_id が移動先に更新される。

    move_entity は設計上この属性を書き換えない (呼び出し側責務)。day_plan が
    更新しないと、作業セッションの head/audience と成果物の配置先が終日
    旧建物のままになる (2026-07-05 実 LLM シムで実証)。
    """
    persona = manager.personas[PERSONA_ID]
    assert persona.current_building_id == "alice_room"
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result()):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    assert manager.occupancy_manager.moves == [
        (PERSONA_ID, "ai", "alice_room", "library"),
    ]
    # 呼び出し側 (day_plan) が属性を更新している (stub は触らない)
    assert persona.current_building_id == "library"


def test_move_failure_runs_in_place_and_notifies_persona(manager, task_refs):
    """移動失敗 (満員等) は現在地で実行 + その事実をペルソナに通知する。

    黙って現在地文脈にならないこと: current_building_id は旧地のまま、
    event_message タグの system 通知が SAIMemory に入り、ハンドラは実行される。
    """
    persona = manager.personas[PERSONA_ID]
    notices: List[Dict[str, Any]] = []
    persona.sai_memory = SimpleNamespace(
        append_persona_message=lambda payload: notices.append(payload),
    )
    manager.occupancy_manager.fail_with = "図書館は定員オーバーです"
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5,
         "title": "記事を調べる", "note": "調べもの"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result()) as mock_ws:
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    # フォールバック: 移動せず現在地で実行 (コマ自体は流れない)
    assert mock_ws.call_count == 1
    assert persona.current_building_id == "alice_room"
    # 失敗の事実がペルソナに見える形で記録される
    assert len(notices) == 1
    content = notices[0]["content"]
    assert "移動できませんでした" in content
    assert "記事を調べる" in content        # どのコマか
    assert "定員オーバー" in content          # なぜか
    assert "event_message" in notices[0]["metadata"]["tags"]
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


# ---------------------------------------------------------------------------
# reschedule_pending_slots の冪等性
# ---------------------------------------------------------------------------


def test_reschedule_pending_slots_is_idempotent(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
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

    assert mock_ws.call_count == 1  # 調べる 1 回のみ (二重発火しない)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]


def test_downtime_recovery_reclassifies_grace_exceeded_slots(manager, task_refs):
    """再起動回復: grace 超過の pending は遅延実行せず「流れた」に確定 (Codex一巡目 #2)。

    起床判断の途中起動 (compose の missed_start) と同じ意味論 — 停止が
    「組む前」か「組んだ後」かで、同じ停止時間の扱いが割れない。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        {"start": "13:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    # 09:00 開始のコマが 3 時間過去 = サーバーが落ちていた間に流れた
    clock.enable_virtual(BASE + timedelta(hours=12))

    pushed = day_plan.reschedule_pending_slots(
        manager, PERSONA_ID, downtime_recovery=True,
    )

    assert pushed == 1  # 13:00 (未来) だけが再 push される
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_MISSED_START
    assert slots[1]["status"] == "pending"


def test_downtime_recovery_respects_defer_allowance(manager, task_refs):
    """繰り下げ済みコマは defer_count ぶんの猶予を足して判定する (正当な遅れを流さない)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:45", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの",
         "status": "deferred", "defer_count": 1},
    ])
    # 15 分過去: 素の grace (10 分) は超えるが、繰り下げ 1 回ぶんの猶予
    # (10+10=20 分) 以内 → 流さず再 push
    clock.enable_virtual(BASE + timedelta(hours=10))

    pushed = day_plan.reschedule_pending_slots(
        manager, PERSONA_ID, downtime_recovery=True,
    )

    assert pushed == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "deferred"  # 流れていない


def test_watchdog_recovery_keeps_late_slots_running(manager, task_refs):
    """プロセス内回復 (downtime_recovery なし) は従来どおり遅延実行に倒す。

    サーバーは生きているので「サーバーが起動していなかったため」という理由での
    確定はできない — 長セッション待ち等の遅れを停止と誤記しない。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=12))

    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID) == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "pending"  # 再 push され、流れてはいない


def test_reschedule_repushes_deferred_and_skips_done(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "調べもの",
         "status": "deferred", "defer_count": 1},
        {"start": "14:00", "kind": "自室で過ごす", "ref": "none",
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


def test_all_kinds_have_registered_handlers():
    """カタログの全 kind にハンドラが解決する (バグ2(a) 回帰)。

    2026-07-05 の実 LLM シムで、有効な kind のコマが no handler で skipped
    になり、就寝判断がそれを本人の「見送り」として作話した。カタログの全 kind が
    組み込みで処理されることを固定する。
    """
    assert day_plan.ALL_KINDS, "kind vocabulary must not be empty"
    for kind in day_plan.ALL_KINDS:
        assert kind in day_plan._SLOT_HANDLERS, f"kind={kind!r} has no handler"
    # 作業セッション系 (execution_type='work_session') は予算ゲート対象
    assert day_plan.WORKER_SESSION_KINDS
    for kind in day_plan.WORKER_SESSION_KINDS:
        assert kind in day_plan._BUDGET_GATED_KINDS, f"kind={kind!r} not budget-gated"
        assert kind in day_plan.ALL_KINDS
    # それ以外 (出かける/自室で過ごす/自由時間) はスタブ (予算を消費しない)
    for kind in set(day_plan.ALL_KINDS) - set(day_plan.WORKER_SESSION_KINDS):
        assert kind not in day_plan._BUDGET_GATED_KINDS, f"kind={kind!r} budget-gated"
    # WORKER_SESSION_KINDS と指示書テンプレートの同期 (旧 assert の回帰固定)
    assert set(day_plan.WORKER_SESSION_KINDS) == set(
        day_plan._WORKER_INSTRUCTION_TEMPLATES
    )


def test_legacy_kinds_are_sealed():
    """封印済みの旧語彙 (六型 + 暮らし/休む) は kind 語彙からもハンドラからも消えている。

    封印は「プロンプトで禁止」ではなく「選択肢として存在しない」で実現する
    (timetable_redesign.md §9-2)。
    """
    for kind in day_plan.LEGACY_KINDS:
        assert kind not in day_plan.ALL_KINDS, f"legacy kind={kind!r} still valid"
        assert kind not in day_plan._SLOT_HANDLERS, f"legacy kind={kind!r} has handler"
        assert kind not in day_plan._BUDGET_GATED_KINDS


@pytest.mark.parametrize(
    "kind, grounding",
    [
        ("調べる", "「読んだ」「調べた」と書かないこと"),
        ("絵を描く", "実際に生成できていない絵を「描いた」と書かないこと"),
        ("日記を書く", "実際に起きていない出来事を日記に書かないこと"),
        ("随筆を書く", "実際に経験・読んでいないことを事実として書かないこと"),
    ],
)
def test_new_worker_kinds_run_sessions_with_grounded_instruction(
    manager, task_refs, kind, grounding
):
    """カタログの作業セッション系 4 種が接地文言つきの指示書で発火する。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": kind, "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 4, "note": "取り組む"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    calls: List[Dict[str, Any]] = []

    def fake_ws(persona_id, instruction, budget_rounds, **kwargs):
        calls.append({"instruction": instruction, "budget_rounds": budget_rounds})
        return _mock_work_session_result(rounds_used=budget_rounds)

    with patch("sea.work_session.run_work_session", side_effect=fake_ws):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    assert len(calls) == 1
    assert calls[0]["budget_rounds"] == 4
    assert "取り組む" in calls[0]["instruction"]          # slot.note
    assert "蒸留記事の続きを読む" in calls[0]["instruction"]  # ref のタイトル
    assert grounding in calls[0]["instruction"]           # 接地文言
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"


def test_unregistered_kind_is_skipped_with_system_reason(manager, task_refs, caplog):
    """ハンドラ未登録 kind は skipped + skip_reason='no_handler' (バグ2(b) 回帰)。

    全 kind が組み込み登録済みのため、未登録状態はレジストリから外して再現する。
    システム都合のスキップが「見送り」(本人判断) として提示されないことは
    slot_result_label 側のテストで固定する。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "絵を描く", "ref": task_refs["task"],
         "facility": "park", "budget_rounds": 0, "note": "公園を歩く"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch.dict(day_plan._SLOT_HANDLERS):
        del day_plan._SLOT_HANDLERS["絵を描く"]
        with caplog.at_level("WARNING", logger="saiverse.day_plan"):
            DaySimulator(
                manager.event_scheduler,
                start=BASE + timedelta(hours=8),
                end=BASE + timedelta(hours=10),
            ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_NO_HANDLER
    assert any("no handler registered" in r.message for r in caplog.records)
    # 未登録 kind では施設移動しない
    assert manager.occupancy_manager.moves == []


def test_old_vocabulary_kind_is_skipped_honestly(manager, task_refs, caplog):
    """旧語彙 (カタログに無い kind) の未消化コマは専用理由で正直にスキップされる。

    語彙再編前のバージョンが保存した plan には旧 kind (暮らし 等) の pending が
    残りうる (Codex 一巡目 #1)。旧→新の勝手な読み替え (意図の捏造) はせず、
    no_handler (システム障害) とも区別した kind_not_in_vocabulary で記録する。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "絵を描く", "ref": "none",
         "facility": "park", "budget_rounds": 0, "note": ""},
    ])
    # 旧バージョンの保存データを再現 (保存検証は旧 kind を弾くため直接書き換え)
    day_plan._update_slot(manager, PERSONA_ID, PLAN_DATE, 0, kind="暮らし")
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with caplog.at_level("INFO", logger="saiverse.day_plan"):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=8),
            end=BASE + timedelta(hours=10),
        ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_KIND_NOT_IN_VOCABULARY
    # システム障害 (no_handler) の WARNING ではなく語彙世代交代の INFO
    assert not any("no handler registered" in r.message for r in caplog.records)
    assert any("not in the current catalog" in r.message for r in caplog.records)
    assert manager.occupancy_manager.moves == []


def test_slot_result_label_distinguishes_system_skips():
    """実績ラベル: システム都合の skipped を本人判断の「見送り」として見せない。"""
    assert day_plan.slot_result_label(
        {"status": "skipped", "skip_reason": day_plan.SKIP_REASON_NO_HANDLER}
    ).startswith("実行できず（システム側の問題")
    assert day_plan.slot_result_label(
        {"status": "skipped", "skip_reason": day_plan.SKIP_REASON_BUDGET_EXHAUSTED}
    ) == "実行できず（作業ラウンドの日次予算切れ）"
    assert day_plan.slot_result_label(
        {"status": "skipped", "skip_reason": day_plan.SKIP_REASON_DEFERRAL_LIMIT}
    ) == "流れた（ユーザーとの会話を優先したため）"
    assert day_plan.slot_result_label(
        {"status": "skipped",
         "skip_reason": day_plan.SKIP_REASON_KIND_NOT_IN_VOCABULARY}
    ) == "実行できず（このコマ種別は現在の時間割では使われていません）"
    # 理由の無い旧データは中立表現 (「見送り」に倒さない)
    legacy = day_plan.slot_result_label({"status": "skipped"})
    assert "見送り" not in legacy
    assert legacy == "実行されず（理由の記録なし）"
    assert day_plan.slot_result_label({"status": "done"}) == "実行済み"
    assert day_plan.slot_result_label({}) == "未実施"


def test_stub_slots_record_presence_only(manager):
    """出かける/自室で過ごす スタブ: done + record_level='presence_only' を永続化する。

    スタブでも施設への実移動 (presence) は本物として行う — カフェ等に実際に
    居ることが遭遇と会話のきっかけになる (まはー決定 2026-07-05、旧 暮らし/休む
    スタブから継承)。詳細な実行記録が無いことだけをマーカーで残し、表示側が
    「実行済み」と偽らない。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "10:00", "kind": "出かける", "ref": "none",
         "facility": "cafe", "budget_rounds": 0, "note": "カフェで過ごす"},
        {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    DaySimulator(
        manager.event_scheduler,
        start=BASE + timedelta(hours=9),
        end=BASE + timedelta(hours=22),
    ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]
    assert [s["record_level"] for s in slots] == [
        day_plan.RECORD_LEVEL_PRESENCE_ONLY,
        day_plan.RECORD_LEVEL_PRESENCE_ONLY,
    ]
    # 施設移動はスタブでも実行される (own_room は private_room_id に解決)
    assert manager.occupancy_manager.moves == [
        (PERSONA_ID, "ai", "alice_room", "cafe"),
        (PERSONA_ID, "ai", "cafe", "alice_room"),
    ]


def test_free_choice_slot_degrades_to_stay_home_with_warning(manager, caplog):
    """自由時間: 選択が成立しないときは自室で過ごす相当に縮退し、WARNING で明示する。

    静かな別物化をしない — 縮退であることをログに残す (T3。選択→委譲の本線は
    tests/test_free_slot_handlers.py が検証する)。この manager スタブには
    pulse_dispatcher が無いため、縮退先の軽い一手 Pulse も起動できず
    presence_only の正直記録に落ちる (fail-open の二段縮退)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "10:00", "kind": "自由時間", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "好きに過ごす"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with patch.object(
        day_plan, "_choose_free_time_kind", return_value=None,
    ), caplog.at_level("WARNING", logger="saiverse.day_plan"):
        DaySimulator(
            manager.event_scheduler,
            start=BASE + timedelta(hours=9),
            end=BASE + timedelta(hours=12),
        ).run()

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert slots[0]["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY
    assert any(
        "free-choice slot" in r.message and "degrading to stay-home" in r.message
        for r in caplog.records
    )


def test_slot_result_label_presence_only_done():
    """実績ラベル: 詳細記録の無い done (presence スタブ) を「実行済み」と偽らない。

    「実行済み」と提示すると、ペルソナが就寝ふりかえりでしていない活動の
    内容 (食事の選定等) を捏造する (soft-confabulation、2026-07-05 実 LLM シム
    異常 #4 の回帰)。
    """
    assert day_plan.slot_result_label(
        {"status": "done", "record_level": day_plan.RECORD_LEVEL_PRESENCE_ONLY}
    ) == "時間を過ごした（詳細な記録なし）"
    # マーカーの無い done (旧データ / セッション系) は従来どおり (後方互換)
    assert day_plan.slot_result_label({"status": "done"}) == "実行済み"
    # done 以外では record_level はラベルに影響しない
    assert day_plan.slot_result_label(
        {"status": "pending", "record_level": day_plan.RECORD_LEVEL_PRESENCE_ONLY}
    ) == "未実施"


def test_record_level_survives_remaining_slot_replacement(manager):
    """record_level は帳簿の一部 — 残りコマ全置換の再検証を通っても保持される。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "10:00", "kind": "出かける", "ref": "none",
         "facility": "cafe", "budget_rounds": 0, "note": "カフェで過ごす",
         "status": "done", "record_level": day_plan.RECORD_LEVEL_PRESENCE_ONLY},
        {"start": "14:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "早めに休む"},
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert slots[0]["record_level"] == day_plan.RECORD_LEVEL_PRESENCE_ONLY
    # 新規コマには record_level が付かない
    assert "record_level" not in slots[1]


def test_skip_reason_survives_remaining_slot_replacement(manager, task_refs):
    """skip_reason は帳簿の一部 — 残りコマ全置換の再検証を通っても保持される。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 4, "note": "調べもの",
         "status": "skipped", "skip_reason": day_plan.SKIP_REASON_NO_HANDLER},
        {"start": "14:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "早めに休む"},
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "skipped"
    assert slots[0]["skip_reason"] == day_plan.SKIP_REASON_NO_HANDLER
    # 新規コマには skip_reason が付かない
    assert "skip_reason" not in slots[1]


def test_replace_remaining_allows_restart_at_consumed_slot_time(manager, task_refs):
    """正当な組み替え: 消化済みコマと同時刻から始まる残りコマの全置換は通る。

    2026-07-05 実 LLM シム 3回目の回帰: 13:30 コマ直後の post_session が
    「13:30 のコマを ref を直して置き直す」組み替えを返したところ、消化済み
    13:30 コマとの境界比較で『昇順でない』と全却下された。昇順検証は新コマ
    区間のみに適用し、消化済み区間 (歴史) は保護したまま置換を通す。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:30", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 4, "note": "調べもの",
         "status": "done"},
        {"start": "13:30", "kind": "随筆を書く", "ref": task_refs["task"],
         "facility": "workshop", "budget_rounds": 6, "note": "済んだコマ",
         "status": "done"},
        {"start": "15:30", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    pushed, notes = day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "13:30", "kind": "随筆を書く", "ref": task_refs["desire"],
         "facility": "workshop", "budget_rounds": 4, "note": "ref を直してやり直す"},
        {"start": "15:30", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    assert pushed == 2
    assert notes == []

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [
        ("09:30", "done"), ("13:30", "done"),
        ("13:30", "pending"), ("15:30", "pending"),
    ]
    # 消化済みコマは書き換えられていない (歴史の保護)
    assert slots[1]["ref"] == task_refs["task"]
    # 新コマはペルソナの意志どおり
    assert slots[2]["ref"] == task_refs["desire"]


def test_replace_remaining_still_rejects_unordered_new_slots(manager, task_refs):
    """新コマ区間そのものが昇順でない置換は従来どおり全却下 (plan も予約も不変)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:30", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 4, "note": "",
         "status": "done"},
        {"start": "15:30", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    with pytest.raises(ValueError, match="not strictly ascending"):
        day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "16:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": ""},
            {"start": "15:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": ""},
        ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [
        ("09:30", "done"), ("15:30", "pending"),
    ]
    assert manager.event_scheduler.has_key(
        day_plan._slot_key(PERSONA_ID, PLAN_DATE, slots[1]["id"])
    )


def test_handler_failure_leaves_slot_fired(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "随筆を書く", "ref": "none",
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


# ---------------------------------------------------------------------------
# 深夜跨ぎ: wake の自己解決 (検収追加 2026-07-12)
# ---------------------------------------------------------------------------


def test_schedule_resolves_wake_for_overnight_slot(manager, monkeypatch):
    """wake 引数を渡さない呼び出し (起床判断 finalize の経路) でも、
    PersonaSchedule から起床時刻を自己解決して深夜コマを翌暦日に予約する。

    委譲実装は wake をオプトイン引数にしていたため、主経路 (finalize →
    schedule_day_plan) では深夜コマが「当日の 00:30 = 過去」となり編成直後に
    即発火するバグが残っていた — その回帰テスト。
    """
    from datetime import datetime as _dt

    from database.models import PersonaSchedule

    db = manager.SessionLocal()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK="judgment_day_open", ENABLED=True, TIME_OF_DAY="07:00",
        ))
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK="judgment_day_close", ENABLED=True, TIME_OF_DAY="01:00",
        ))
        db.commit()
    finally:
        db.close()

    # 注: save_day_plan の昇順検証は暦時刻ベースのため、深夜帯コマは
    # リスト末尾には置けない (時間割の意味論は当面「暦日内」— 深夜帯は
    # コマの無い自由時間)。ここでは暦時刻昇順で保存できる並びを使い、
    # 「万一 start < wake のコマが存在した場合に翌暦日で予約される」
    # 防御 (wake 自己解決) だけを検証する。
    slots = [
        {"start": "00:30", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "深夜の一息"},
        {"start": "09:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ]
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, slots)

    captured: List[tuple] = []
    monkeypatch.setattr(
        day_plan, "_push_slot",
        lambda mgr, pid, pdate, sid, fire_at: captured.append((sid, fire_at)),
    )
    pushed = day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)  # wake 渡さない
    assert pushed == 2

    fire_by_id = dict(captured)
    ids = _slot_ids(manager)
    d = _dt.fromisoformat(PLAN_DATE)
    assert fire_by_id[ids[1]] == d.replace(hour=9, minute=0)
    # 深夜コマ (start < wake) は翌暦日 (同じ営業日の尻尾) として予約される
    assert fire_by_id[ids[0]] == (d + timedelta(days=1)).replace(hour=0, minute=30)


# ---------------------------------------------------------------------------
# 実行台帳で包む三区間発火 (W2 Chunk B, A5/A6) — 予約 / ハンドラ / 精算の原子性
#
# 台帳付き manager で _fire_slot を同期直呼びし、予約 tx・精算 tx の各段で障害を
# 注入して、A5 (予算精算の非原子性) と A6 (done 保存失敗で episode 永久 open) が
# 単一患部で解けていることを確認する。ライフ正典まわりの A5 は test_life_phase2.py。
# ---------------------------------------------------------------------------


def _attach_ledger(manager):
    """manager に実行台帳を結線して返す (autonomy_wiring テストと同じ流儀)。"""
    from saiverse import execution_ledger as XL

    manager.execution_ledger = XL.ExecutionLedger(manager.SessionLocal)
    return manager.execution_ledger


def _slot_exec_id(manager, kind="slot.fire"):
    """台帳に採番された slot.fire 実行の execution_id を取り出す (無ければ None)。"""
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        row = (
            db.query(ExecutionLedgerEntry)
            .filter(ExecutionLedgerEntry.KIND == kind)
            .order_by(ExecutionLedgerEntry.CREATED_AT.desc())
            .first()
        )
        return row.EXECUTION_ID if row is not None else None
    finally:
        db.close()


def _save_single_gated_slot(manager, task_refs, *, budget_rounds=5):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": budget_rounds, "note": "調べもの"},
    ])


def test_reservation_tx_failure_skips_handler_and_leaves_state_unchanged(manager, task_refs):
    """A5: 予約 tx が転けたら handler は 0 回・slot pending・予算不変・台帳 prepared。"""
    _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("saiverse.episodes.open_episode", side_effect=RuntimeError("db down")), \
            patch("sea.work_session.run_work_session") as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 0  # 不可逆処理 (ハンドラ) は始まっていない
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "pending"  # fired にならない (全ロールバック)
    # 予算は不変 (予約分も巻き戻る)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 0
    # 台帳は prepared のまま (mark_running が同 tx で巻き戻る) — 安全に再実行できる
    assert len(manager.execution_ledger.list_prepared("slot.fire")) == 1
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None


def test_settlement_failure_leaves_fired_open_running_with_reserved_budget(manager, task_refs):
    """A6/A5: 精算 tx (episode close) 失敗 → slot=fired・episode=open・台帳=running・
    予算は予約額のまま (返金されない)。回復で拾える状態に収束する。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)) as mock_ws, \
            patch("saiverse.episodes.close_episode", side_effect=RuntimeError("commit fail")):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 1  # ハンドラは走った
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "fired"  # done に進まない (精算ロールバック)
    from saiverse import episodes
    open_ep = episodes.get_open_episode(manager, PERSONA_ID)
    assert open_ep is not None and open_ep["status"] == "open"  # 出来事は開いたまま
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "running"
    # 予算は予約額 (5) のまま — 実測 3 への精算 (返金) は適用されない (A5 の安全側)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 5


def test_settlement_failure_via_mark_applied_is_also_atomic(manager, task_refs):
    """A6: 精算 tx 内の台帳 applied 書き込み失敗でも done/episode/予算が全ロールバック。

    done 保存・episode close・予算調整・applied は単一 tx なので、どの書き込みが
    転けても収束状態は同じ (fired・open・running・予約額保持)。
    """
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    def _boom(*a, **k):
        raise RuntimeError("applied write fail")

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)), \
            patch.object(ledger, "mark_applied", side_effect=_boom):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "fired"
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is not None
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "running"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 5


def test_successful_fire_settles_once_then_double_fire_is_ignored(manager, task_refs):
    """A5/A6: 正常発火で used が一度だけ増え slot が一度だけ done・台帳 completed。
    同 index の再発火は二重発火ガード (claim not runnable) で handler を呼ばない。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    # 予約 5 → 実測 3 に精算 (返金 -2) されて used == 実測 3
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None  # 閉じている
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"

    # 二重発火: 同 index を再度 fire しても claim not runnable で handler は呼ばれない
    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=99)) as mock_ws2:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    assert mock_ws2.call_count == 0
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"  # 依然 done (二重に何も起きない)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3  # 不変


def test_mutate_slots_cas_preserves_concurrent_replacement(manager, task_refs):
    """CAS 更新 (第五陣 P1): 読みと書きの間に別の書き手が置換を commit したら、
    古い配列を書き戻さず (= 置換を消さず)、最新 plan で変異をやり直す。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "13:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 3, "note": "A"},
        {"start": "15:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "B"},
    ])
    calls = {"n": 0}

    def mutate(slots):
        calls["n"] += 1
        if calls["n"] == 1:
            # 読みの後・書きの前に、別の書き手が A/B → C の置換を commit する
            day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
                {"start": "18:00", "kind": "自室で過ごす", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "note": "C"},
            ])
            slots[0]["status"] = "deferred"  # 古い配列への変異 — 書かれてはいけない
            return slots[0]
        # 再試行では置換後の最新 plan が渡される
        assert [s["note"] for s in slots] == ["C"]
        slots[0]["note"] = "C-touched"
        return slots[0]

    result = day_plan._mutate_slots_cas(
        manager, PERSONA_ID, PLAN_DATE, mutate, context="test",
    )
    assert calls["n"] == 2                       # 1回目は世代不一致で棄却→再試行
    assert result is not None and result["note"] == "C-touched"
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    # C は消えず (A/B が復活せず)、再試行の変異だけが乗っている
    assert [(s["note"], s["status"]) for s in slots] == [("C-touched", "pending")]


def test_update_slot_does_not_resurrect_replaced_plan(manager, task_refs):
    """第五陣 P1 の Sol 再現: _update_slot の読みと書きの間に replace_remaining_slots
    が A/B → C を commit しても、古い A/B の書き戻しで C が消えない (A の deferred
    化は対象消失として中止される)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "13:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 3, "note": "A"},
        {"start": "15:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "B"},
    ])
    a_id = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["id"]

    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
                {"start": "18:00", "kind": "自室で過ごす", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "note": "C"},
            ])
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked):
        result = day_plan._update_slot(
            manager, PERSONA_ID, PLAN_DATE, 0, expected_id=a_id, status="deferred",
        )

    assert result is None  # A は新世代に存在しない → 何も書かず中止
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["note"], s["status"]) for s in slots] == [("C", "pending")]


def test_reserve_conflict_preserves_replacement_and_aborts_fire(manager, task_refs):
    """第五陣 P1 の tx 側: 予約 tx の読みと書きの間に置換が commit されたら、
    古い配列で fired を書き込まず (置換を消さず)、再試行 → 対象消失で安全に離脱する。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs)  # 09:00 の gated コマ A
    clock.enable_virtual(BASE + timedelta(hours=9))

    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
                {"start": "10:00", "kind": "自室で過ごす", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "note": "C"},
            ])
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked), \
            patch("sea.work_session.run_work_session") as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 0  # ハンドラは走らない
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    # 置換 (C) が保存されたまま — 古い配列の fired 書き戻しで消えていない
    assert [(s["note"], s["status"]) for s in slots] == [("C", "pending")]
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 0
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None
    # 台帳: 対象消失で prepared → failed (abandon)。走行痕跡なし
    exec_id = _slot_exec_id(manager)
    assert exec_id is not None
    assert ledger.get_execution(exec_id)["status"] == "failed"


def test_reserve_tx_preserves_concurrent_meta_update(manager, task_refs):
    """第六陣 P1 (Sol 再現): 予約 tx の読みと書きの間に update_plan_meta (明日メモ)
    が commit されても、古い meta の書き戻しでメモを消さない — meta を書くときは
    meta も CAS 条件に含め、競合時は最新 meta から予算を再計算する。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "old"})
    clock.enable_virtual(BASE + timedelta(hours=9))

    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            day_plan.update_plan_meta(
                manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "new"},
            )
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked), \
            patch("sea.work_session.run_work_session",
                  return_value=_mock_work_session_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 1
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta.get("tomorrow_memo") == "new"  # 並走メモが消えていない
    # 予算は最新 meta 上で予約→精算され実測 3 に収束
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"


def test_settle_tx_preserves_concurrent_meta_update(manager, task_refs):
    """第六陣 P1 の精算側: 精算 tx (返金 delta の meta 書き込み) の読み書き間に
    commit された明日メモが消えない。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "old"})
    clock.enable_virtual(BASE + timedelta(hours=9))

    real_load = day_plan._load_plan_row
    calls = {"n": 0}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        calls["n"] += 1
        # 1 回目 = 予約 tx (素通し)。2 回目 = 精算 tx の読み — ここで割り込む
        if calls["n"] == 2 and row is not None:
            day_plan.update_plan_meta(
                manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "new"},
            )
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked), \
            patch("sea.work_session.run_work_session",
                  return_value=_mock_work_session_result(rounds_used=3)):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta.get("tomorrow_memo") == "new"  # 精算の書き戻しで消えていない
    # 返金 (5 予約 → 実測 3) は最新 meta 上で再計算されて適用済み
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"


def test_update_plan_meta_preserves_concurrent_budget_write(manager, task_refs):
    """第六陣 P1 の逆方向 (transfer): update_plan_meta の読み書き間に別の meta
    書き込み (予算等) が commit されても、古い meta のマージ書き戻しで消さない。"""
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "old"})

    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            # 読みの後・書きの前に、別の書き手が予算消費を commit する
            day_plan.update_plan_meta(
                manager, PERSONA_ID, PLAN_DATE, {day_plan.META_BUDGET_USED: 7},
            )
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked):
        merged = day_plan.update_plan_meta(
            manager, PERSONA_ID, PLAN_DATE, {"tomorrow_memo": "new"},
        )

    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta.get("tomorrow_memo") == "new"          # 自分の更新は書けている
    assert meta.get(day_plan.META_BUDGET_USED) == 7    # 並走の予算書き込みも生存
    assert merged.get(day_plan.META_BUDGET_USED) == 7  # 再試行は最新 meta へマージ


def test_record_judgment_pulse_concurrent_increments_both_count(manager, task_refs):
    """第七陣 P1 (Sol 再現): record_judgment_pulse を並走させても増分が失われない。

    旧実装は外で読んだ lives に +1 した完成値を update_plan_meta へ渡していたため、
    CAS が最新 meta を読み直しても古い完成値が同キーを上書きし、並走 2 本で
    judgment_pulses が 2 でなく 1 になった。増分計算を CAS の再試行の内側へ移した
    ことで、競合のたびに最新 meta の上へ積み直される。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    real_load = day_plan._load_plan_row
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

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked):
        result = day_plan.record_judgment_pulse(
            manager, PERSONA_ID, PLAN_DATE, at_time="10:00",
        )

    assert result is not None and result["judgment_pulses"] == 2
    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["judgment_pulses"] == 2  # 2 本とも数えられている


def test_consume_budget_concurrent_increments_accumulate(manager, task_refs):
    """第七陣 P1: consume_budget の並走増分が失われず合算される (used = 4 + 3)。"""
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 4)
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked):
        day_plan.consume_budget(manager, PERSONA_ID, PLAN_DATE, 3)

    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 7


def test_consume_life_rounds_concurrent_increments_accumulate(manager, task_refs):
    """第七陣 P1: consume_life_rounds の並走増分が失われず合算される (4 + 3)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    real_load = day_plan._load_plan_row
    state = {"fired": False}

    def hooked(db, pid, pdate):
        row = real_load(db, pid, pdate)
        if not state["fired"] and row is not None:
            state["fired"] = True
            day_plan.consume_life_rounds(
                manager, PERSONA_ID, PLAN_DATE, 4, at_time="10:00",
            )
        return row

    with patch.object(day_plan, "_load_plan_row", side_effect=hooked):
        day_plan.consume_life_rounds(
            manager, PERSONA_ID, PLAN_DATE, 3, at_time="10:00",
        )

    lives = day_plan.get_lives(manager, PERSONA_ID, PLAN_DATE)
    assert lives[0]["used_rounds"] == 7


def test_fire_slot_follows_identity_when_index_shifts(manager, task_refs):
    """id 照準 (第四陣 P1): 呼び出し元の index が組み替えでズレていても、
    slot_id が指すコマを発火する — _fire_slot 自身が読んだ plan で id を解決する。"""
    _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "X"},
        {"start": "10:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "Y"},
    ])
    x_id = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["id"]
    clock.enable_virtual(BASE + timedelta(hours=9))

    # 「id→index 解決後に組み替えで index がズレた」状況を、ズレた index=1 と
    # 正しい照準 slot_id=X を渡すことで再現する
    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 1, slot_id=x_id)

    assert mock_ws.call_count == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"      # X (id の指すコマ) が発火した
    assert slots[1]["status"] == "pending"   # ズレた index の Y は無傷


def test_fire_slot_with_vanished_id_is_noop_even_if_index_valid(manager, task_refs):
    """id 照準 (第四陣 P1 の Sol 再現): 発火直前に置換で消えた id は、渡された
    index に有効なコマが居ても発火しない (別コマの前倒し done を防ぐ)。"""
    _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs)  # index 0 に有効な 09:00 コマ
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0, slot_id="vanished0000")

    assert mock_ws.call_count == 0
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "pending"       # index 0 のコマは撃たれていない
    assert _slot_exec_id(manager) is None        # 台帳にも実行が採番されていない


def test_concurrent_claim_loser_does_not_break_winners_running_ledger(manager, task_refs):
    """二重 claim の後発が離脱しても先発の running 台帳を壊さない (Codex 第三陣 [P1])。

    claim_execution は既存 prepared を再利用し、並走発火の両方に同じ execution_id
    を runnable として返す。旧実装は後発が _SlotVanished 離脱時に無条件 mark_failed
    で共有台帳を failed へ落とし、先発の精算が failed → applied の IllegalTransition
    で爆発した (slot=fired・予算予約済み・episode=open・台帳=failed の壊れた収束)。
    後発の離脱は abandon_prepared (prepared のときだけ failed) に変更済み。
    """
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    slot = slots[0]
    slot_id = slot["id"]
    # 後発 (B) が先に claim だけ済ませた窓を作る — 先発 (A) の claim は同じ
    # prepared 行を再利用するので exec_id は共有される
    exec_id, runnable, _ = ledger.claim_execution(
        "slot.fire", idempotency_key=f"{PERSONA_ID}:{PLAN_DATE}:{slot_id}",
        persona_id=PERSONA_ID,
        payload={"persona_id": PERSONA_ID, "plan_date": PLAN_DATE, "index": 0,
                 "slot_id": slot_id, "slot_kind": slot["kind"], "reserved_rounds": 5},
    )
    assert runnable

    def handler_with_interleaved_loser(*args, **kwargs):
        # 先発 A のハンドラ実行中に、後発 B の続き (予約 tx → 離脱) が走る:
        # slot は A が fired 済みなので B は _SlotVanished で中断する
        with pytest.raises(day_plan._SlotVanished):
            day_plan._reserve_slot_tx(
                manager, ledger, exec_id, PERSONA_ID, PLAN_DATE, 0, slot_id,
                slot, True, 5,
            )
        # 離脱時、共有台帳は running (A の所有) — failed に落とせず無変更
        assert not ledger.abandon_prepared(exec_id, "slot vanished (loser exit)")
        assert ledger.get_execution(exec_id)["status"] == "running"
        return _mock_work_session_result(rounds_used=3)

    with patch("sea.work_session.run_work_session",
               side_effect=handler_with_interleaved_loser) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    # 先発の精算が爆発せず、実行は一度だけ完走する
    assert mock_ws.call_count == 1
    assert _slot_exec_id(manager) == exec_id  # exec_id は共有されていた
    assert ledger.get_execution(exec_id)["status"] == "completed"
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None


def test_reserve_tx_seat_race_loser_rolls_back_without_side_effects(manager, task_refs):
    """席取り (prepared → running の条件付き遷移) に負けた側は _ClaimLost で全
    ロールバックし、slot / 予算 / episode / 台帳のどれにも痕跡を残さない。

    二重 claim の両方が「slot はまだ pending」を見た同着ケース — 旧実装は両方が
    mark_running を通過しうる (二重実行・予算二重予約・出来事二重 open)。
    """
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    slot_id = slots[0]["id"]
    exec_id, runnable, _ = ledger.claim_execution(
        "slot.fire", idempotency_key=f"{PERSONA_ID}:{PLAN_DATE}:{slot_id}",
        persona_id=PERSONA_ID, payload={},
    )
    assert runnable
    # 勝者が先に席を取った (running)
    assert ledger.try_mark_running(exec_id)

    # 敗者の予約 tx: slot はまだ pending に見えるが、席取り CAS で負ける
    with pytest.raises(day_plan._ClaimLost):
        day_plan._reserve_slot_tx(
            manager, ledger, exec_id, PERSONA_ID, PLAN_DATE, 0, slot_id,
            slots[0], True, 5,
        )

    # 全ロールバック: slot pending・予算不変・episode 無し・台帳は勝者の running のまま
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["status"] == "pending"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 0
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None
    assert ledger.get_execution(exec_id)["status"] == "running"


def test_handler_raise_marks_unknown_closes_episode_retains_reservation(manager, task_refs):
    """防御経路: ハンドラ例外 → 台帳 unknown・episode close (best-effort)・slot fired・
    予約額保持 (LLM が動いたか不明なので自動再実行しない照合対象にする)。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session", side_effect=RuntimeError("boom")):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "fired"  # 「実行したが完了記録なし」
    from saiverse import episodes
    assert episodes.get_open_episode(manager, PERSONA_ID) is None  # best-effort close 済み
    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "unknown"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 5  # 予約保持


def test_no_ledger_manager_falls_back_to_legacy_fire(manager, task_refs):
    """縮退: execution_ledger を持たない manager では従来経路で done へ到達する。"""
    assert getattr(manager, "execution_ledger", None) is None
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)) as mock_ws:
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    # 旧経路: consume_budget が実測 3 を積む
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3


# ---------------------------------------------------------------------------
# A1: replace_day_plan — 起床判断 day_open の原子的全置換
#
# 検証失敗時に「旧予約だけ先に消えて plan が孤児化する」旧順序を断ち、旧 plan・
# 旧予約とも一切変更しないことを確認する (監査 A1)。
# ---------------------------------------------------------------------------


def _slot_keys_present(manager, plan_date=PLAN_DATE):
    """現在 EventScheduler に有効な当該 plan のコマ index 集合 (key はコマ id ベース)。"""
    slots = day_plan.load_day_plan(manager, PERSONA_ID, plan_date) or []
    present = set()
    for i, slot in enumerate(slots):
        sid = slot.get("id")
        if sid and manager.event_scheduler.has_key(
            day_plan._slot_key(PERSONA_ID, plan_date, sid)
        ):
            present.add(i)
    return present


def _slot_ids(manager, plan_date=PLAN_DATE):
    """現在の plan のコマ id 列 (index 順)。"""
    slots = day_plan.load_day_plan(manager, PERSONA_ID, plan_date) or []
    return [s.get("id") for s in slots]


def _seed_two_slot_plan(manager, task_refs):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "13:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 3, "note": "旧1"},
        {"start": "15:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)


def test_replace_day_plan_success_swaps_reservations(manager, task_refs):
    """成功時: 旧コマの予約が残らず、新 plan の pending だけが予約される。"""
    _seed_two_slot_plan(manager, task_refs)
    assert _slot_keys_present(manager) == {0, 1}
    old_ids = _slot_ids(manager)

    pushed, notes = day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "18:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    assert pushed == 1
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["18:00"]
    # 新 plan のコマだけが予約されている
    assert _slot_keys_present(manager) == {0}
    # 旧コマ (id ベース key) の予約は cancel 済みで残らない
    for old_id in old_ids:
        assert not manager.event_scheduler.has_key(
            day_plan._slot_key(PERSONA_ID, PLAN_DATE, old_id)
        )


def test_replace_cancel_failure_stale_reservation_harmless_and_watchdog_detects(
    manager, task_refs,
):
    """cancel 失敗で旧時刻の予約が残留しても誤発火せず、watchdog が途絶を検出する
    (Codex 第三陣 [P1])。

    旧 index ベース key では、残留した旧予約 (13:00/15:00) が新 plan (18:00/20:00)
    と同じ key 文字列を持つため、(1) watchdog は find_lost == [] で異常を見逃し、
    (2) 旧時刻の発火が新 plan の別コマを前倒しで誤発火させた。id ベース key への
    移行でこの両方が塞がれていることの回帰。
    """
    _seed_two_slot_plan(manager, task_refs)  # 13:00 / 15:00 を予約済み
    old_slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    old_ids = _slot_ids(manager)

    # cancel が最初から失敗する状況 (Codex の再現条件)。入力は**旧コマを id ごと
    # 写して時刻だけ変えた**最悪形 (第四陣 P2) — 全置換の id 新世代化が無いと
    # 新旧の予約 key が同一になり、以下の (1)(2) が両方破れる。
    with patch.object(
        manager.event_scheduler, "cancel", side_effect=RuntimeError("cancel down"),
    ):
        pushed, _notes = day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {**old_slots[0], "start": "18:00"},
            {**old_slots[1], "start": "20:00"},
        ])
    assert pushed == 0  # 張り替え失敗は例外にせず watchdog 回復へ委ねる
    # 全置換は id の新世代を採番する (旧 id の持ち越し禁止 — 第四陣 P2)
    assert set(_slot_ids(manager)).isdisjoint(set(old_ids))

    # 旧予約は残留している (cancel できなかった)
    for old_id in old_ids:
        assert manager.event_scheduler.has_key(
            day_plan._slot_key(PERSONA_ID, PLAN_DATE, old_id)
        )
    # (1) 残留予約に隠されず、新コマ 2 件の予約途絶が検出される (旧実装は [])
    assert day_plan.find_lost_slot_reservations(manager, PERSONA_ID, PLAN_DATE) == [0, 1]

    # (2) 残留した旧予約が発火しても、新 plan のコマを誤発火しない (無害な空振り)
    with patch("sea.work_session.run_work_session") as mock_ws:
        day_plan._fire_slot_by_id(manager, PERSONA_ID, PLAN_DATE, old_ids[0])
    assert mock_ws.call_count == 0
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["pending", "pending"]  # 新コマは無傷

    # 回復: watchdog 相当の再 push で新コマの予約が張り直され、途絶は解消する
    assert day_plan.reschedule_pending_slots(manager, PERSONA_ID, PLAN_DATE) == 2
    assert day_plan.find_lost_slot_reservations(manager, PERSONA_ID, PLAN_DATE) == []


def test_replace_remaining_mints_fresh_ids_for_new_slots(manager, task_refs):
    """残りコマ置換: 新コマ区間は入力が id を写していても新世代を採番する。
    消化済み区間 (帳簿) は既存 id を保持する (精算・回復の逆引き対象のため)。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:30", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 4, "note": "", "status": "done"},
        {"start": "15:30", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": ""},
    ])
    before = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    history_id, old_pending_id = before[0]["id"], before[1]["id"]

    day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
        {**before[1], "start": "16:00"},  # 旧 pending を id ごと写した入力
    ])
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["id"] == history_id       # 帳簿は identity 維持
    assert slots[1]["id"] != old_pending_id   # 新コマは新世代


def test_replace_day_plan_format_failure_leaves_plan_and_reservations(manager, task_refs):
    """書式検証失敗 (ValueError) — 旧 plan・旧予約とも一切変更されない。"""
    _seed_two_slot_plan(manager, task_refs)
    before = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with pytest.raises(ValueError):
        day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "9時", "kind": "自室で過ごす", "ref": "none",  # start 書式不正
             "facility": "own_room", "budget_rounds": 0, "note": ""},
        ])

    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == before
    assert _slot_keys_present(manager) == {0, 1}  # 予約も無傷


def test_replace_day_plan_all_excluded_by_life_leaves_plan_and_reservations(manager, task_refs):
    """ライフ範囲で全除外 (ValueError) — 旧 plan・旧予約とも不変。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=12))  # 12:00
    _seed_two_slot_plan(manager, task_refs)
    before = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)

    with pytest.raises(ValueError):
        # 23:00 は就寝 (22:00) より後 — 丸めようが無く全除外 → kept 空
        day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "23:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": ""},
        ])

    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == before
    assert _slot_keys_present(manager) == {0, 1}


# ---------------------------------------------------------------------------
# D5: 精算失敗で running のまま残ったコマ発火の settle-close 回復
#
# test_settlement_failure_...（上）が作る収束状態 (fired/open/running/予約額保持)
# を、deadline 超過後に回復 tick の _collect_stale_slot_executions が一度だけ
# settle-close することを統合的に確認する。
# ---------------------------------------------------------------------------


def _age_execution(manager, execution_id, seconds):
    """台帳行の UPDATED_AT を seconds 秒だけ過去へずらす (deadline 超過を作る)。"""
    from database.models import ExecutionLedgerEntry

    db = manager.SessionLocal()
    try:
        db.query(ExecutionLedgerEntry).filter(
            ExecutionLedgerEntry.EXECUTION_ID == execution_id
        ).update({"UPDATED_AT": ExecutionLedgerEntry.UPDATED_AT - int(seconds)})
        db.commit()
    finally:
        db.close()


def _make_stale_settled_slot(manager, task_refs):
    """精算 tx (episode close) を壊して fired/open/running/予約額保持 に収束させる。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))
    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)), \
            patch("saiverse.episodes.close_episode", side_effect=RuntimeError("commit fail")):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    return ledger, _slot_exec_id(manager)


def test_recovery_settle_closes_stale_running_slot(manager, task_refs):
    """回復: deadline 超過の running slot.fire が settle-close される
    (episode closed・slot done・台帳 completed・予算は予約額 5 のまま)。"""
    from saiverse import episodes, execution_ledger_wiring as wiring

    ledger, exec_id = _make_stale_settled_slot(manager, task_refs)
    # 収束状態の確認
    assert ledger.get_execution(exec_id)["status"] == "running"
    assert episodes.get_open_episode(manager, PERSONA_ID) is not None

    # deadline (900s) 超過へ (仮想時計を 20 分進める)
    clock.advance_to(BASE + timedelta(hours=9, minutes=20))
    wiring._collect_stale_slot_executions(manager)

    assert ledger.get_execution(exec_id)["status"] == "completed"
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    assert episodes.get_open_episode(manager, PERSONA_ID) is None  # 閉じた
    # 予算は予約額のまま (返金しない保守精算)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 5


def test_recovery_is_idempotent_on_double_tick(manager, task_refs):
    """二度目の tick は running でない (= 既に completed) ので何もしない。"""
    from saiverse import execution_ledger_wiring as wiring

    ledger, exec_id = _make_stale_settled_slot(manager, task_refs)
    clock.advance_to(BASE + timedelta(hours=9, minutes=20))
    wiring._collect_stale_slot_executions(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"
    used_after_first = day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"]

    # 2 本目: no-op (list_running が completed を返さない + settle のガード)
    clock.advance_to(BASE + timedelta(hours=9, minutes=40))
    wiring._collect_stale_slot_executions(manager)
    assert ledger.get_execution(exec_id)["status"] == "completed"
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["status"] == "done"
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == used_after_first


def test_recovery_deadline_spares_fresh_running_slot(manager, task_refs):
    """deadline 未満の running slot.fire は settle されない (稼働中の誤 settle 回避)。"""
    from saiverse import execution_ledger_wiring as wiring

    ledger, exec_id = _make_stale_settled_slot(manager, task_refs)
    # まだ deadline (900s) 未満 (5 分しか経っていない)
    clock.advance_to(BASE + timedelta(hours=9, minutes=5))
    wiring._collect_stale_slot_executions(manager)
    assert ledger.get_execution(exec_id)["status"] == "running"  # 触られない


# ---------------------------------------------------------------------------
# Codex レビュー指摘 (2026-07-20) の回帰
# ---------------------------------------------------------------------------


def test_settle_marks_slot_by_id_after_midhandler_reshuffle(manager, task_refs):
    """Finding 2: ハンドラ中に時間割が組み替わっても (post_session の
    replace_remaining_slots)、精算は発火した当該コマ (不変 id) を done にする —
    配列 index ではなく id で引くため、前詰めで別コマを誤って done にしない。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "08:00", "kind": "自室で過ごす", "ref": "none",
         "facility": "own_room", "budget_rounds": 0, "note": "defer"},
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "work"},
    ])
    # index 0 を deferred にして「index 0=deferred / index 1=実行中」の状況を作る。
    day_plan._update_slot(manager, PERSONA_ID, PLAN_DATE, 0, status="deferred")
    clock.enable_virtual(BASE + timedelta(hours=9))
    worker_id = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[1]["id"]

    def _reshuffle_then_return(*a, **k):
        # ハンドラ中の残り時間割置換: 実行中コマ (fired) が index 0 へ前詰めされ、
        # 新コマ (20:00) が index 1 に入る。
        day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": "new"},
        ])
        return _mock_work_session_result(rounds_used=3)

    with patch("sea.work_session.run_work_session", side_effect=_reshuffle_then_return), \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 1)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    by_id = {s["id"]: s for s in slots}
    # 発火した当該コマが done (index が動いても id で正しく締まる)
    assert by_id[worker_id]["status"] == "done"
    # 新規に前詰めされたコマ (現 index 1) は pending のまま — 誤 done にしない
    new_slot = next(s for s in slots if s["start"] == "20:00")
    assert new_slot["status"] == "pending"
    assert new_slot["id"] != worker_id
    # 予算・台帳は当該コマぶんだけ精算 (予約 5 → 実測 3、completed)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 3
    assert ledger.get_execution(_slot_exec_id(manager))["status"] == "completed"


def test_slot_idempotency_key_is_stable_id_not_index(manager, task_refs):
    """Finding 2: 冪等キーが不変 id ベースなので、組み替えで旧 index に来た別コマは
    誤って二重発火扱いにならず、独立に発火できる。"""
    _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 40)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))
    first_id = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["id"]

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=3)), \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["status"] == "done"

    # 別コマを同じ index 0 に置く (id は別) → 冪等キーが id ベースなので発火できる。
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "10:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "second"},
    ])
    second_id = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["id"]
    assert second_id != first_id
    clock.advance_to(BASE + timedelta(hours=10))
    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=2)) as mock_ws, \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    assert mock_ws.call_count == 1  # 別 id なので二重発火ガードに掛からない
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["status"] == "done"


def test_recovery_closes_orphaned_unknown_slot_episode(manager, task_refs):
    """Finding 3: ハンドラ例外 + episode close 失敗が重なると episode が open のまま
    unknown へ進む。回復がその孤児 episode を閉じる (slot/台帳の状態は保つ)。"""
    from saiverse import episodes, execution_ledger_wiring as wiring

    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session", side_effect=RuntimeError("boom")), \
            patch("saiverse.episodes.close_episode", side_effect=RuntimeError("close fail")):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    exec_id = _slot_exec_id(manager)
    assert ledger.get_execution(exec_id)["status"] == "unknown"
    assert episodes.get_open_episode(manager, PERSONA_ID) is not None  # 孤児

    wiring._close_orphaned_unknown_slot_episodes(manager)

    assert episodes.get_open_episode(manager, PERSONA_ID) is None  # 閉じた
    # slot は fired のまま (handler 失敗なので done ではない)、台帳も unknown のまま
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)[0]["status"] == "fired"
    assert ledger.get_execution(exec_id)["status"] == "unknown"
    # 冪等: 二度目は open episode が無いので no-op (例外を投げない)
    wiring._close_orphaned_unknown_slot_episodes(manager)
    assert episodes.get_open_episode(manager, PERSONA_ID) is None


def test_reservation_failure_does_not_touch_desire(manager, task_refs):
    """Finding 4: 予約 tx が転けたら touch_desire は呼ばれない (取り組んでいない欲求を
    再試行のたびに再訪記録して昇格候補へ押し上げない)。予約成立後に初めて touch する。"""
    _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)  # ref = task:...
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("saiverse.episodes.open_episode", side_effect=RuntimeError("db down")), \
            patch("saiverse.desire_engine.touch_desire") as mock_touch, \
            patch("sea.work_session.run_work_session"):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    assert mock_touch.call_count == 0  # 予約が転けたので再訪も記録しない

    # 予約が成立すれば touch は一度だけ呼ばれる (予約後へ移動したことの確認)
    with patch("saiverse.desire_engine.touch_desire") as mock_touch2, \
            patch("sea.work_session.run_work_session",
                  return_value=_mock_work_session_result(rounds_used=3)), \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)
    assert mock_touch2.call_count == 1


def test_negative_used_rounds_is_rejected_no_over_refund(manager, task_refs):
    """Finding 5: ハンドラが負の used_rounds を返しても返金として受理しない — 予約額を
    そのまま消費として残し、台帳にも負値を保存しない (旧 consume 系と同じ非負要求)。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    with patch("sea.work_session.run_work_session",
               return_value=_mock_work_session_result(rounds_used=-3)), \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert slots[0]["status"] == "done"
    # 予約額 5 のまま (負 delta による過剰返金なし)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 5
    exec_id = _slot_exec_id(manager)
    result = ledger.get_execution(exec_id)["result"]
    assert result["used_rounds"] is None  # 負値は保存しない
    assert ledger.get_execution(exec_id)["status"] == "completed"


def test_replace_day_plan_persistent_save_failure_keeps_old_plan_and_reservations(manager, task_refs):
    """Finding 1 (継続障害): 保存を先に試みるので、DB 保存が **継続的に** 失敗しても
    旧 plan は DB に残り、旧予約は cancel 前なので無傷 — 「旧 plan 可視・予約消失」の
    孤児を作らない (前修正の「復元も同じ保存に依存」で継続障害だと予約消失が再発した
    のを、保存先行で恒久的に断つ)。"""
    _seed_two_slot_plan(manager, task_refs)
    before = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert _slot_keys_present(manager) == {0, 1}

    # 継続障害 (常に失敗) — 復元経路に頼らないことを保証する。
    with patch("saiverse.day_plan._upsert_plan_slots", side_effect=RuntimeError("save fail")):
        with pytest.raises(RuntimeError):
            day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
                {"start": "18:00", "kind": "自室で過ごす", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "note": "new"},
            ])

    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == before  # 旧 plan 不変
    assert _slot_keys_present(manager) == {0, 1}  # 旧予約 無傷 (cancel 前に保存失敗)


def test_replace_day_plan_persistent_repush_failure_converges_to_new_plan(manager, task_refs):
    """Finding 1 (継続障害): 保存後の再予約が **継続的に** 失敗しても、DB は既に新 plan
    なので「旧 plan 可視・予約消失」の孤児にはならず、watchdog が回復できる状態へ収束
    する (例外にせず pushed=0)。監査 A1「明示的な回復状態へ収束」。"""
    _seed_two_slot_plan(manager, task_refs)

    with patch("saiverse.day_plan.schedule_day_plan", side_effect=RuntimeError("push fail")):
        pushed, _notes = day_plan.replace_day_plan(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "18:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": "new"},
        ])

    assert pushed == 0  # 例外にせず、予約回復は watchdog へ委ねる
    # DB は新 plan (孤児 = 旧 plan が見えるまま、ではない)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["18:00"]
    # watchdog が「予約途絶」として検出でき回復できる (旧予約は cancel 済み)
    assert day_plan.find_lost_slot_reservations(manager, PERSONA_ID, PLAN_DATE) == [0]


def test_reserve_aborts_when_slot_vanishes_between_claim_and_reservation(manager, task_refs):
    """Finding 2: claim 後・予約前に別判断が replace_remaining_slots で当該コマを外すと、
    予約 tx が id で対象消失を検知しハンドラを始めず中断・台帳 failed にする (発火時
    index で別コマを誤って実行しない)。"""
    ledger = _attach_ledger(manager)
    day_plan.init_budget_ledger(manager, PERSONA_ID, PLAN_DATE, 20)
    _save_single_gated_slot(manager, task_refs, budget_rounds=5)
    clock.enable_virtual(BASE + timedelta(hours=9))

    real_reserve = day_plan._reserve_slot_tx

    def reshuffle_then_reserve(*a, **k):
        # claim 後・予約 tx 実行の直前に当該コマ (pending) を時間割から外す。
        day_plan.replace_remaining_slots(manager, PERSONA_ID, PLAN_DATE, [
            {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
             "facility": "own_room", "budget_rounds": 0, "note": "other"},
        ])
        return real_reserve(*a, **k)

    with patch("saiverse.day_plan._reserve_slot_tx", side_effect=reshuffle_then_reserve), \
            patch("sea.work_session.run_work_session") as mock_ws, \
            patch("saiverse.autonomy_wiring.fire_judgment_point",
                  return_value={"submitted": False}):
        day_plan._fire_slot(manager, PERSONA_ID, PLAN_DATE, 0)

    assert mock_ws.call_count == 0  # ハンドラは始まらない (別コマを実行しない)
    assert ledger.get_execution(_slot_exec_id(manager))["status"] == "failed"  # 副作用ゼロ中断
    # 置換で入った別コマ (20:00) は fired にされず pending のまま
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert all(s["status"] == "pending" for s in slots)
    # 予算も動かない (予約前に中断)
    assert day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)["used"] == 0
