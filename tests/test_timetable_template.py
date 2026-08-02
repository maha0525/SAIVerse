"""習慣テンプレート (saiverse/timetable_template.py) のテスト — 時間割改修 T2。

対象 (timetable_redesign.md §5.1/§5.2/§11-12):

- テンプレート CRUD と検証 (昇順違反・カタログ外 kind・負予算・不正 ref の拒否)
- 「埋める」化: 穴だけが LLM 値になり、確定フィールドの逸脱がテンプレ値へ
  矯正されること (compose 単体 + judgment_finalize day_open 経由)
- テンプレ未設定のペルソナは従来どおりの全生成 (移行の安全弁)
- 途中起動: 正午起動で午前のテンプレコマが「流れた」(missed_start) 記録 +
  午後から合流し、丸めクランプ (現在時刻への繰り上げ) が起きないこと
- API 3 本 (GET/PUT/DELETE) の正常系 + 検証エラー (422)

harness は tests/test_judgment_points.py と同流儀 (in-memory SQLite +
SimpleNamespace manager + 仮想クロック)。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_manager
from database.models import AI, Base, City, PersonaSchedule, User
from saiverse import clock
from saiverse import day_plan
from saiverse import timetable_template as tt
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
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
    """SAIMemory adapter の最小スタブ (judgment_finalize の記録先)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)
        return f"m{len(self.messages)}"


@pytest.fixture
def manager(session_factory):
    """timetable_template / judgment_finalize が触る実属性のみの最小スタブ。"""
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
    )


@pytest.fixture
def ptm(manager):
    return PersonaTaskManager(manager.SessionLocal)


@pytest.fixture
def task_refs(manager, ptm):
    t1 = ptm.create_task(
        persona_id=PERSONA_ID, title="蒸留記事の続きを読む",
        goal="要点を覚え書きにする", auto_activate=False,
    )
    assert t1["task_ref"] == "task:1"
    return {"task": "task:1"}


@pytest.fixture
def finalize_mod():
    return load_builtin_tool("judgment_finalize")


def _persona_ctx(manager, tmp_path):
    from tools.context import persona_context
    return persona_context(PERSONA_ID, tmp_path, manager=manager)


def _template_slots():
    """確定 3 コマ + 穴入り: 09:00 は全確定、13:00 は全部穴、20:00 は種別のみ確定。"""
    return [
        {"start": "09:00", "kind": "調べる", "title": "朝の調べ物をする",
         "facility": "library", "note": "記事の続き", "budget_rounds": 5,
         "ref": "task:1"},
        {"start": "13:00"},
        {"start": "20:00", "kind": "自室で過ごす"},
    ]


def _llm_slot(start, kind, **over):
    slot = {
        "start": start, "kind": kind, "ref": "none", "facility": "own_room",
        "budget_rounds": 0, "title": f"{kind}をする", "note": "",
    }
    slot.update(over)
    return slot


# ---------------------------------------------------------------------------
# CRUD と検証
# ---------------------------------------------------------------------------


def test_template_crud_roundtrip(manager, task_refs):
    assert tt.get_template(manager, PERSONA_ID) is None
    assert tt.get_active_template(manager, PERSONA_ID) is None

    saved = tt.save_template(manager, PERSONA_ID, _template_slots())
    assert saved["enabled"] is True
    assert [s["start"] for s in saved["slots"]] == ["09:00", "13:00", "20:00"]
    # 穴 (null / 欠落) のフィールドは保存形から省かれる
    assert "kind" not in saved["slots"][1]

    loaded = tt.get_template(manager, PERSONA_ID)
    assert loaded["slots"] == saved["slots"]
    assert tt.get_active_template(manager, PERSONA_ID) is not None

    # 無効化 → get では見えるが起床判断 (active) からは見えない
    tt.save_template(manager, PERSONA_ID, _template_slots(), enabled=False)
    assert tt.get_template(manager, PERSONA_ID)["enabled"] is False
    assert tt.get_active_template(manager, PERSONA_ID) is None

    assert tt.delete_template(manager, PERSONA_ID) is True
    assert tt.get_template(manager, PERSONA_ID) is None
    assert tt.delete_template(manager, PERSONA_ID) is False


@pytest.mark.parametrize("slots, match", [
    ([], "non-empty"),
    ([{"start": "9:00"}], "HH:MM"),
    ([{"start": "10:00"}, {"start": "09:00"}], "ascending"),
    ([{"start": "09:00", "kind": "暮らし"}], "catalog"),   # 封印済み旧 kind
    ([{"start": "09:00", "kind": "作る"}], "catalog"),     # 封印済み旧 kind
    ([{"start": "09:00", "budget_rounds": -1}], "non-negative"),
    ([{"start": "09:00", "budget_rounds": True}], "non-negative"),
    ([{"start": "09:00", "ref": "foo"}], "ref"),
    ([{"start": "09:00", "facility": "  "}], "facility"),
])
def test_save_template_rejects_invalid_slots(manager, slots, match):
    with pytest.raises(ValueError, match=match):
        tt.save_template(manager, PERSONA_ID, slots)


