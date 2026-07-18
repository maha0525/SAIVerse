"""判断点 5 種のテスト (judgment_points.md §4〜§8)。

一時 DB (in-memory SQLite) + mock LLM 出力 (構造化出力 dict) + 仮想クロックで検証:

- day_open: 動的スキーマ (ref/facility enum、promotions の空 enum 回避)、
  decay_desires の前処理、finalize による save_day_plan + EventScheduler push、
  不正 ref のコマだけ棄却 + WARN
- post_conversation: picked_tasks (track_ref enum + origin_quote 接地)、
  resume_session の動的挿入 (中断中セッションがあるときのみ)、収穫ゼロ正常、
  remaining_timetable 全置換、resume_now の即時コマ挿入 / drop の作業メモ片づけ
- post_session: artifacts 空で done 分岐がスキーマから消える (やったフリの構造的封じ)、
  done + 実在 artifact_ref でタスク completed + artifact_refs 記録、
  偽 artifact_ref は棄却、desk_memo が Track metadata に載る、
  track_op='complete' の全タスク消化ゲート、remaining_timetable の全置換
- on_event: reaction の 4 分岐、alert での engage_now 縮退 (スキーマ + finalize
  二重ガード)、insert_slot の時刻整合検証、note_only の plan meta 覚え書き
- day_close: 今日触れた欲求のみの desire_reviews enum、tomorrow_memo /
  day_digest が翌朝 day_open の状況テキストに現れる (連結)、
  apply_desire_reviews の適用
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
from saiverse.persona_task_manager import STAGE_CANDIDATE, PersonaTaskManager
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
def task_refs(manager, ptm):
    """task:1 (バックログ) と desire:2 (欲求候補) を用意する。"""
    t1 = ptm.create_task(
        persona_id=PERSONA_ID, title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする", auto_activate=False,
    )
    t2 = ptm.create_task(
        persona_id=PERSONA_ID, title="言葉の標本集",
        stage=STAGE_CANDIDATE, desire_source="test-seed",
        origin="autonomous", auto_activate=False, desire_type="作る",
    )
    assert t1["task_ref"] == "task:1"
    assert t2["task_ref"] == "task:2"
    return {"task": "task:1", "desire": "task:2"}


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
    # 昨夜の自分からのメモ (yesterday の plan meta に格納)
    day_plan.update_plan_meta(manager, PERSONA_ID, YESTERDAY,
                              {"tomorrow_memo": "明日は標本集の続きから"})

    # 放置された欲求 (20 日前が最終接触) — decay の前処理で期限切れになるはず
    stale = ptm.create_task(
        persona_id=PERSONA_ID, title="古い思いつき",
        stage=STAGE_CANDIDATE, desire_source="test-seed", auto_activate=False,
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
    # 状況テキスト: 昨日の自分からのメモ・バックログ・欲求・予算・施設
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
    assert "task:2" in ref_enum
    assert "none" in ref_enum
    assert "task:3" not in ref_enum, "decay で期限切れになった欲求が enum に残っている"
    assert slot["properties"]["facility"]["enum"] == ["library", "workshop", "own_room"]
    # 表題 (title): 各コマにペルソナ自身が付ける (一日新聞の主役列)
    assert "title" in slot["properties"]
    assert "title" in slot["required"]
    assert "短い表題" in text  # プロンプト側の指示
    # 昇格候補ゼロ → promotions フィールド自体が無い (空 enum 事故防止)
    assert "promotions" not in schema["properties"]

    # decay の前処理が実際に走っている (期限切れ = cancelled + expired)
    stale_after = ptm.get_task(stale["id"], persona_id=PERSONA_ID)
    assert stale_after["status"] == "cancelled"
    assert stale_after["desire_state"] == "expired"

    # judgment_context には plan_date が載る
    ctx = json.loads(args["judgment_context"])
    assert ctx["plan_date"] == PLAN_DATE


def test_day_open_desire_candidate_lines_match_ref_enum(manager, task_refs):
    """やりたいこと候補の各行の ref 表記が、コマ ref の enum の要素と一致する。

    回帰防止 (2026-07-05 実 LLM 一日シム): プロンプトが欲求を task:2 と生表示し、
    enum は desire:2 だったため、ペルソナの書いた task:2 が制約デコードで
    無関係な task:1 に滑った。プロンプト表示と enum の整合そのものを固定する。
    """
    jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    args = manager.pulse_controller.submissions[0]["args"]
    text = args["situation_text"]
    slot = args["response_schema"]["properties"]["timetable"]["items"]
    ref_enum = set(slot["properties"]["ref"]["enum"])

    lines = text.splitlines()
    start = lines.index("やりたいこと候補:")
    candidate_lines = []
    for line in lines[start + 1:]:
        if not line.startswith("- "):
            break
        candidate_lines.append(line)
    assert candidate_lines, "やりたいこと候補が 1 行も無い (フィクスチャの前提が崩れた)"
    for line in candidate_lines:
        ref = line[2:].split(" ", 1)[0]
        assert ref in ref_enum, f"表示 ref {ref!r} が enum {sorted(ref_enum)} に無い: {line}"


def test_day_open_promotions_enum_present_when_candidates(manager, task_refs):
    from saiverse.desire_engine import touch_desire

    for _ in range(3):
        touch_desire(manager, PERSONA_ID, task_refs["desire"])

    result = jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    promos = schema["properties"]["promotions"]
    assert promos["items"]["properties"]["desire_ref"]["enum"] == ["task:2"]


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


def test_day_open_finalize_rounds_and_excludes_slots_with_life_declared(
    manager, task_refs, finalize_mod, tmp_path,
):
    """life.md §3 追補 (2026-07-14 実機の破綻の回帰): ライフが宣言されている日、
    過去開始のコマは現在時刻へ丸め、丸めても活動時間の外 (就寝後) のコマだけを
    除外する部分救済。旧挙動 (全体 raise) は 3 分のズレで時間割を全滅させた。
    調整が起きたことは judgment_finalize の適用エコー (SAIMemory 記録) に
    日常語で明示される。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    clock.enable_virtual(BASE + timedelta(hours=1))  # 08:00 (起床から少し遅れて編成)

    output = {
        "monologue": "今日は記事の続きから入って、夜は休もう。",
        "timetable": [
            {"start": "07:30", "kind": "知る", "ref": task_refs["task"],
             "facility": "library", "budget_rounds": 5, "note": "", "title": "記事の続き"},
            _rest_slot("12:00"),
            _rest_slot("23:00"),  # ライフ終了 (22:00) より後 — 丸めようが無い
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["08:00", "12:00"]

    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "（1番目の予定は開始時刻を08:00に調整しました）" in content
    assert "（3番目の予定は活動時間の外のため外しました）" in content


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
            {"desire_ref": "task:2", "title": "言葉の標本集",
             "intent": "気に入った言い回しを集める"},
            {"desire_ref": "task:9", "title": "架空", "intent": "x"},  # 候補にない
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
    assert any("task:9" in r.message for r in caplog.records)
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
    # continue 相当に降格し、作業メモは Track に残る
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

    def fake_purpose_seed(**kwargs):
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
        with patch.dict(tools_pkg.TOOL_REGISTRY, {"purpose_seed": fake_purpose_seed}):
            with _persona_ctx(manager, tmp_path):
                finalize_mod.judgment_finalize(
                    judgment_output=output, kind="post_session", judgment_context=ctx,
                )

    # desk_memo (作業メモ) が Track metadata に載る
    track = manager.track_manager.get(track_id)
    memo = json.loads(track.track_metadata)["desk_memo"]
    assert memo["text"] == "第3節まで読了。次は第4節から"
    assert memo["status"] == "continue"
    assert memo["task_ref"] == "task:1"

    # new_desires → purpose_seed (type/source 付き; P2c-1 で desire_add から切替)。
    # 未知の型は棄却 + WARN
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


def test_post_session_remaining_timetable_restart_at_consumed_time_applies(
    manager, task_refs, finalize_mod, tmp_path
):
    """正当な組み替え: 消化済みコマと同時刻から始まる置換が finalize 経由で通る。

    2026-07-05 実 LLM シム 3回目の回帰 (sanitize → replace_remaining_slots の
    通し): 13:30 コマ消化直後に「13:30 を ref を直して置き直す」置換が
    『昇順でない』で全却下 → ペルソナは組み替えたつもりのまま、が直っている。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "13:30", "kind": "作る", "ref": "task:1",
         "facility": "workshop", "budget_rounds": 6, "note": "済んだコマ",
         "status": "done"},
        _rest_slot("17:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    output = {
        "monologue": "13:30 のコマは対象を直してやり直す。",
        "remaining_timetable": [
            {"start": "13:30", "kind": "作る", "ref": "task:2",
             "facility": "workshop", "budget_rounds": 4, "note": "対象を直した"},
            _rest_slot("17:00"),
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": []})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    assert "applied=True" in summary
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"], s["ref"]) for s in slots] == [
        ("13:30", "done", "task:1"),
        ("13:30", "pending", "task:2"),
        ("17:00", "pending", "none"),
    ]
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "残りの時間割を組み替えた" in content
    # 組み替え後の各コマが対象/場所付きで載る (番号だけの「N コマ」で終わらせない)。
    # task:2 は独白に出てこないので、コマ明細が追記されたことの判別材料になる。
    assert "task:2" in content
    assert "@workshop" in content


def test_post_session_remaining_timetable_rounds_and_excludes_with_life_declared(
    manager, task_refs, finalize_mod, tmp_path,
):
    """life.md §3 追補: 残りコマの全置換 (post_session 経由) でも、ライフが
    宣言されている日は丸め・部分救済が効き、調整はエコーに明示される。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {**_rest_slot("09:00"), "status": "done"},
        _rest_slot("20:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    clock.enable_virtual(BASE + timedelta(hours=7))  # 14:00
    output = {
        "monologue": "夕方は早めに休もう。",
        "remaining_timetable": [
            {"start": "13:00", "kind": "作る", "ref": "task:1",
             "facility": "workshop", "budget_rounds": 4, "note": "続き"},
            _rest_slot("18:00"),
            _rest_slot("23:00"),  # ライフ終了 (22:00) より後 — 丸めようが無い
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": []})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [
        ("09:00", "done"), ("14:00", "pending"), ("18:00", "pending"),
    ]
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "（1番目の予定は開始時刻を14:00に調整しました）" in content
    assert "（3番目の予定は活動時間の外のため外しました）" in content
    assert "うち 1 コマは無効または活動時間の外のため除外されました" in content


def test_post_session_remaining_timetable_rejection_reaches_persona(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    """置換が全滅した却下は warnings (ログ) だけでなくペルソナの文脈にも返る。

    黙って捨てるとペルソナは「組み替えた」つもりのまま一日を続ける
    (接地原則違反。2026-07-05 実 LLM シム 3回目 異常②の回帰)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_rest_slot("17:00")])
    output = {
        "monologue": "残りを組み替えるつもり。",
        "remaining_timetable": [
            {"start": "15:00", "kind": "作る", "ref": "task:99",  # 実在しない
             "facility": "workshop", "budget_rounds": 4, "note": "?"},
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": []})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_session", judgment_context=ctx,
            )

    # 時間割は不変
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [("17:00", "pending")]
    # 却下がペルソナに見える記録 (SAIMemory content) に載る
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "時間割の変更は適用されませんでした" in content
    assert any("task:99" in r.message for r in caplog.records)


def test_post_session_empty_remaining_timetable_is_silent_no_change(
    manager, task_refs, finalize_mod, tmp_path
):
    """remaining_timetable=[] は null と同じ「変更なし」— 却下エコーを書かない。

    空の時間割は不変条件 (最低 1 コマ) で保存できず、[] が有効な変更要求で
    ありうる余地がない。実データでは [] は「残りコマが現実に無い時点の判断」
    の事実記述として出る (2026-07-18 観測: 却下 6 件全件) — 却下エコーで
    咎めるとペルソナの記憶に無意味な失敗文が積もるため、黙って変更なし扱い。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [_rest_slot("17:00")])
    output = {
        "monologue": "残りの予定はこのままでいい。",
        "remaining_timetable": [],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": []})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    # 時間割は不変
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [("17:00", "pending")]
    # 却下エコーはペルソナの記録に載らない
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "時間割の変更は適用されませんでした" not in content
    assert "空の時間割" not in content


# ---------------------------------------------------------------------------
# post_session: 終了済みタスクの再裁定封じ (シム 3回目 異常③)
# ---------------------------------------------------------------------------


def test_post_session_schema_omits_verdict_for_completed_task(
    manager, ptm, task_refs
):
    """終了済みタスクが対象のセッションでは task_verdict 欄自体を出さない。

    completed 済みタスクに done 裁定 → artifact_refs 多重追記、を構造的に
    封じる (2026-07-05 実 LLM シム 3回目 異常③)。
    """
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.update_task_status(task_id, status="completed",
                           actor="test", persona_id=PERSONA_ID)

    schema = jp.build_post_session_schema(
        manager, PERSONA_ID, ["item-abc"], "task:1", None,
    )
    assert "task_verdict" not in schema["properties"]
    assert "task_verdict" not in schema["required"]

    # 生きているタスクなら従来どおり欄が出る (退行防止)
    schema_alive = jp.build_post_session_schema(
        manager, PERSONA_ID, ["item-abc"], "task:2", None,
    )
    assert "task_verdict" in schema_alive["properties"]


def test_post_session_re_done_on_completed_task_is_rejected(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    """finalize 側の二重ガード: 終了済みタスクへの done 裁定は適用しない。

    artifact_refs への多重追記も、終了済みタスクへの desk_memo (偽の
    「中断中の作業」化) もしない。
    """
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.update_task_status(task_id, status="completed",
                           actor="test", persona_id=PERSONA_ID)
    ptm.append_artifact_ref(task_id, "item-old",
                            persona_id=PERSONA_ID, actor="test")

    output = {
        "monologue": "また完了にしておこう。",
        "task_verdict": {"status": "done", "artifact_ref": "item-new",
                         "desk_memo": "二度目の完了"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": ["item-new"],
                      "task_ref": "task:1", "track_id": track_id})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_session", judgment_context=ctx,
            )

    task = ptm.get_task(task_id, persona_id=PERSONA_ID)
    assert task["status"] == "completed"
    assert task["artifact_refs"] == ["item-old"], "終了済みタスクに成果物が多重追記された"
    assert any("既に completed" in r.message for r in caplog.records)
    # desk_memo も書かれない (終了済みタスクを「中断中」に見せない)
    track = manager.track_manager.get(track_id)
    assert not track.track_metadata or "desk_memo" not in json.loads(track.track_metadata)
    assert "applied=False" in summary


def test_sanitize_timetable_rejects_completed_task_ref(manager, ptm, task_refs):
    """終了済みタスクを指すコマは棄却される (新しい時間割が完了済みを指せない)。"""
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.update_task_status(task_id, status="completed",
                           actor="test", persona_id=PERSONA_ID)

    slots, warnings = jp.sanitize_timetable(manager, PERSONA_ID, [
        {"start": "10:00", "kind": "作る", "ref": "task:1",
         "facility": "workshop", "budget_rounds": 4, "note": "完了済みを指す"},
        {"start": "12:00", "kind": "作る", "ref": "task:2",
         "facility": "workshop", "budget_rounds": 4, "note": "生きている欲求"},
    ])
    assert [s["ref"] for s in slots] == ["task:2"]
    assert any("completed" in w for w in warnings)


# ---------------------------------------------------------------------------
# plan meta (tomorrow_memo の置き場)
# ---------------------------------------------------------------------------


def test_plan_meta_roundtrip_and_survives_slot_save(manager, task_refs):
    # plan 行が無くても meta を書ける (就寝判断が時間割の無い日にメモを残せる)
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
        jp.run_judgment_point(manager, PERSONA_ID, "nap_time")


def test_missing_persona_returns_unsubmitted(manager):
    result = jp.run_judgment_point(manager, "nobody", "day_open")
    assert result["submitted"] is False
    assert manager.pulse_controller.submissions == []


# ---------------------------------------------------------------------------
# post_conversation: 起動 (スキーマ + 状況テキスト + resume の動的挿入)
# ---------------------------------------------------------------------------


def test_post_conversation_dispatch_schema_and_situation(manager, task_refs):
    manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
        initial_status="running",
    )

    result = jp.run_judgment_point(manager, PERSONA_ID, "post_conversation")

    assert result["submitted"] is True
    assert result["playbook"] == "judgment_post_conversation"
    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)

    picked = schema["properties"]["picked_tasks"]["items"]
    assert picked["properties"]["track_ref"]["enum"] == ["track:1", "new"]
    assert picked["required"] == ["title", "track_ref", "origin_quote"]
    # 中断中セッションなし → resume_session フィールド自体が無い (空 enum 事故防止)
    assert "resume_session" not in schema["properties"]
    assert set(schema["required"]) == {
        "monologue", "picked_tasks", "new_desires", "remaining_timetable",
    }

    text = args["situation_text"]
    assert "07:00" in text  # 現在時刻
    assert "track:1" in text and "調べ物" in text  # track_ref の選択材料
    assert "task:1" in text  # 既存タスク (重複作成の抑止)
    assert "言葉の標本集" in text  # 既存欲求 (重複作成の抑止)

    ctx = json.loads(args["judgment_context"])
    assert ctx["plan_date"] == PLAN_DATE
    assert ctx["track_refs"] == ["track:1"]
    assert "resume" not in ctx


def test_post_conversation_resume_appears_only_with_interrupted_session(
    manager, task_refs
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    jp.save_desk_memo(manager, track_id, {
        "text": "第3節まで読了。次は第4節から", "status": "continue",
        "task_ref": "task:1", "updated_at": "2026-07-03T22:00:00",
    })

    result = jp.run_judgment_point(manager, PERSONA_ID, "post_conversation")
    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    assert schema["properties"]["resume_session"]["enum"] == [
        "resume_now", "defer_to_slot", "drop",
    ]
    assert "第3節まで読了" in args["situation_text"]

    ctx = json.loads(args["judgment_context"])
    assert ctx["resume"]["track_id"] == track_id
    assert ctx["resume"]["task_ref"] == "task:1"


# ---------------------------------------------------------------------------
# post_conversation: finalize
# ---------------------------------------------------------------------------


def test_post_conversation_finalize_creates_tasks_with_origin_quote(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
        initial_status="running",
    )
    output = {
        "monologue": "約束を忘れないうちに書き留めておく。",
        "picked_tasks": [
            {"title": "蒸留メモを見せる", "track_ref": "track:1",
             "origin_quote": "「できたら見せてほしい」と言われた"},
            {"title": "新しい題材を探す", "track_ref": "new",
             "origin_quote": "「次は何を作るの？」と聞かれた"},
            {"title": "引用なしの思いつき", "track_ref": "track:1",
             "origin_quote": ""},  # 接地なし → 棄却
        ],
        "new_desires": [],
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "track_refs": ["track:1"]})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_conversation",
                judgment_context=ctx, situation_text="[会話終了判断] ...",
            )

    # 既存 Track (t:1) にタスクが 1 件、origin_quote は notes に保存される
    bound = ptm.list_tasks(
        PERSONA_ID, track_id=track_id, parent_kind="track", include_steps=False,
    )
    assert len(bound) == 1
    assert bound[0]["title"] == "蒸留メモを見せる"
    assert bound[0]["notes"] == "「できたら見せてほしい」と言われた"

    # track_ref='new' → 新規 autonomous Track が立ち、タスクが紐づく
    new_tracks = [
        t for t in manager.track_manager.list_for_persona(PERSONA_ID)
        if t.title == "新しい題材を探す"
    ]
    assert len(new_tracks) == 1
    assert new_tracks[0].track_type == "autonomous"
    assert new_tracks[0].status == "unstarted"
    assert "entry_line_role" in json.loads(new_tracks[0].track_metadata)
    new_bound = ptm.list_tasks(
        PERSONA_ID, track_id=new_tracks[0].track_id,
        parent_kind="track", include_steps=False,
    )
    assert len(new_bound) == 1
    assert new_bound[0]["notes"] == "「次は何を作るの？」と聞かれた"

    # origin_quote 無しは棄却 + WARN
    assert any("origin_quote" in r.message for r in caplog.records)
    all_titles = [
        t["title"] for t in ptm.list_tasks(PERSONA_ID, include_steps=False)
    ]
    assert "引用なしの思いつき" not in all_titles

    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert recorded["scope"] == "committed"
    assert '"picked_tasks"' not in recorded["content"]  # JSON 非混入
    assert "judgment:post_conversation" in recorded["metadata"]["tags"]
    assert "applied=True" in summary


def test_post_conversation_zero_harvest_is_normal(
    manager, task_refs, finalize_mod, tmp_path
):
    """収穫ゼロ (両配列空 + 時間割変更なし) は警告なしの正常系。"""
    output = {
        "monologue": "ただの雑談だった。心地よい時間だったな。",
        "picked_tasks": [],
        "new_desires": [],
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "track_refs": []})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )

    assert "warnings=0" in summary
    assert "applied=False" in summary
    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert recorded["scope"] == "discardable"
    assert "ただの雑談だった" in recorded["content"]


