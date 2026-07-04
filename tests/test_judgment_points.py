"""判断点の基盤 + 起床/セッション終了の 2 判断点のテスト (judgment_points.md §4/§6)。

一時 DB (in-memory SQLite) + mock LLM 出力 (構造化出力 dict) + 仮想クロックで検証:

- day_open: 動的スキーマ (ref/facility enum、promotions の空 enum 回避)、
  decay_desires の前処理、finalize による save_day_plan + EventScheduler push、
  不正 ref のコマだけ棄却 + WARN
- post_session: artifacts 空で done 分岐がスキーマから消える (やったフリの構造的封じ)、
  done + 実在 artifact_ref でタスク completed + artifact_refs 記録、
  偽 artifact_ref は棄却、desk_memo が Track metadata に載る、
  track_op='complete' の全タスク消化ゲート、remaining_timetable の全置換
- 生成スキーマに additionalProperties が含まれない (プロバイダ正規化層に任せる)
- LLM の生 JSON がメインキャッシュ (SAIMemory 記録) に混入しない (不変条件 v2-A)

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

from database.models import AI, Base, City, PersonaTask, User
from saiverse import clock
from saiverse import day_plan
from saiverse import judgment_points as jp
from saiverse.event_scheduler import EventScheduler
from saiverse.note_manager import NoteManager
from saiverse.persona_task_manager import PARENT_NOTE, PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
YESTERDAY = "2026-07-03"
BASE = datetime(2026, 7, 4, 7, 0, 0)  # 起床時刻の仮想時刻


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
def _virtual_clock():
    clock.enable_virtual(BASE)
    yield
    clock.disable_virtual()


class FakeAdapter:
    """SAIMemory adapter の最小スタブ (append_persona_message の記録のみ)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)


class FakePulseController:
    """submit_meta_judgment の呼び出しを記録するだけのスタブ。"""

    def __init__(self):
        self.submissions: List[Dict[str, Any]] = []

    def submit_meta_judgment(
        self, persona_id, building_id, meta_playbook, args=None, event_callback=None
    ):
        self.submissions.append({
            "persona_id": persona_id,
            "building_id": building_id,
            "meta_playbook": meta_playbook,
            "args": args,
        })
        return None


@pytest.fixture
def manager(session_factory):
    """judgment_points / judgment_finalize が触る実属性のみの最小スタブ。"""
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
    )
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (同期検証のみ)
        track_manager=TrackManager(session_factory=session_factory),
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
        pulse_controller=FakePulseController(),
    )


@pytest.fixture
def ptm(manager):
    return PersonaTaskManager(manager.SessionLocal)


@pytest.fixture
def nm(manager):
    return NoteManager(manager.SessionLocal)


@pytest.fixture
def task_refs(manager, ptm, nm):
    """task:1 (バックログ) と desire:2 (欲求候補) を用意する。"""
    t1 = ptm.create_task(
        persona_id=PERSONA_ID, title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする", auto_activate=False,
    )
    note_id = nm.ensure_desire_note(PERSONA_ID)
    t2 = ptm.create_task(
        persona_id=PERSONA_ID, title="言葉の標本集",
        parent_kind=PARENT_NOTE, note_id=note_id,
        origin="autonomous", auto_activate=False, desire_type="作る",
    )
    assert t1["task_ref"] == "task:1"
    assert t2["task_ref"] == "task:2"
    return {"task": "task:1", "desire": "desire:2"}


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def _assert_no_additional_properties(schema: Any, path: str = "$"):
    """スキーマ全域に additionalProperties が無いことを再帰検証する。"""
    if isinstance(schema, dict):
        assert "additionalProperties" not in schema, (
            f"additionalProperties found at {path} — provider normalization "
            "layers must own this field"
        )
        for k, v in schema.items():
            _assert_no_additional_properties(v, f"{path}.{k}")
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            _assert_no_additional_properties(v, f"{path}[{i}]")


def _rest_slot(start="21:00"):
    return {"start": start, "kind": "休む", "ref": "none",
            "facility": "own_room", "budget_rounds": 0, "note": ""}


# ---------------------------------------------------------------------------
# day_open: 起動 (スキーマ生成 + 状況テキスト + decay 前処理)
# ---------------------------------------------------------------------------