def test_template_ascending_follows_wake_origin(manager, session_factory):
    """深夜跨ぎ (起床 07:00) では 23:00 → 00:30 が正当な流れ順になる。"""
    db = session_factory()
    try:
        db.add(PersonaSchedule(
            PERSONA_ID=PERSONA_ID, SCHEDULE_TYPE="periodic",
            META_PLAYBOOK="judgment_day_open", TIME_OF_DAY="07:00",
            ENABLED=True,
        ))
        db.commit()
    finally:
        db.close()
    saved = tt.save_template(manager, PERSONA_ID, [
        {"start": "23:00"}, {"start": "00:30"},
    ])
    assert [s["start"] for s in saved["slots"]] == ["23:00", "00:30"]
    # 暦順 (起床起点なし) では逆順になる並びが、起床起点で通っている
    with pytest.raises(ValueError, match="ascending"):
        tt.save_template(manager, PERSONA_ID, [
            {"start": "08:00"}, {"start": "07:30"},
        ])


# ---------------------------------------------------------------------------
# 「埋める」化: compose 単体
# ---------------------------------------------------------------------------


def test_compose_fills_holes_and_corrects_fixed_fields(manager, task_refs):
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    llm = [
        # 確定コマからの逸脱: 種別・場所・表題・対象を勝手に変えている
        _llm_slot("09:00", "出かける", title="散歩する"),
        # 穴コマ: この値がそのまま採用されるべき
        _llm_slot("13:00", "随筆を書く", facility="workshop",
                  budget_rounds=4, title="昼の随筆を書く", note="気分で"),
        _llm_slot("20:00", "自室で過ごす", title="静かに過ごす"),
    ]
    ledger, pending, corrections = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template, llm,
    )
    assert ledger == []
    assert len(pending) == 3

    # 確定フィールドはテンプレ値へ矯正 (却下でなくコマは残る)
    first = pending[0]
    assert first["kind"] == "調べる"
    assert first["facility"] == "library"
    assert first["title"] == "朝の調べ物をする"
    assert first["ref"] == "task:1"
    assert first["budget_rounds"] == 5
    assert any("種別" in c and "調べる" in c for c in corrections)
    assert any("表題" in c for c in corrections)

    # 穴のフィールドは LLM の自由
    second = pending[1]
    assert second["kind"] == "随筆を書く"
    assert second["facility"] == "workshop"
    assert second["budget_rounds"] == 4
    assert second["title"] == "昼の随筆を書く"

    # 種別のみ確定のコマ: 種別は維持、表題は LLM 値
    third = pending[2]
    assert third["kind"] == "自室で過ごす"
    assert third["title"] == "静かに過ごす"
    assert third["ref"] == "none"


def test_compose_frame_survives_empty_llm_output(manager, task_refs):
    """LLM 出力が全滅しても枠は守られる (穴は既定値で埋まる)。"""
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    ledger, pending, corrections = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template, [],
    )
    assert ledger == []
    assert [s["start"] for s in pending] == ["09:00", "13:00", "20:00"]
    # kind の穴は既定種別 (自由時間) で埋まる
    assert pending[1]["kind"] == "自由時間"
    assert any("自由時間" in c for c in corrections)
    # 保存検証を通る形になっている (replace_day_plan がそのまま受ける)
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, pending)


def test_compose_drops_extra_llm_slots(manager, task_refs):
    """テンプレートに無いコマは追加されない (枠は LLM 出力で変わらない)。"""
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    llm = [
        _llm_slot("09:00", "調べる", facility="library",
                  budget_rounds=5, ref="task:1", title="朝の調べ物をする"),
        _llm_slot("13:00", "絵を描く", budget_rounds=3),
        _llm_slot("20:00", "自室で過ごす"),
        _llm_slot("22:00", "出かける", title="夜の散歩をする"),  # 枠に無い
    ]
    ledger, pending, corrections = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template, llm,
    )
    assert len(pending) == 3
    assert all(s["start"] != "22:00" for s in pending)
    assert any("22:00" in c for c in corrections)


def test_compose_downgrades_stale_forced_ref(manager, ptm, task_refs):
    """テンプレ確定 ref のタスクが終了済みならコマは残して ref だけ none へ。"""
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.update_task_status(
        task_id, status="completed", actor="test", persona_id=PERSONA_ID,
    )
    ledger, pending, corrections = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template,
        [_llm_slot("09:00", "調べる", facility="library")],
    )
    assert pending[0]["ref"] == "none"
    assert pending[0]["kind"] == "調べる"  # コマ自体は残る
    assert any("task:1" in c for c in corrections)