def test_post_conversation_remaining_timetable_full_replace(
    manager, task_refs, finalize_mod, tmp_path
):
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "14:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        _rest_slot("20:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert manager.event_scheduler.pending_count() == 2

    output = {
        "monologue": "会話で時間を使ったから、残りは一本にまとめる。",
        "picked_tasks": [],
        "new_desires": [],
        "remaining_timetable": [
            {"start": "16:00", "kind": "知る", "ref": "task:1",
             "facility": "library", "budget_rounds": 4, "note": "続き"},
        ],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "track_refs": []})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [("16:00", "pending")]
    assert manager.event_scheduler.pending_count() == 1


def test_post_conversation_resume_now_inserts_immediate_slot(
    manager, task_refs, finalize_mod, tmp_path
):
    """resume_now = 凍結タスクを参照する作業コマの現在時刻への即時挿入。

    kind / facility / 予算は同じタスクを指していた元コマから引き継ぐ。
    """
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "06:30", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "朝の調べもの",
         "status": "fired"},
        _rest_slot("21:00"),
    ])
    output = {
        "monologue": "会話も終わったし、さっきの続きに戻ろう。",
        "picked_tasks": [],
        "new_desires": [],
        "remaining_timetable": None,
        "resume_session": "resume_now",
    }
    ctx = json.dumps({
        "plan_date": PLAN_DATE, "track_refs": [],
        "resume": {"track_id": track_id, "task_ref": "task:1",
                   "text": "第3節まで読了"},
    })
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [
        ("06:30", "fired"), ("07:00", "pending"), ("21:00", "pending"),
    ]
    inserted = slots[1]
    assert inserted["kind"] == "知る"           # 元コマから引き継ぎ
    assert inserted["ref"] == "task:1"
    assert inserted["facility"] == "library"    # 元コマから引き継ぎ
    assert inserted["budget_rounds"] == 5       # 元コマから引き継ぎ
    key = f"day_plan:{PERSONA_ID}:{PLAN_DATE}:"
    assert manager.event_scheduler.has_key(key + "1")
    assert "applied=True" in summary