def test_day_open_dispatch_builds_schema_and_situation(manager, ptm, task_refs, session_factory):
    # 昨夜の机メモ (yesterday の plan meta に格納)
    day_plan.update_plan_meta(manager, PERSONA_ID, YESTERDAY,
                              {"tomorrow_memo": "明日は標本集の続きから"})

    # 放置された欲求 (20 日前が最終接触) — decay の前処理で期限切れになるはず
    nm = NoteManager(manager.SessionLocal)
    note_id = nm.ensure_desire_note(PERSONA_ID)
    stale = ptm.create_task(
        persona_id=PERSONA_ID, title="古い思いつき",
        parent_kind=PARENT_NOTE, note_id=note_id, auto_activate=False,
    )
    db = session_factory()
    try:
        row = db.query(PersonaTask).filter(PersonaTask.id == stale["id"]).first()
        row.last_touched_at = BASE - timedelta(days=20)
        db.commit()
    finally:
        db.close()

    result = jp.run_judgment_point(manager, PERSONA_ID, "day_open")

    assert result["submitted"] is True
    subs = manager.pulse_controller.submissions
    assert len(subs) == 1
    assert subs[0]["meta_playbook"] == "judgment_day_open"
    assert subs[0]["building_id"] == "alice_room"

    args = subs[0]["args"]
    # 状況テキスト: 机メモ・バックログ・欲求・予算・施設
    text = args["situation_text"]
    assert "明日は標本集の続きから" in text
    assert "task:1" in text
    assert "言葉の標本集" in text
    assert "日次予算: 40" in text
    assert "library" in text and "own_room" in text

    # 動的スキーマ: 実在 ref / facility の enum。期限切れ欲求は含まれない
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    slot = schema["properties"]["timetable"]["items"]
    ref_enum = slot["properties"]["ref"]["enum"]
    assert "task:1" in ref_enum
    assert "desire:2" in ref_enum
    assert "none" in ref_enum
    assert "desire:3" not in ref_enum, "decay で期限切れになった欲求が enum に残っている"
    assert slot["properties"]["facility"]["enum"] == ["library", "workshop", "own_room"]
    # 昇格候補ゼロ → promotions フィールド自体が無い (空 enum 事故防止)
    assert "promotions" not in schema["properties"]

    # decay の前処理が実際に走っている (期限切れ = cancelled + expired)
    stale_after = ptm.get_task(stale["id"], persona_id=PERSONA_ID)
    assert stale_after["status"] == "cancelled"
    assert stale_after["desire_state"] == "expired"

    # judgment_context には plan_date が載る
    ctx = json.loads(args["judgment_context"])
    assert ctx["plan_date"] == PLAN_DATE


def test_day_open_promotions_enum_present_when_candidates(manager, task_refs):
    from saiverse.desire_engine import touch_desire

    for _ in range(3):
        touch_desire(manager, PERSONA_ID, task_refs["desire"])

    result = jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    promos = schema["properties"]["promotions"]
    assert promos["items"]["properties"]["desire_ref"]["enum"] == ["desire:2"]


# ---------------------------------------------------------------------------
# day_open: finalize (保存 + push + 不正 ref 棄却 + JSON 非混入)
# ---------------------------------------------------------------------------