# ---------------------------------------------------------------------------
# 途中起動の合流 (§11-12)
# ---------------------------------------------------------------------------


def test_compose_marks_past_slots_missed_and_joins_from_now(manager, task_refs):
    clock.enable_virtual(datetime(2026, 7, 4, 12, 0, 0))  # 正午起動
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    ledger, pending, corrections = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template, [],
    )
    # 午前のコマ (09:00) は「流れた」帳簿へ。start は丸めず原時刻のまま
    assert len(ledger) == 1
    missed = ledger[0]
    assert missed["start"] == "09:00"
    assert missed["status"] == day_plan.STATUS_SKIPPED
    assert missed["skip_reason"] == day_plan.SKIP_REASON_MISSED_START
    assert day_plan.slot_result_label(missed) == \
        "流れた（サーバーが起動していなかったため）"
    # 午後のコマから合流
    assert [s["start"] for s in pending] == ["13:00", "20:00"]
    assert any("流れた" in c for c in corrections)


def test_compose_grace_keeps_slightly_late_slot_pending(manager, task_refs):
    """数分のズレ (閾値以内) は「流れた」にせず既存の丸め救済に委ねる。"""
    clock.enable_virtual(datetime(2026, 7, 4, 9, 5, 0))  # 5 分の遅発
    template = tt.save_template(manager, PERSONA_ID, _template_slots())["slots"]
    ledger, pending, _ = tt.compose_timetable_from_template(
        manager, PERSONA_ID, PLAN_DATE, template, [],
    )
    assert ledger == []
    assert [s["start"] for s in pending] == ["09:00", "13:00", "20:00"]


# ---------------------------------------------------------------------------
# judgment_finalize (day_open) 経由の統合
# ---------------------------------------------------------------------------


def _day_open_output(timetable):
    return {"monologue": "今日も雛形どおりに。", "timetable": timetable}


def _run_finalize(manager, finalize_mod, tmp_path, output):
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        return finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )


def test_finalize_enforces_template_over_llm_output(
    manager, task_refs, finalize_mod, tmp_path,
):
    tt.save_template(manager, PERSONA_ID, _template_slots())
    output = _day_open_output([
        # 確定コマからの逸脱 (種別・場所) — テンプレ値に矯正されるべき
        _llm_slot("09:00", "出かける", title="散歩する"),
        _llm_slot("13:00", "絵を描く", budget_rounds=3, title="昼の絵を描く"),
        _llm_slot("20:00", "自室で過ごす", title="静かに過ごす"),
    ])
    _run_finalize(manager, finalize_mod, tmp_path, output)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["09:00", "13:00", "20:00"]
    assert slots[0]["kind"] == "調べる"          # 矯正 (テンプレ確定値)
    assert slots[0]["facility"] == "library"
    assert slots[0]["ref"] == "task:1"
    assert slots[1]["kind"] == "絵を描く"        # 穴 = LLM の自由
    assert slots[1]["budget_rounds"] == 3
    assert all(s["status"] == "pending" for s in slots)
    assert manager.event_scheduler.pending_count() == 3

    # 矯正は monologue とは別の適用サマリ行として記録される
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "テンプレートの確定項目を維持" in content
    assert "今日も雛形どおりに。" in content


def test_finalize_without_template_keeps_free_composition(
    manager, task_refs, finalize_mod, tmp_path,
):
    """テンプレ未設定のペルソナは従来どおり LLM の時間割がそのまま保存される。"""
    output = _day_open_output([
        _llm_slot("09:00", "出かける", title="散歩する"),
        _llm_slot("21:00", "自室で過ごす"),
    ])
    _run_finalize(manager, finalize_mod, tmp_path, output)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["kind"] for s in slots] == ["出かける", "自室で過ごす"]
    assert manager.event_scheduler.pending_count() == 2