def test_post_conversation_resume_drop_clears_desk_memo(
    manager, task_refs, finalize_mod, tmp_path
):
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    jp.save_desk_memo(manager, track_id, {
        "text": "第3節まで", "status": "continue", "task_ref": "task:1",
        "updated_at": "2026-07-03T22:00:00",
    })
    assert jp.find_interrupted_session(manager, PERSONA_ID) is not None

    output = {
        "monologue": "あの作業はもういい。区切りにする。",
        "picked_tasks": [], "new_desires": [], "remaining_timetable": None,
        "resume_session": "drop",
    }
    ctx = json.dumps({
        "plan_date": PLAN_DATE, "track_refs": [],
        "resume": {"track_id": track_id, "task_ref": "task:1", "text": "第3節まで"},
    })
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_conversation", judgment_context=ctx,
        )

    track = manager.track_manager.get(track_id)
    assert "desk_memo" not in json.loads(track.track_metadata)
    assert jp.find_interrupted_session(manager, PERSONA_ID) is None


def test_post_conversation_resume_without_interrupted_session_rejected(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    """ctx に resume が無い (= 中断中セッションが無い) のに選ばれたら棄却。"""
    output = {
        "monologue": "……",
        "picked_tasks": [], "new_desires": [], "remaining_timetable": None,
        "resume_session": "resume_now",
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "track_refs": []})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_conversation",
                judgment_context=ctx,
            )
    assert any("中断中セッションがありません" in r.message for r in caplog.records)
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) is None