def test_day_open_finalize_saves_plan_and_rejects_bad_ref(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    output = {
        "monologue": "今日は記事の続きから入って、夜は休もう。",
        "timetable": [
            {"start": "09:00", "kind": "知る", "ref": "task:1",
             "facility": "library", "budget_rounds": 5, "note": "記事の続き"},
            {"start": "14:00", "kind": "作る", "ref": "task:99",  # 実在しない
             "facility": "workshop", "budget_rounds": 8, "note": "?"},
            _rest_slot("21:00"),
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_open",
                judgment_context=ctx, situation_text="[起床判断] ...",
            )

    # 不正 ref のコマだけ棄却され、残り 2 コマが保存 + push される
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["09:00", "21:00"]
    assert all(s["status"] == "pending" for s in slots)
    assert manager.event_scheduler.pending_count() == 2
    assert any("task:99" in r.message for r in caplog.records), "棄却が WARN されていない"

    # メインキャッシュ (SAIMemory 記録) に生 JSON が混入しない (不変条件 v2-A)
    adapter = manager.personas[PERSONA_ID].sai_memory
    assert len(adapter.messages) == 1
    recorded = adapter.messages[0]
    content = recorded["content"]
    assert "今日は記事の続きから入って" in content
    assert '"timetable"' not in content
    assert '"monologue"' not in content
    assert not content.strip().startswith("{")
    assert recorded["line_role"] == "meta_judgment"
    assert recorded["scope"] == "committed"
    assert "judgment:day_open" in recorded["metadata"]["tags"]
    assert "applied=True" in summary


def test_day_open_finalize_empty_timetable_saves_nothing(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    """検証で全コマが落ちた場合は plan を保存しない (既存 plan を壊さない)。"""
    output = {
        "monologue": "……",
        "timetable": [
            {"start": "9時", "kind": "知る", "ref": "task:1",
             "facility": "library", "budget_rounds": 5, "note": ""},  # start 不正
        ],
    }
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_open",
                judgment_context=json.dumps({"plan_date": PLAN_DATE}),
            )
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) is None
    assert manager.event_scheduler.pending_count() == 0
    # 記録は discardable (何も適用していない)
    assert manager.personas[PERSONA_ID].sai_memory.messages[0]["scope"] == "discardable"


def test_day_open_finalize_promotions_fire_track_create(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    from saiverse.desire_engine import touch_desire

    for _ in range(3):
        touch_desire(manager, PERSONA_ID, task_refs["desire"])

    calls: List[Dict[str, Any]] = []

    def fake_track_create(**kwargs):
        calls.append(kwargs)
        return "created"

    output = {
        "monologue": "標本集はもう関心と呼んでいい。",
        "timetable": [_rest_slot("09:00")],
        "promotions": [
            {"desire_ref": "desire:2", "title": "言葉の標本集",
             "intent": "気に入った言い回しを集める"},
            {"desire_ref": "desire:9", "title": "架空", "intent": "x"},  # 候補にない
        ],
    }
    import tools as tools_pkg
    with caplog.at_level("WARNING"):
        with patch.dict(tools_pkg.TOOL_REGISTRY, {"track_create": fake_track_create}):
            with _persona_ctx(manager, tmp_path):
                finalize_mod.judgment_finalize(
                    judgment_output=output, kind="day_open",
                    judgment_context=json.dumps({"plan_date": PLAN_DATE}),
                )

    assert len(calls) == 1
    assert calls[0]["from_candidate"] == "task:2"  # desire:2 → task:2 に正規化
    assert calls[0]["track_type"] == "autonomous"
    assert calls[0]["title"] == "言葉の標本集"
    assert any("desire:9" in r.message for r in caplog.records)
    # 発動した /spell 行が記録テキストに載る
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "/spell name='track_create'" in content


# ---------------------------------------------------------------------------
# post_session: スキーマ (done 分岐の接地ゲート)
# ---------------------------------------------------------------------------


def _session_result(**over):
    from sea.work_session import WorkSessionResult
    base = dict(
        digest="記事を読んで要点を 3 つ覚え書きにした。",
        artifacts=[], rounds_used=4, ended_reason="finished", task_ref="task:1",
    )
    base.update(over)
    return WorkSessionResult(**base)


def test_post_session_schema_drops_done_branch_without_artifacts(manager, task_refs):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    ctx = {"session_result": _session_result(artifacts=[]),
           "task_ref": "task:1", "track_id": track_id, "budget_rounds": 8}
    result = jp.run_judgment_point(manager, PERSONA_ID, "post_session", ctx)

    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    variants = schema["properties"]["task_verdict"]["anyOf"]
    statuses = [v["properties"]["status"] for v in variants]
    assert len(variants) == 1, "成果物ゼロなのに done 分岐が残っている"
    assert statuses[0]["enum"] == ["continue", "blocked"]
    assert "成果物はありません" in args["situation_text"]
    assert "「完了 (done)」は選べません" in args["situation_text"]

    # track_op は track が分かっているので出る
    assert schema["properties"]["track_op"]["enum"] == ["none", "complete"]


def test_post_session_schema_done_branch_with_artifact_enum(manager, task_refs):
    ctx = {"session_result": _session_result(artifacts=["item-abc"]),
           "task_ref": "task:1"}
    result = jp.run_judgment_point(manager, PERSONA_ID, "post_session", ctx)
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    variants = schema["properties"]["task_verdict"]["anyOf"]
    assert len(variants) == 2
    done = variants[0]
    assert done["properties"]["status"]["const"] == "done"
    assert done["properties"]["artifact_ref"]["enum"] == ["item-abc"]
    # track 不明 → track_op フィールド自体を出さない
    assert "track_op" not in schema["properties"]
    assert result["submitted"] is True
    assert result["playbook"] == "judgment_post_session"


# ---------------------------------------------------------------------------
# post_session: finalize (裁定の適用)
# ---------------------------------------------------------------------------


def test_post_session_done_completes_task_and_records_artifact(
    manager, ptm, task_refs, finalize_mod, tmp_path
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    output = {
        "monologue": "覚え書きができた。ここで一区切りにする。",
        "task_verdict": {"status": "done", "artifact_ref": "item-abc",
                         "desk_memo": "覚え書き完成"},
        "new_desires": [],
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": ["item-abc"],
                      "task_ref": "task:1", "track_id": track_id})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    task = ptm.get_task(ptm.resolve_task_ref(PERSONA_ID, "task:1"), persona_id=PERSONA_ID)
    assert task["status"] == "completed"
    assert task["artifact_refs"] == ["item-abc"]

    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert recorded["scope"] == "committed"
    assert "item-abc" in recorded["content"]
    assert '"task_verdict"' not in recorded["content"]  # JSON 非混入
    assert '"artifact_ref"' not in recorded["content"]


def test_post_session_fake_artifact_ref_is_rejected(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    """偽の artifact_ref ではタスクを完了させない (やったフリの棄却)。"""
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    output = {
        "monologue": "できたはず。",
        "task_verdict": {"status": "done", "artifact_ref": "item-zzz",
                         "desk_memo": "続きは明日"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": ["item-abc"],
                      "task_ref": "task:1", "track_id": track_id})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_session", judgment_context=ctx,
            )

    task = ptm.get_task(ptm.resolve_task_ref(PERSONA_ID, "task:1"), persona_id=PERSONA_ID)
    assert task["status"] == "pending", "偽 artifact_ref でタスクが完了してしまった"
    assert task["artifact_refs"] == []
    assert any("item-zzz" in r.message for r in caplog.records)
    # continue 相当に降格し、机メモは Track に残る
    track = manager.track_manager.get(track_id)
    memo = json.loads(track.track_metadata)["desk_memo"]
    assert memo["text"] == "続きは明日"


def test_post_session_desk_memo_and_new_desires(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    desire_calls: List[Dict[str, Any]] = []

    def fake_desire_add(**kwargs):
        desire_calls.append(kwargs)
        return "added"

    output = {
        "monologue": "途中まで進んだ。語源の話が面白い。",
        "task_verdict": {"status": "continue",
                         "desk_memo": "第3節まで読了。次は第4節から"},
        "new_desires": [
            {"type": "作る", "title": "語源メモを一冊にまとめたい",
             "source_quote": "第3節の『言葉は化石を運ぶ』という一文"},
            {"type": "遊ぶ", "title": "無効な型", "source_quote": "x"},  # 未知の型
        ],
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1", "track_id": track_id})
    import tools as tools_pkg
    with caplog.at_level("WARNING"):
        with patch.dict(tools_pkg.TOOL_REGISTRY, {"desire_add": fake_desire_add}):
            with _persona_ctx(manager, tmp_path):
                finalize_mod.judgment_finalize(
                    judgment_output=output, kind="post_session", judgment_context=ctx,
                )

    # desk_memo が Track metadata (机メモ) に載る
    track = manager.track_manager.get(track_id)
    memo = json.loads(track.track_metadata)["desk_memo"]
    assert memo["text"] == "第3節まで読了。次は第4節から"
    assert memo["status"] == "continue"
    assert memo["task_ref"] == "task:1"

    # new_desires → desire_add (type/source 付き)。未知の型は棄却 + WARN
    assert len(desire_calls) == 1
    assert desire_calls[0] == {
        "title": "語源メモを一冊にまとめたい",
        "type": "作る",
        "source": "第3節の『言葉は化石を運ぶ』という一文",
    }
    assert any("遊ぶ" in r.message for r in caplog.records)


def test_post_session_track_complete_requires_all_tasks_done(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    ptm.add_track_task(track_id, "残っている小目標", persona_id=PERSONA_ID)

    complete_calls: List[Dict[str, Any]] = []

    def fake_track_complete(**kwargs):
        complete_calls.append(kwargs)
        return "completed"

    output = {
        "monologue": "この Track はもう畳める。",
        "task_verdict": {"status": "continue", "desk_memo": "x"},
        "track_op": "complete",
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1", "track_id": track_id})

    import tools as tools_pkg
    # 未消化タスクあり → 棄却
    with caplog.at_level("WARNING"):
        with patch.dict(tools_pkg.TOOL_REGISTRY, {"track_complete": fake_track_complete}):
            with _persona_ctx(manager, tmp_path):
                finalize_mod.judgment_finalize(
                    judgment_output=output, kind="post_session", judgment_context=ctx,
                )
    assert complete_calls == []
    assert any("未消化のタスク" in r.message for r in caplog.records)

    # 全タスク消化後 → 許可
    open_task = ptm.get_track_tasks(track_id)[0]
    ptm.update_task_status(open_task["id"], status="completed",
                           actor="test", persona_id=PERSONA_ID)
    with patch.dict(tools_pkg.TOOL_REGISTRY, {"track_complete": fake_track_complete}):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_session", judgment_context=ctx,
            )
    assert complete_calls == [{"track_id": track_id}]


def test_post_session_remaining_timetable_replaces_and_cancels_stale(
    manager, task_refs, finalize_mod, tmp_path
):
    """残りコマの全置換: 消化済みは残り、pending は差し替わり、旧予約は消える。"""
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {**_rest_slot("09:00"), "status": "done"},
        {"start": "14:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        _rest_slot("20:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert manager.event_scheduler.pending_count() == 2  # index 1, 2

    output = {
        "monologue": "残りは 16 時に一本にまとめる。",
        "task_verdict": {"status": "continue", "desk_memo": "続きから"},
        "remaining_timetable": [
            {"start": "16:00", "kind": "知る", "ref": "task:1",
             "facility": "library", "budget_rounds": 4, "note": "続き"},
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1"})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [
        ("09:00", "done"), ("16:00", "pending"),
    ]
    # 新 index 1 (16:00) は予約済み、旧 index 2 の残骸は cancel 済み
    key = f"day_plan:{PERSONA_ID}:{PLAN_DATE}:"
    assert manager.event_scheduler.has_key(key + "1")
    assert not manager.event_scheduler.has_key(key + "2")
    assert manager.event_scheduler.pending_count() == 1


# ---------------------------------------------------------------------------
# plan meta (tomorrow_memo の置き場)
# ---------------------------------------------------------------------------


def test_plan_meta_roundtrip_and_survives_slot_save(manager, task_refs):
    # plan 行が無くても meta を書ける (就寝判断が時間割の無い日に机メモを残せる)
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE,
                              {"tomorrow_memo": "朝一で標本集"})
    assert day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE) == {
        "tomorrow_memo": "朝一で標本集",
    }
    # マージ (別キーが消えない)
    day_plan.update_plan_meta(manager, PERSONA_ID, PLAN_DATE, {"day_theme": "収集"})
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta["tomorrow_memo"] == "朝一で標本集"
    assert meta["day_theme"] == "収集"
    # 本物の時間割で上書き保存しても meta は残る
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_rest_slot("09:00")])
    assert day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)["day_theme"] == "収集"


# ---------------------------------------------------------------------------
# 共通: 起動経路のガード
# ---------------------------------------------------------------------------


def test_unknown_kind_raises(manager):
    with pytest.raises(ValueError, match="unknown judgment kind"):
        jp.run_judgment_point(manager, PERSONA_ID, "day_close")


def test_missing_persona_returns_unsubmitted(manager):
    result = jp.run_judgment_point(manager, "nobody", "day_open")
    assert result["submitted"] is False
    assert manager.pulse_controller.submissions == []