def test_finalize_midday_records_missed_and_skips_clamp(
    manager, task_refs, finalize_mod, tmp_path,
):
    """正午起動: 午前のテンプレコマは「流れた」記録 (丸めクランプなし) +
    午後から合流。ライフ宣言があっても帳簿区間は現在時刻へ繰り上げられない。"""
    clock.enable_virtual(datetime(2026, 7, 4, 12, 0, 0))
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    tt.save_template(manager, PERSONA_ID, _template_slots())
    output = _day_open_output([
        _llm_slot("13:00", "絵を描く", budget_rounds=3, title="昼の絵を描く"),
        _llm_slot("20:00", "自室で過ごす", title="静かに過ごす"),
    ])
    _run_finalize(manager, finalize_mod, tmp_path, output)

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["start"] for s in slots] == ["09:00", "13:00", "20:00"]
    missed = slots[0]
    assert missed["status"] == day_plan.STATUS_SKIPPED
    assert missed["skip_reason"] == day_plan.SKIP_REASON_MISSED_START
    assert missed["start"] == "09:00", "流れたコマが現在時刻へ丸められている"
    pending = [s for s in slots if s["status"] == "pending"]
    assert [s["start"] for s in pending] == ["13:00", "20:00"]
    assert manager.event_scheduler.pending_count() == 2

    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "流れた（サーバーが起動していなかったため）" in content


def test_day_open_situation_text_shows_template_and_holes(manager, task_refs):
    from saiverse import judgment_points as jp

    tt.save_template(manager, PERSONA_ID, _template_slots())
    text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "[今日の習慣テンプレート]" in text
    assert "09:00 調べる" in text
    assert "＜空欄＞" in text          # 13:00 の穴が明示される
    assert "埋めて" in text
    # テンプレ無しの従来文面は出ない
    assert "今日の時間割を編成してください" not in text

    tt.delete_template(manager, PERSONA_ID)
    text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "[今日の習慣テンプレート]" not in text
    assert "今日の時間割を編成してください" in text


# ---------------------------------------------------------------------------
# API (GET / PUT / DELETE)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(manager):
    from api.routes.people import timetable_template as tt_route

    app = FastAPI()
    app.include_router(tt_route.router, prefix="/api/people")
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as c:
        yield c


def test_api_roundtrip(client, manager, task_refs):
    url = f"/api/people/{PERSONA_ID}/timetable-template"

    # 未設定は null
    res = client.get(url)
    assert res.status_code == 200
    assert res.json() is None

    res = client.put(url, json={"slots": _template_slots()})
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is True
    assert [s["start"] for s in body["slots"]] == ["09:00", "13:00", "20:00"]

    res = client.get(url)
    assert res.status_code == 200
    assert res.json()["slots"] == body["slots"]

    res = client.delete(url)
    assert res.status_code == 200
    assert res.json() == {"deleted": True}
    assert client.get(url).json() is None
    assert client.delete(url).json() == {"deleted": False}


@pytest.mark.parametrize("slots, needle", [
    ([{"start": "10:00"}, {"start": "09:00"}], "ascending"),
    ([{"start": "09:00", "kind": "暮らし"}], "暮らし"),      # カタログ外 kind の明示
    ([{"start": "09:00", "budget_rounds": -1}], "budget_rounds"),
])
def test_api_put_validation_errors(client, slots, needle):
    res = client.put(
        f"/api/people/{PERSONA_ID}/timetable-template", json={"slots": slots},
    )
    assert res.status_code == 422
    assert needle in json.dumps(res.json(), ensure_ascii=False)


def test_api_unknown_persona_is_404(client):
    assert client.get("/api/people/nobody/timetable-template").status_code == 404
    assert client.delete("/api/people/nobody/timetable-template").status_code == 404
    assert client.put(
        "/api/people/nobody/timetable-template",
        json={"slots": [{"start": "09:00"}]},
    ).status_code == 404
    assert client.get(
        "/api/people/nobody/timetable-template/facilities"
    ).status_code == 404


def test_api_facilities_match_judgment_enum(client, manager):
    """場所セレクトの選択肢 = 起床判断の facility enum と同じ集合 (T2b)。"""
    from saiverse import judgment_points as jp

    res = client.get(f"/api/people/{PERSONA_ID}/timetable-template/facilities")
    assert res.status_code == 200
    options = res.json()
    assert [o["id"] for o in options] == jp.collect_facility_ids(manager)
    names = {o["id"]: o["name"] for o in options}
    assert names["library"] == "図書館"
    assert names["own_room"] == "自室"


def test_api_slot_kinds_lists_catalog():
    """kind セレクトの選択肢 = コマ種別カタログの語彙 (提示順も一致、T2b)。

    config ルーターは manager 非依存の /slot-kinds だけを叩く。
    """
    from api.routes import config as config_route
    from saiverse import slot_kind_catalog

    app = FastAPI()
    app.include_router(config_route.router, prefix="/api/config")
    with TestClient(app) as c:
        res = c.get("/api/config/slot-kinds")
    assert res.status_code == 200
    body = res.json()
    assert [k["name"] for k in body] == list(slot_kind_catalog.kind_names())
    assert [k["name"] for k in body] == list(day_plan.all_kinds())
    for entry in body:
        assert entry["execution_type"] in slot_kind_catalog.EXECUTION_TYPES
        assert entry["description"]