# ---------------------------------------------------------------------------
# on_event: 起動 (4 分岐スキーマ / alert 縮退)
# ---------------------------------------------------------------------------


def _reaction_types(schema):
    variants = schema["properties"]["reaction"]["anyOf"]
    return [v["properties"]["type"]["const"] for v in variants]


def test_on_event_dispatch_schema_four_branches(manager, task_refs):
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event",
        {"event_text": "来訪: ボブが訪ねてきた"},
    )
    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    assert _reaction_types(schema) == [
        "engage_now", "insert_slot", "note_only", "ignore",
    ]
    # insert_slot の slot は §3.2 共通定義 (実在 ref / facility の enum)
    slot = schema["properties"]["reaction"]["anyOf"][1]["properties"]["slot"]
    assert "task:1" in slot["properties"]["ref"]["enum"]
    assert slot["properties"]["facility"]["enum"] == ["library", "workshop", "own_room"]

    text = args["situation_text"]
    assert "ボブが訪ねてきた" in text
    assert "07:00" in text
    assert "手すき" in text  # running Track なし → 暮らし

    ctx = json.loads(args["judgment_context"])
    assert ctx["is_alert"] is False
    assert result["playbook"] == "judgment_on_event"


def test_on_event_alert_collapses_to_engage_now(manager, task_refs):
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event",
        {"event_text": "ユーザーからの呼びかけ", "is_alert": True},
    )
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    assert _reaction_types(schema) == ["engage_now"]
    assert "即応が必要" in result["args"]["situation_text"]
    assert json.loads(result["args"]["judgment_context"])["is_alert"] is True


