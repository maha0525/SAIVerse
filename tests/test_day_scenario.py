"""⑨ mock 一日シム通しテスト (saiverse/day_scenario.py + day_report.py、自律行動 v2 §12)。

mock LLM (LLM コストゼロ) で一日を端から端まで回す配線テスト:

- 標準の一日: 9:00 起床 (day_open が時間割を編成) → 10:00 知る (図書館) →
  14:00 作る (工房、mock 成果物 = 実 Item) → 15:00 ユーザー会話割り込み →
  15:30 会話終了 (post_conversation) → 20:00 休む → 22:00 就寝 (day_close)
  - 成果物 Item が DB に実在する
  - タスクが artifact_refs 付きで completed になる
  - 日次予算台帳が実測ラウンドで消費される
  - day_close の tomorrow_memo が翌日 day_open の状況テキストに出る (連結)
  - 一日レポートに予定 vs 実績・成果物 (saiverse:// URI)・明日の自分へのメモが含まれる
  - 実時間 30 秒未満で完走する
- 終日不在 + 空バックログ: 全コマ 暮らし/休む でもクラッシュせずレポートが出る

判断点の LLM は MockJudgmentPulseController (judge_fn) が代替し、作業セッションの
LLM はスクリプト応答 (document_create スペル発動テキスト → mock スペルが Item を
実際に作る) が代替する。tools のロードは動的なので spell 登録・実行は
sea.runtime_llm 名前空間の patch で差し替える (test_work_session.py と同じ流儀)。

teardown で engine.dispose() + clock.disable_virtual() を必ず行う。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import AI, Base, City, Item, User
from saiverse import clock
from saiverse import day_plan
from saiverse import judgment_points as jp
from saiverse.day_report import generate_day_report, save_day_report
from saiverse.day_scenario import (
    MockJudgmentPulseController,
    RealConversationUserEventDriver,
    ScenarioPlayer,
    SyncJudgmentDispatcher,
    TrackSimUserEventDriver,
    parse_scenario,
)
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager
from saiverse.track_manager import TrackManager

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"


# ---------------------------------------------------------------------------
# fixtures / fakes
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


class RecordingAdapter:
    """SAIMemory adapter の mock (append + tag フィルタ付き読み出し)。

    実 adapter (saiverse_memory/adapter.py) の読み出し挙動に合わせている:

    - created_at は Unix epoch (int) — ISO 文字列で返すと day_report の
      日付フィルタが偶然通ってしまい、実環境でのみ壊れる
    - paired_action_text は「タグ無しの user payload」として直前に展開される
    - タグ無し payload はタグフィルタを素通しする (レガシー互換の寛容フォールバック)
    """

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def get_current_thread(self):
        return f"{PERSONA_ID}:persona_main"

    def append_persona_message(self, payload):
        payload = dict(payload)
        created = payload.get("created_at")
        if isinstance(created, str):
            payload["created_at"] = int(datetime.fromisoformat(created).timestamp())
        self.messages.append(payload)

    def recent_persona_messages_by_count(self, max_messages, *, required_tags=None,
                                         required_line_roles=None,
                                         required_scopes=None, pulse_id=None):
        expanded: List[Dict[str, Any]] = []
        for p in self.messages:
            action_text = p.get("paired_action_text")
            if action_text:
                expanded.append({
                    "role": "user",
                    "content": action_text,
                    "created_at": p.get("created_at"),
                })
                p = {k: v for k, v in p.items() if k != "paired_action_text"}
            expanded.append(p)
        selected = []
        for payload in expanded:
            tags = (payload.get("metadata") or {}).get("tags") or []
            if required_tags and tags and not all(t in tags for t in required_tags):
                continue
            selected.append(payload)
        return selected[-max_messages:]


class ScriptedLLMClient:
    """スクリプト化された応答を順に返す mock LLM (作業セッション用)。"""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.calls = 0

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.calls += 1
        if not self.responses:
            raise AssertionError("ScriptedLLMClient: no scripted responses left")
        return self.responses.pop(0)

    def consume_usage(self):
        return None


class FakeWorkRuntime:
    """run_work_session が触る SEARuntime の最小フェイク (test_work_session と同型)。

    _store_memory はペルソナの RecordingAdapter へ転送する — 一日レポートと
    day_close の状況テキストが committed ダイジェストを読めるようにするため。
    """

    def __init__(self, llm_client, personas: Dict[str, Any]):
        self.llm_client = llm_client
        self.personas = personas

    def _prepare_context(self, persona, building_id, user_input, requirements,
                         pulse_id=None, **kwargs):
        return [{"role": "system", "content": "HEAD"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False,
                           state=None):
        return self.llm_client

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _is_spell_enabled_for_persona(self, persona):
        return True

    def _dump_llm_io(self, playbook_name, node_id, persona, messages, output_text):
        return None

    def _accumulate_usage(self, state, model, input_tokens, output_tokens,
                          cost_usd, cached_tokens=0, cache_write_tokens=0):
        return None

    def _touch_anchor_after_llm_call(self, persona, usage):
        return None

    def _get_or_create_pulse_context(self, pulse_id, thread_id):
        from sea.pulse_context import PulseContext
        return PulseContext(pulse_id=pulse_id, thread_id=thread_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        return None

    def _store_memory(self, persona, text, *, role="assistant", tags=None,
                      pulse_id=None, metadata=None, playbook_name=None,
                      pulse_context=None, line_role=None, line_id=None,
                      origin_track_id=None, scope=None, paired_action_text=None,
                      thought_signature=None, spell_origin_id=None, spell_seq=None,
                      return_message_id=False):
        resolved_scope = scope
        if pulse_context is not None and resolved_scope is None:
            resolved_scope = pulse_context.current_line_metadata().get("scope")
        payload_metadata = dict(metadata or {})
        payload_metadata["tags"] = list(tags or [])
        persona.sai_memory.append_persona_message({
            "role": role,
            "content": text,
            "created_at": clock.now().isoformat(timespec="seconds"),
            "metadata": payload_metadata,
            "scope": resolved_scope,
            "line_role": line_role,
            "paired_action_text": paired_action_text,
        })
        return "msg-x" if return_message_id else True


def _make_manager(session_factory, tmp_path, judge_fn, session_responses):
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
        persona_name="Alice",
        current_building_id="alice_room",
        private_room_id="alice_room",
        sai_memory=RecordingAdapter(),
    )
    personas = {PERSONA_ID: persona}

    class StubOccupancy:
        # 本物と同じく persona.current_building_id は触らない
        # (更新は day_plan._move_to_facility の責務)。
        def __init__(self):
            self.moves: List[tuple] = []

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            return True, "ok"

    llm = ScriptedLLMClient(session_responses)
    manager = SimpleNamespace(
        SessionLocal=session_factory,
        personas=personas,
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
        event_scheduler=EventScheduler(),  # start() しない (DES 前提)
        track_manager=TrackManager(session_factory=session_factory),
        occupancy_manager=StubOccupancy(),
        sea_runtime=FakeWorkRuntime(llm, personas),
        _session_llm=llm,
    )
    manager.pulse_controller = MockJudgmentPulseController(manager, judge_fn, tmp_path)
    return manager


def _make_fake_spell(session_factory, created_item_ids: List[str]):
    """mock スペル実行器。document_create は Item を DB に実際に作る (接地)。"""

    async def fake_spell(tool_name, tool_args, persona, state, playbook_name,
                         event_callback, messages=None):
        if tool_name == "document_create":
            item_id = str(uuid.uuid4())
            title = str(tool_args.get("title") or "mock文書")
            now = datetime.utcnow()
            db = session_factory()
            try:
                db.add(Item(
                    ITEM_ID=item_id, NAME=title, TYPE="document", DESCRIPTION="",
                    CREATOR_ID=persona.persona_id, CREATED_AT=now, UPDATED_AT=now,
                ))
                db.commit()
            finally:
                db.close()
            created_item_ids.append(item_id)
            return (f"文書「{title}」を作成しました。アイテムID: {item_id}", None)
        return ("検索結果: 標本と蒸留に関する記事が 3 件見つかりました。", None)

    return fake_spell


def _patched_spells(session_factory, created_item_ids):
    return (
        patch("sea.runtime_llm.SPELL_TOOL_NAMES",
              {"document_create", "searxng_search"}),
        patch("sea.runtime_llm._run_spell_tool_async",
              new=_make_fake_spell(session_factory, created_item_ids)),
    )


# ---------------------------------------------------------------------------
# 標準の一日 (通し)
# ---------------------------------------------------------------------------


_TIMETABLE = [
    {"start": "10:00", "kind": "知る", "title": "標本の材料を探す", "ref": "task:2",
     "facility": "library", "budget_rounds": 3, "note": "図鑑コーナーを中心に見る"},
    {"start": "14:00", "kind": "作る", "title": "共有文の下書きを書く", "ref": "task:1",
     "facility": "workshop", "budget_rounds": 8, "note": "命令調にしないこと"},
    {"start": "20:00", "kind": "休む", "ref": "none",
     "facility": "own_room", "budget_rounds": 0, "note": ""},
]


def _standard_judge(kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """判断点の mock 裁定 (標準の一日シナリオ用)。"""
    ctx = json.loads(args.get("judgment_context") or "{}")
    if kind == "day_open":
        return {"monologue": "今日は午前に調べ物、午後に共有文を仕上げる。",
                "timetable": [dict(s) for s in _TIMETABLE]}
    if kind == "post_session":
        output: Dict[str, Any] = {
            "monologue": "セッションを終えた。",
            "new_desires": [],
            "remaining_timetable": None,
        }
        artifacts = ctx.get("artifacts") or []
        if ctx.get("task_ref"):
            if artifacts:
                output["task_verdict"] = {
                    "status": "done", "artifact_ref": artifacts[0],
                    "desk_memo": "共有文はレビュー待ち",
                }
            else:
                output["task_verdict"] = {
                    "status": "continue", "desk_memo": "材料は集まりつつある",
                }
        return output
    if kind == "post_conversation":
        return {"monologue": "楽しい会話だった。拾うものは特にない。",
                "picked_tasks": [], "new_desires": [],
                "remaining_timetable": None}
    if kind == "day_close":
        return {
            "monologue": "予定と実際を見比べた。概ね予定どおりの一日だった。",
            "tomorrow_memo": "明日は標本集の整理から始める",
            "day_theme": "制作",
            "user_report_seeds": ["共有文の下書きを仕上げました"],
        }
    raise AssertionError(f"unexpected judgment kind: {kind}")


#: 作業セッションの mock LLM 応答 (発火順):
#: セッション 1 (知る): searxng スペル → 締め → ダイジェスト
#: セッション 2 (作る): document_create スペル → 締め → ダイジェスト
_SESSION_RESPONSES = [
    "まず記事を探す。\n/spell name='searxng_search' args={\"query\": \"言葉 標本 蒸留\"}",
    "めぼしい記事を確認した。今日はここまでにする。",
    "記事を 3 件確認して、標本集に使えそうな言い回しを頭に留めた。",
    "本文を書く。\n/spell name='document_create' args={\"title\": \"共有文の下書き\"}",
    "書き上げて読み直した。命令調を誘い口調に直してある。",
    "共有文の下書きを書いた。『検査に行け』ではなく『一緒に見に行こう』に直した。",
]

_STANDARD_SCENARIO = {
    "persona_id": PERSONA_ID,
    "plan_date": PLAN_DATE,
    "wake": "09:00",
    "sleep": "22:00",
    "daily_budget_rounds": 20,
    "seed": {
        "desires": [{"title": "言葉の標本集", "type": "作る",
                     "source": "会話で言い回しを褒められた"}],
        "tasks": [{"title": "共有文の下書きを書く", "goal": "本文が実在すること"}],
    },
    "user_events": [
        {"at": "15:00", "type": "message", "text": "ただいま。調子はどう？"},
        {"at": "15:30", "type": "leave"},
    ],
    "events": [],
}


@pytest.fixture
def standard_run(session_factory, tmp_path):
    """標準の一日シナリオを 1 回だけ実行し、(manager, result, elapsed, items) を返す。"""
    manager = _make_manager(
        session_factory, tmp_path, _standard_judge, list(_SESSION_RESPONSES),
    )
    created_item_ids: List[str] = []
    p_names, p_exec = _patched_spells(session_factory, created_item_ids)

    started = time.monotonic()
    with p_names, p_exec:
        result = ScenarioPlayer().run(manager, dict(_STANDARD_SCENARIO))
    elapsed = time.monotonic() - started
    return manager, result, elapsed, created_item_ids


def test_standard_day_runs_end_to_end(standard_run):
    manager, result, elapsed, created_item_ids = standard_run

    # 実時間 30 秒未満 (DES による一日圧縮)
    assert elapsed < 30.0, f"one simulated day took {elapsed:.1f}s (>= 30s)"

    # イベント消化: day_open + コマ 3 + ユーザーイベント 2 + day_close = 7
    assert result.executed_events == 7
    # 判断点の並び: 起床 → セッション終了 x2 → 会話終了 → 就寝
    assert [j["kind"] for j in result.judgments] == [
        "day_open", "post_session", "post_session", "post_conversation", "day_close",
    ]
    assert all(j["submitted"] for j in result.judgments)
    # 仮想時刻は就寝時刻で止まっている
    assert clock.now() == datetime(2026, 7, 4, 22, 0, 0)

    # 全コマ消化 (done)
    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done", "done"]

    # 施設移動: 自室 → 図書館 → 工房 → 自室
    assert manager.occupancy_manager.moves == [
        (PERSONA_ID, "ai", "alice_room", "library"),
        (PERSONA_ID, "ai", "library", "workshop"),
        (PERSONA_ID, "ai", "workshop", "alice_room"),
    ]

    # ユーザー会話 Track が作られ、退室で completed になっている
    completed = [
        t for t in manager.track_manager.list_for_persona(
            PERSONA_ID, statuses=["completed"],
        )
        if t.track_type == "user_conversation"
    ]
    assert len(completed) == 1


def test_standard_day_artifact_item_exists_and_task_completed(standard_run):
    manager, result, elapsed, created_item_ids = standard_run

    # 成果物 Item が実在する (mock スペルが実際に DB へ作った 1 件)
    assert len(created_item_ids) == 1
    db = manager.SessionLocal()
    try:
        rows = db.query(Item).filter(Item.CREATOR_ID == PERSONA_ID).all()
        assert [r.ITEM_ID for r in rows] == created_item_ids
        assert rows[0].NAME == "共有文の下書き"
    finally:
        db.close()

    # タスクが artifact_refs 付きで completed
    ptm = PersonaTaskManager(manager.SessionLocal)
    task = ptm.get_task(
        ptm.resolve_task_ref(PERSONA_ID, "task:1"), persona_id=PERSONA_ID,
    )
    assert task["status"] == "completed"
    assert task["artifact_refs"] == created_item_ids

    # 知る コマの desire 参照 → 再訪が帳簿に載る
    desire = ptm.get_task(
        ptm.resolve_task_ref(PERSONA_ID, "task:2"), persona_id=PERSONA_ID,
    )
    assert desire["touch_count"] == 1


def test_standard_day_budget_ledger_reflects_actual_rounds(standard_run):
    manager, result, elapsed, created_item_ids = standard_run
    # 各セッション 1 ラウンド (spell 1 発 → 締め) x 2 = 実測 2 ラウンド消費
    state = day_plan.get_budget_state(manager, PERSONA_ID, PLAN_DATE)
    assert state == {"total": 20, "used": 2, "remaining": 18}


def test_standard_day_tomorrow_memo_links_to_next_day_open(standard_run):
    manager, result, elapsed, created_item_ids = standard_run
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta["tomorrow_memo"] == "明日は標本集の整理から始める"
    assert meta["day_theme"] == "制作"

    # 翌朝の起床判断の状況テキストに昨日の自分からのメモと実績ダイジェストが出る
    clock.advance_to(datetime(2026, 7, 5, 7, 0, 0))
    morning_text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "明日は標本集の整理から始める" in morning_text
    assert "今日の時間割（予定 → 実績）" in morning_text  # day_digest (実績要約)
    assert "10:00" in morning_text


def test_standard_day_report_contents(standard_run, tmp_path):
    manager, result, elapsed, created_item_ids = standard_run
    report = generate_day_report(manager, PERSONA_ID, PLAN_DATE)

    # ヘッダ (ペルソナ名・日付・テーマ)
    assert "Alice の一日新聞 — 2026-07-04" in report
    assert "今日のテーマ: 制作" in report
    # 時間割: 主役 3 列は「時刻 | やること (表題) | 実績」。型・場所・参照・
    # 予算・予定メモは補足列 (まはーフィードバック #1/#2)
    assert "| 時刻 | やること | 実績 | 補足 |" in report
    assert "| 10:00 | 標本の材料を探す | 実行済み | 知る ／ 図書館 ／ 参照: task:2 ／ 予算: 3 ／ 図鑑コーナーを中心に見る |" in report
    assert "| 14:00 | 共有文の下書きを書く | 実行済み | 作る ／ 工房 ／ 参照: task:1 ／ 予算: 8 ／ 命令調にしないこと |" in report
    # title の無いコマ (休む) は kind で代替表示、場所は表示名 (後方互換)。
    # 休む (スタブ) は「実行済み」と偽らず、詳細記録が無いことを正直に示す
    assert "| 20:00 | 休む | 時間を過ごした（詳細な記録なし） | 自分の部屋 |" in report
    # 節順序: 時間割 → 就寝のふりかえり → システム的な節 (フィードバック #3)
    assert report.index("## 時間割") < report.index("## 就寝のふりかえり") \
        < report.index("## 作業セッションの成果") < report.index("## 作業予算")
    # セッションの成果 (ダイジェスト + 成果物名と saiverse:// URI)
    assert "共有文の下書きを書いた" in report
    # URI は AI 可視の short_id (item:N 系)。UUID は裏方なので出さない。
    db = manager.SessionLocal()
    try:
        item = db.query(Item).filter(Item.ITEM_ID == created_item_ids[0]).first()
        item_ref = item.SHORT_ID if item.SHORT_ID is not None else created_item_ids[0]
    finally:
        db.close()
    assert f"共有文の下書き（saiverse://item/{item_ref}/content）" in report
    # 判断プロンプト (paired_action_text のタグ無し展開) が成果・独白に混入しない
    # (adapter のタグフィルタはタグ無し行を素通しするため、day_report 側のタグ
    #  厳密チェックが無いと [起床判断] 等の状況テキストがセッション扱いになる)
    assert "[起床判断]" not in report
    assert "[就寝判断]" not in report
    # 欲求の動き (今日生まれた種の欲求と再訪)
    assert "[作る] 言葉の標本集 — 出自: 会話で言い回しを褒められた" in report
    assert "再訪: 1 回" in report
    # 予算 (実測)
    assert "消費 2 / 全体 20 ラウンド（残り 18）" in report
    # 就寝のふりかえり (独白・明日の自分へのメモ・報告種)
    assert "概ね予定どおりの一日だった" in report
    assert "明日の自分へのメモ: 明日は標本集の整理から始める" in report
    assert "共有文の下書きを仕上げました" in report
    # 就寝判断の適用エコー行は載せない — 独白だけを表示し、明日へのメモ・テーマ等
    # は plan meta 由来の節で一度だけ出す (重複解消、フィードバック #5)
    assert "（今日のふりかえりを記録した）" not in report
    assert report.count("明日は標本集の整理から始める") == 1

    # 保存 (base_dir 上書きでテスト内に閉じる)
    path = save_day_report(
        manager, PERSONA_ID, PLAN_DATE, report_text=report,
        base_dir=tmp_path / "day_reports",
    )
    assert path.name == "2026-07-04.md"
    assert path.read_text(encoding="utf-8") == report


# ---------------------------------------------------------------------------
# 実 LLM モードのユーザー会話経路 (バグ回帰: 2026-07-05 クオン一日シム)
# ---------------------------------------------------------------------------


def test_sync_dispatcher_submit_user_runs_sea_user_path():
    """SyncJudgmentDispatcher.submit_user が実 SEA のユーザー会話経路を同期で叩く。

    回帰: --real で UserConversationTrackHandler → run_sea_user →
    pulse_controller.submit_user が AttributeError になり、ユーザー発話への
    応答が一切生成されなかった。
    """
    calls: List[Dict[str, Any]] = []

    class RecordingRuntime:
        def run_meta_user(self, persona, **kwargs):
            calls.append({"persona": persona, **kwargs})
            return ["応答した"]

    persona = SimpleNamespace(persona_id=PERSONA_ID)
    manager = SimpleNamespace(
        personas={PERSONA_ID: persona}, sea_runtime=RecordingRuntime(),
    )
    dispatcher = SyncJudgmentDispatcher(manager)

    out = dispatcher.submit_user(
        persona_id=PERSONA_ID, building_id="lobby", user_input="",
        origin_track_id="track-1",
    )

    assert out == ["応答した"]
    assert len(calls) == 1
    assert calls[0]["persona"] is persona
    assert calls[0]["pulse_type"] == "user"
    assert calls[0]["building_id"] == "lobby"
    assert calls[0]["origin_track_id"] == "track-1"

    with pytest.raises(RuntimeError, match="not found"):
        dispatcher.submit_user(
            persona_id="ghost", building_id="lobby", user_input="x",
        )


class _FakeHistoryManager:
    """building_messages の最小フェイク (seq 採番 + heard_by 保持)。"""

    def __init__(self):
        self.building: List[Dict[str, Any]] = []

    def add_to_building_only(self, building_id, msg, *, heard_by=None,
                             origin_track_id=None):
        entry = dict(msg)
        entry["seq"] = len(self.building) + 1
        entry["heard_by"] = list(heard_by or [])
        self.building.append(entry)
        return entry

    def get_building_history(self, building_id):
        return list(self.building)


class _FakeTrackManagerWithHook:
    """create(initial_status='running') で observer (main_line 起動) を模す。"""

    def __init__(self, on_activate=None):
        self.on_activate = on_activate
        self.created: List[Dict[str, Any]] = []
        self.running_track = None
        self.completed: List[str] = []
        self.paused: List[str] = []

    def get_running(self, persona_id):
        return self.running_track

    def create(self, persona_id, track_type, title=None, intent=None,
               output_target="none", is_persistent=False, metadata=None,
               initial_status="unstarted"):
        track_id = f"track-{len(self.created) + 1}"
        self.created.append({
            "track_id": track_id, "track_type": track_type,
            "output_target": output_target, "initial_status": initial_status,
        })
        self.running_track = SimpleNamespace(
            track_id=track_id, track_type=track_type,
        )
        if self.on_activate is not None:
            self.on_activate(track_id)
        return track_id

    def complete(self, track_id):
        self.completed.append(track_id)
        self.running_track = None

    def pause(self, track_id):
        self.paused.append(track_id)
        self.running_track = None


def _make_real_driver_manager(persona, reply: bool):
    """RealConversationUserEventDriver 用の最小 manager フェイク。

    reply=True なら run_sea_user (実 Pulse の代役) がペルソナ応答を
    building_messages に書く。False なら何も書かない (応答生成失敗の再現)。
    """
    manager = SimpleNamespace(personas={PERSONA_ID: persona}, user_id=1)
    sea_calls: List[Dict[str, Any]] = []

    def run_sea_user(p, building_id, user_input, **kwargs):
        sea_calls.append({
            "building_id": building_id, "user_input": user_input, **kwargs,
        })
        if reply:
            persona.history_manager.add_to_building_only(
                building_id,
                {"role": "assistant", "content": "おかえり", "persona_id": PERSONA_ID},
                heard_by=[PERSONA_ID],
            )
        return ["おかえり"] if reply else []

    manager.run_sea_user = run_sea_user
    manager.track_manager = _FakeTrackManagerWithHook(
        on_activate=lambda tid: manager.run_sea_user(
            persona, persona.current_building_id, "", origin_track_id=tid,
        ),
    )
    manager._sea_calls = sea_calls
    return manager


def test_real_driver_records_user_message_and_detects_reply():
    """実会話ドライバ: 発話の building 記録 + 応答の実在検査 + 会話継続の直接起動。"""
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, current_building_id="lobby",
        history_manager=_FakeHistoryManager(),
    )
    manager = _make_real_driver_manager(persona, reply=True)
    driver = RealConversationUserEventDriver()

    driver.begin_conversation(manager, PERSONA_ID, "ただいま")

    hist = persona.history_manager.building
    assert hist[0]["role"] == "user" and hist[0]["content"] == "ただいま"
    # auto_ingest の取り込み条件 (heard_by にペルソナ) + 閲覧者フィルタ (ユーザー)
    assert PERSONA_ID in hist[0]["heard_by"] and "1" in hist[0]["heard_by"]
    # Track は実経路と同じ output_target で running 作成される
    assert manager.track_manager.created == [{
        "track_id": "track-1", "track_type": "user_conversation",
        "output_target": "building:current", "initial_status": "running",
    }]
    assert driver.conversation_had_exchange(manager, PERSONA_ID) is True

    # 会話継続: 2 通目は既存 running Track へ直接メインライン起動 (Track 追加なし)
    driver.begin_conversation(manager, PERSONA_ID, "つかれたー……")
    assert len(manager.track_manager.created) == 1
    assert manager._sea_calls[-1]["user_input"] == "つかれたー……"
    assert manager._sea_calls[-1]["origin_track_id"] == "track-1"

    assert driver.end_conversation(manager, PERSONA_ID) is True
    assert manager.track_manager.completed == ["track-1"]


def test_end_conversation_pauses_persistent_track():
    """本番由来の永続会話 Track は complete でなく pause で畳む。

    world clone した実ペルソナで顕在化 (2026-07-06): 対ユーザー会話 Track は
    is_persistent=true で completed への遷移が禁止。leave の意味論は本番の
    wait_response 自動 pause と同じ running → pending。
    """
    tm = _FakeTrackManagerWithHook()
    tm.running_track = SimpleNamespace(
        track_id="t-persistent", track_type="user_conversation", is_persistent=True,
    )
    manager = SimpleNamespace(track_manager=tm)
    driver = TrackSimUserEventDriver()

    assert driver.end_conversation(manager, PERSONA_ID) is True
    assert tm.paused == ["t-persistent"]
    assert tm.completed == []


def test_real_driver_flags_zero_exchange_when_no_reply(caplog):
    """応答が building_messages に実在しない会話は「成立していない」と記録される。"""
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, current_building_id="lobby",
        history_manager=_FakeHistoryManager(),
    )
    manager = _make_real_driver_manager(persona, reply=False)
    driver = RealConversationUserEventDriver()

    with caplog.at_level("WARNING", logger="saiverse.day_scenario"):
        driver.begin_conversation(manager, PERSONA_ID, "ただいま")

    assert driver.conversation_had_exchange(manager, PERSONA_ID) is False
    assert any("did not reply" in r.message for r in caplog.records)


class _NoExchangeDriver(TrackSimUserEventDriver):
    """会話は再生するが「往復ゼロ」を申告するドライバ (応答生成失敗の再現)。"""

    def conversation_had_exchange(self, manager, persona_id):
        return False


def test_zero_exchange_conversation_skips_post_conversation(session_factory, tmp_path):
    """1 往復も成立しなかった会話では post_conversation 判断を撃たない。

    回帰: --real で応答生成が失敗したのに「会話がひと区切りつきました」前提の
    判断が走り、ペルソナが存在しない会話の振り返りを書いた (作話の温床)。
    """
    manager = _make_manager(
        session_factory, tmp_path, _standard_judge, list(_SESSION_RESPONSES),
    )
    created: List[str] = []
    p_names, p_exec = _patched_spells(session_factory, created)
    with p_names, p_exec:
        result = ScenarioPlayer(user_event_driver=_NoExchangeDriver()).run(
            manager, dict(_STANDARD_SCENARIO),
        )

    # post_conversation は submitted されず、skip の帳簿だけが残る
    # (_standard_judge は post_conversation が来たら応答を返すので、撃たれて
    #  いないことは submitted 記録の不在で判定できる)
    post_conv = [j for j in result.judgments if j["kind"] == "post_conversation"]
    assert len(post_conv) == 1
    assert post_conv[0]["submitted"] is False
    assert "no exchange" in post_conv[0]["reason"]
    # 会話イベント自体は再生されている (Track が completed)
    completed = [
        t for t in manager.track_manager.list_for_persona(
            PERSONA_ID, statuses=["completed"],
        )
        if t.track_type == "user_conversation"
    ]
    assert len(completed) == 1
    # 他の判断点は通常どおり
    assert [j["kind"] for j in result.judgments if j["submitted"]] == [
        "day_open", "post_session", "post_session", "day_close",
    ]


def test_pre_existing_periodic_reservations_cleared_before_sim(
    session_factory, tmp_path,
):
    """シナリオ外の既存予約 (db_polling 等の実時刻シード) はシムで空回りさせない。

    回帰: --real で SAIVerseManager.__init__ が仮想化前に積んだ 3 秒周期の
    db_polling を、仮想時刻が予約時刻へ達した時点から DES が一日ぶん
    (約 8,300 ステップ) 空実行した (2026-07-05 実 LLM シム 3回目)。
    ScenarioPlayer はシム実行前に既存予約を全て除去する。
    """
    manager = _make_manager(
        session_factory, tmp_path, _standard_judge, list(_SESSION_RESPONSES),
    )
    polls: List[str] = []

    def _db_polling_tick():
        polls.append(clock.now().isoformat(timespec="seconds"))
        manager.event_scheduler.schedule(
            fire_at=clock.now() + timedelta(seconds=3),
            callback=_db_polling_tick,
            key="db_polling",
        )

    # SAIVerseManager.__init__ 相当: シナリオ開始前に積まれる自己再予約型の
    # 周期イベント。仮想の一日 (09:00-22:00) の中に入る時刻で積む — 実シムで
    # 起きた「到達した瞬間から 3 秒刻みで空回り」の再現条件。
    manager.event_scheduler.schedule(
        fire_at=datetime(2026, 7, 4, 12, 0, 0),
        callback=_db_polling_tick,
        key="db_polling",
    )

    created: List[str] = []
    p_names, p_exec = _patched_spells(session_factory, created)
    with p_names, p_exec:
        result = ScenarioPlayer().run(manager, dict(_STANDARD_SCENARIO))

    assert polls == [], "シナリオ外の定期イベントがシム中に発火した"
    assert not manager.event_scheduler.has_key("db_polling")
    # シナリオ由来のイベントは通常どおり消化される (標準の一日と同数)
    assert result.executed_events == 7
    assert [j["kind"] for j in result.judgments] == [
        "day_open", "post_session", "post_session", "post_conversation", "day_close",
    ]


def test_day_report_shows_system_skip_honestly(session_factory, tmp_path):
    """一日新聞: システム都合の skipped を「見送り」(本人判断) として表示しない。"""
    manager = _make_manager(session_factory, tmp_path, _standard_judge, [])
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "21:00", "kind": "自分を更新する", "ref": "none",
         "facility": "own_room", "budget_rounds": 8, "note": "気づきの整理",
         "title": "気づきを整理する", "status": "skipped",
         "skip_reason": day_plan.SKIP_REASON_NO_HANDLER},
    ])
    report = generate_day_report(manager, PERSONA_ID, PLAN_DATE)
    assert "見送り" not in report
    assert "| 21:00 | 気づきを整理する | 実行できず（システム側の問題: このコマ種別の実行手段が未実装） |" in report


# ---------------------------------------------------------------------------
# 終日不在 + 空バックログ
# ---------------------------------------------------------------------------


def _idle_judge(kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if kind == "day_open":
        return {
            "monologue": "急ぐものは何もない。静かに過ごす。",
            "timetable": [
                {"start": "10:00", "kind": "暮らし", "title": "静かに過ごす",
                 "ref": "none", "facility": "own_room", "budget_rounds": 0,
                 "note": "静かな時間"},
                # title なし (旧データ互換): 新聞は kind で代替表示する
                {"start": "20:00", "kind": "休む", "ref": "none",
                 "facility": "own_room", "budget_rounds": 0, "note": ""},
            ],
        }
    if kind == "day_close":
        return {"monologue": "何も成さない一日だったが、それでいい。",
                "tomorrow_memo": "明日も同じように"}
    raise AssertionError(f"unexpected judgment kind: {kind}")


def test_absent_all_day_with_empty_backlog(session_factory, tmp_path):
    scenario = parse_scenario({
        "persona_id": PERSONA_ID,
        "plan_date": PLAN_DATE,
        "wake": "09:00",
        "sleep": "21:00",
        "user_events": [{"type": "absent_all_day"}],
    })
    manager = _make_manager(session_factory, tmp_path, _idle_judge, [])
    created: List[str] = []
    p_names, p_exec = _patched_spells(session_factory, created)

    with p_names, p_exec:
        result = ScenarioPlayer().run(manager, scenario)

    # day_open + コマ 2 (暮らし/休む) + day_close = 4。LLM は一度も呼ばれない
    assert result.executed_events == 4
    assert [j["kind"] for j in result.judgments] == ["day_open", "day_close"]
    assert manager._session_llm.calls == 0
    assert created == []

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]

    # レポートは穴なく出る (データの無い節は「（なし）」)
    report = generate_day_report(manager, PERSONA_ID, PLAN_DATE)
    assert "の一日新聞 — 2026-07-04" in report
    # 暮らし/休む (スタブ) は「実行済み」でなく「時間を過ごした（詳細な記録
    # なし）」— していない活動の詳細をペルソナに捏造させない (異常 #4 回帰)
    assert "| 10:00 | 静かに過ごす | 時間を過ごした（詳細な記録なし） | 暮らし ／ 自分の部屋 ／ 静かな時間 |" in report
    assert "| 20:00 | 休む | 時間を過ごした（詳細な記録なし） | 自分の部屋 |" in report  # title なし → kind 代替
    assert "## 作業セッションの成果" in report
    assert "（なし）" in report
    assert "明日の自分へのメモ: 明日も同じように" in report
    assert "何も成さない一日だったが" in report


# ---------------------------------------------------------------------------
# シナリオ定義の検証
# ---------------------------------------------------------------------------


def test_parse_scenario_rejects_invalid_definitions():
    base = {"persona_id": PERSONA_ID, "plan_date": PLAN_DATE,
            "wake": "09:00", "sleep": "22:00"}
    with pytest.raises(ValueError, match="persona_id"):
        parse_scenario({**base, "persona_id": ""})
    with pytest.raises(ValueError, match="wake"):
        parse_scenario({**base, "wake": "9時"})
    with pytest.raises(ValueError, match="after wake"):
        parse_scenario({**base, "sleep": "08:00"})
    with pytest.raises(ValueError, match="requires text"):
        parse_scenario({**base, "user_events": [{"at": "10:00", "type": "message"}]})
    with pytest.raises(ValueError, match="type"):
        parse_scenario({**base, "user_events": [{"at": "10:00", "type": "wave"}]})
    with pytest.raises(ValueError, match="description"):
        parse_scenario({**base, "events": [{"at": "10:00"}]})
    with pytest.raises(ValueError, match="daily_budget_rounds"):
        parse_scenario({**base, "daily_budget_rounds": 0})