def test_on_event_requires_event_text(manager):
    with pytest.raises(ValueError, match="event_text"):
        jp.run_judgment_point(manager, PERSONA_ID, "on_event")


def test_on_event_situation_shows_running_activity(manager, task_refs):
    manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="標本集の整理",
        initial_status="running",
    )
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    assert "標本集の整理" in result["args"]["situation_text"]


# ---------------------------------------------------------------------------
# on_event: finalize (4 分岐 + alert 二重ガード + 時刻整合)
# ---------------------------------------------------------------------------


def test_on_event_finalize_engage_now(manager, task_refs, finalize_mod, tmp_path):
    output = {
        "monologue": "ボブが来たなら顔を出そう。",
        "reaction": {"type": "engage_now"},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "来訪: ボブ"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    # 呼び出し側が読む判断結果として summary に反映 (応対の起動は配線後続)
    assert "reaction=engage_now" in summary
    assert "applied=True" in summary
    assert manager.personas[PERSONA_ID].sai_memory.messages[0]["scope"] == "committed"


def test_on_event_finalize_insert_slot_and_time_validation(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    # 有効なコマ (現在 07:00 より後) → 挿入 + 予約
    output = {
        "monologue": "今は手が離せないから午後に見よう。",
        "reaction": {"type": "insert_slot", "slot": {
            "start": "15:00", "kind": "知る", "ref": "task:1",
            "facility": "library", "budget_rounds": 4, "note": "届いた資料を読む",
        }},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "資料が届いた"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [("15:00", "pending")]
    assert manager.event_scheduler.pending_count() == 1
    assert "reaction=insert_slot" in summary
    assert "applied=True" in summary

    # 過去時刻 (06:00 < 現在 07:00) → 棄却 + WARN、時間割は不変
    output_past = {
        "monologue": "……",
        "reaction": {"type": "insert_slot", "slot": {
            "start": "06:00", "kind": "知る", "ref": "task:1",
            "facility": "library", "budget_rounds": 4, "note": "x",
        }},
    }
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary2, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output_past, kind="on_event", judgment_context=ctx,
            )
    assert any("現在時刻" in r.message for r in caplog.records)
    assert "applied=False" in summary2
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [(s["start"], s["status"]) for s in slots] == [("15:00", "pending")]


def test_on_event_finalize_note_only_saves_event_memo(
    manager, task_refs, finalize_mod, tmp_path
):
    output = {
        "monologue": "今すぐでなくていい。覚えておこう。",
        "reaction": {"type": "note_only",
                     "memo": "新しい展示が始まったらしい。今度見に行く"},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "掲示板の告知"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    memos = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)["event_memos"]
    assert len(memos) == 1
    assert memos[0]["text"] == "新しい展示が始まったらしい。今度見に行く"
    assert memos[0]["event"] == "掲示板の告知"
    assert "reaction=note_only" in summary
    assert "applied=True" in summary


def test_on_event_finalize_ignore_is_discardable(
    manager, task_refs, finalize_mod, tmp_path
):
    output = {"monologue": "自分には関係のない通知だ。",
              "reaction": {"type": "ignore"}}
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "無関係な通知"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert "reaction=ignore" in summary
    assert "applied=False" in summary
    assert manager.personas[PERSONA_ID].sai_memory.messages[0]["scope"] == "discardable"


def test_on_event_finalize_alert_rejects_non_engage(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    """alert ではスキーマ縮退に加えて finalize でも engage_now 以外を棄却する。"""
    output = {
        "monologue": "後回しにしたい。",
        "reaction": {"type": "insert_slot", "slot": {
            "start": "15:00", "kind": "知る", "ref": "task:1",
            "facility": "library", "budget_rounds": 4, "note": "x",
        }},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": True,
                      "event_text": "ユーザーからの呼びかけ"})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="on_event", judgment_context=ctx,
            )
    assert any("engage_now のみ" in r.message for r in caplog.records)
    assert "applied=False" in summary
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) is None


# ---------------------------------------------------------------------------
# day_close: 起動 (今日触れた欲求のみの enum + 予定 vs 実績)
# ---------------------------------------------------------------------------


def test_day_close_dispatch_schema_and_situation(
    manager, ptm, task_refs, session_factory
):
    from saiverse.desire_engine import touch_desire

    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "記事の続き",
         "status": "done"},
        {**_rest_slot("21:00"), "status": "pending"},
    ])
    # desire:2 は今日 (仮想 2026-07-04) 触れた
    touch_desire(manager, PERSONA_ID, task_refs["desire"])
    # 触れていない欲求 (作成も接触も昨日以前) は enum に出ない
    old = ptm.create_task(
        persona_id=PERSONA_ID, title="以前からの思いつき",
        stage=STAGE_CANDIDATE, desire_source="test-seed", auto_activate=False,
    )
    db = session_factory()
    try:
        row = db.query(PersonaTask).filter(PersonaTask.id == old["id"]).first()
        row.created_at = BASE - timedelta(days=3)
        row.last_touched_at = BASE - timedelta(days=3)
        db.commit()
    finally:
        db.close()

    result = jp.run_judgment_point(manager, PERSONA_ID, "day_close")

    assert result["submitted"] is True
    assert result["playbook"] == "judgment_day_close"
    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    reviews = schema["properties"]["desire_reviews"]["items"]["properties"]
    assert reviews["desire_ref"]["enum"] == ["task:2"], (
        "今日触れていない欲求が enum に混入している"
    )
    assert reviews["verdict"]["enum"] == ["keep", "fading", "fulfilled"]
    assert schema["properties"]["user_report_seeds"]["maxItems"] == 3
    assert schema["required"] == ["monologue", "tomorrow_memo"]

    text = args["situation_text"]
    assert "09:00" in text and "実行済み" in text  # 予定 vs 実績
    assert "21:00" in text and "未実施" in text
    assert "消化 5 / 計画 5" in text  # 予算 (計画値) の対照
    assert "言葉の標本集" in text  # 今日触れた欲求一覧
    assert "以前からの思いつき" not in text

    ctx = json.loads(args["judgment_context"])
    assert ctx["touched_desire_refs"] == ["task:2"]


def test_day_close_situation_shows_system_skip_honestly(manager, task_refs):
    """システム都合で skipped になったコマは「見送り」(本人判断) として提示しない。

    2026-07-05 の実 LLM シム回帰: no-handler スキップが「→ 見送り」と提示され、
    ペルソナが「あえて見送る判断をした」と理由まで捏造した (接地原則違反)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "記事の続き",
         "status": "done"},
        {"start": "21:00", "kind": "自分を更新する", "ref": task_refs["desire"],
         "facility": "own_room", "budget_rounds": 8, "note": "気づきの整理",
         "status": "skipped",
         "skip_reason": day_plan.SKIP_REASON_NO_HANDLER},
    ])

    text = jp.build_day_results_text(manager, PERSONA_ID, PLAN_DATE)
    assert "見送り" not in text
    assert "実行できず（システム側の問題" in text
    # 実行できなかったコマの予算は「消化」に数えない (従来どおり)
    assert "消化 5 / 計画 13" in text


def test_day_close_situation_shows_presence_only_honestly(manager, task_refs):
    """詳細記録の無い done (暮らし/休む スタブ) を「実行済み」として提示しない。

    2026-07-05 の実 LLM シム回帰 (異常 #4): スタブで何も実行していない暮らし
    コマが「→ 実行済み」と提示され、ペルソナが「食事の選定を行った」等、
    していない活動を自分の成果としてふりかえった (soft-confabulation)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "12:00", "kind": "暮らし", "ref": "none",
         "facility": "cafe", "budget_rounds": 0, "note": "昼の時間",
         "status": "done",
         "record_level": day_plan.RECORD_LEVEL_PRESENCE_ONLY},
        # マーカーの無い done (旧データ / セッション系) は従来どおり (後方互換)
        {"start": "14:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "記事の続き",
         "status": "done"},
    ])

    text = jp.build_day_results_text(manager, PERSONA_ID, PLAN_DATE)
    assert "時間を過ごした（詳細な記録なし）" in text
    assert "実行済み" in text  # マーカー無し done の後方互換
    # 暮らしコマの行が「実行済み」になっていないこと
    living_line = next(line for line in text.splitlines() if "12:00" in line)
    assert "実行済み" not in living_line
    assert "時間を過ごした（詳細な記録なし）" in living_line


def test_day_close_schema_omits_desire_reviews_when_none_touched(
    manager, ptm, task_refs, session_factory
):
    # fixture の desire:2 は「今日作成」扱いなので、作成日を過去に倒す
    desire_id = ptm.resolve_task_ref(PERSONA_ID, "task:2")
    db = session_factory()
    try:
        row = db.query(PersonaTask).filter(PersonaTask.id == desire_id).first()
        row.created_at = BASE - timedelta(days=2)
        row.last_touched_at = BASE - timedelta(days=2)
        db.commit()
    finally:
        db.close()

    result = jp.run_judgment_point(manager, PERSONA_ID, "day_close")
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    assert "desire_reviews" not in schema["properties"], (
        "触れた欲求ゼロなのに desire_reviews が要求されている (空 enum 事故)"
    )


# ---------------------------------------------------------------------------
# day_close: finalize (+ 翌朝 day_open との連結)
# ---------------------------------------------------------------------------


def test_day_close_finalize_and_day_open_linkage(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    from saiverse.desire_engine import touch_desire

    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "知る", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "記事の続き",
         "status": "done"},
    ])
    touch_desire(manager, PERSONA_ID, task_refs["desire"])

    output = {
        "monologue": "概ね予定どおりに進んだ一日だった。",
        "tomorrow_memo": "朝一は標本集の整理から始める",
        "day_theme": "収集",
        "desire_reviews": [
            {"desire_ref": "task:2", "verdict": "fulfilled"},
            {"desire_ref": "task:9", "verdict": "keep"},  # 触れていない → 棄却
        ],
        "user_report_seeds": ["蒸留記事の要点を覚え書きにまとめた"],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE,
                      "touched_desire_refs": ["task:2"]})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_close",
                judgment_context=ctx, situation_text="[就寝判断] ...",
            )

    # meta_json に明日へのメモ・テーマ・報告種・実績ダイジェストが保存される
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta["tomorrow_memo"] == "朝一は標本集の整理から始める"
    assert meta["day_theme"] == "収集"
    assert meta["user_report_seeds"] == ["蒸留記事の要点を覚え書きにまとめた"]
    assert "09:00" in meta["day_digest"]  # 決定論構築の実績要約

    # desire_reviews: fulfilled は即消化 (completed)、enum 外は棄却 + WARN
    desire = ptm.get_task(
        ptm.resolve_task_ref(PERSONA_ID, "task:2"), persona_id=PERSONA_ID,
    )
    assert desire["status"] == "completed"
    assert any("task:9" in r.message for r in caplog.records)

    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert recorded["scope"] == "committed"
    assert '"tomorrow_memo"' not in recorded["content"]  # JSON 非混入
    assert "judgment:day_close" in recorded["metadata"]["tags"]
    assert "applied=True" in summary

    # --- 連結: 翌朝の起床判断が昨夜のメモとダイジェストを読む -------------
    clock.advance_to(datetime(2026, 7, 5, 7, 0, 0))
    morning_text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "朝一は標本集の整理から始める" in morning_text  # tomorrow_memo
    assert "今日の時間割（予定 → 実績）" in morning_text   # day_digest
    assert "09:00" in morning_text


def test_day_close_finalize_empty_memo_warns_but_saves_digest(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    output = {"monologue": "……", "tomorrow_memo": ""}
    ctx = json.dumps({"plan_date": PLAN_DATE, "touched_desire_refs": []})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_close", judgment_context=ctx,
            )
    assert any("tomorrow_memo が空" in r.message for r in caplog.records)
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert "tomorrow_memo" not in meta
    assert meta["day_digest"] == "今日の時間割はありませんでした。"
