"""⑨ mock 一日シム通しテスト (saiverse/day_scenario.py + day_report.py、自律行動 v2 §12)。

mock LLM (LLM コストゼロ) で一日を端から端まで回す配線テスト:

- 標準の一日: 9:00 起床 (day_open が時間割を編成) → 10:00 調べる (図書館) →
  14:00 随筆を書く (工房、mock 成果物 = 実 Item) → 15:00 ユーザー会話割り込み →
  15:30 会話終了 (出来事を閉じる帳簿処理のみ) → 20:00 自室で過ごす →
  22:00 就寝 (day_close)
  - 成果物 Item が DB に実在する
  - タスクが artifact_refs 付きで completed になる
  - 日次予算台帳が実測ラウンドで消費される
  - day_close の tomorrow_memo が翌日 day_open の状況テキストに出る (連結)
  - 一日レポートに予定 vs 実績・成果物 (saiverse:// URI)・明日の自分へのメモが含まれる
  - 実時間 30 秒未満で完走する
- 終日不在 + 空バックログ: 全コマ 出かける/自室で過ごす でもクラッシュせずレポートが出る

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
from saiverse import day_scenario
from saiverse import judgment_points as jp
from saiverse.day_report import generate_day_report, save_day_report
from saiverse.day_scenario import (
    MockJudgmentPulseController,
    RealConversationUserEventDriver,
    ScenarioPlayer,
    SyncJudgmentDispatcher,
    ConversationStateSimUserEventDriver,
    parse_scenario,
)
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import PersonaTaskManager

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
        mid = f"msg-{len(self.messages) + 1}"
        payload["id"] = mid
        self.messages.append(payload)
        # 実 adapter と同じく message id を返す
        return mid

    def recent_persona_messages_by_count(self, max_messages, *, required_tags=None,
                                         required_line_roles=None,
                                         required_scopes=None, pulse_id=None,
                                         strict_tags=False):
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
            if required_tags and strict_tags and not tags:
                # 実 adapter の strict_tags と同じ: タグ無し行 (paired_action
                # 展開行を含む) を legacy 救済で素通しにしない
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
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _prepare_context(self, persona, building_id, user_input, requirements=None,
                         pulse_id=None, **kwargs):
        return [{"role": "system", "content": "HEAD"}]

    def _select_llm_client(self, node_def, persona, needs_structured_output=False,
                           state=None):
        return self.llm_client

    def select_llm_client(self, node_def, persona, execution_context=None,
                          needs_structured_output=False, state=None):
        model = execution_context.model_key if execution_context is not None else "fake-model"
        return self.llm_client, model

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

    def _get_or_create_pulse_context(self, pulse_id):
        from sea.pulse_context import PulseContext
        return PulseContext(pulse_id=pulse_id)

    def _flush_pulse_logs(self, persona, pulse_context):
        return None

    def _store_memory(self, persona, text, *, role="assistant", tags=None,
                      pulse_id=None, metadata=None, playbook_name=None,
                      pulse_context=None, line_role=None, line_id=None,
                      scope=None, paired_action_text=None,
                      thought_signature=None, spell_origin_id=None, spell_seq=None,
                      return_message_id=False, beat_state=None):
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
        city = City(USERID=1, CITY_SLUG="test_city", UI_PORT=3001, API_PORT=8001)
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
        # 本物と同じ契約 (W7 柱5): 成功時に persona 属性を service 側で更新する。
        def __init__(self):
            self.moves: List[tuple] = []

        def move_entity(self, entity_id, entity_type, from_id, to_id, db_session=None):
            self.moves.append((entity_id, entity_type, from_id, to_id))
            target = personas.get(entity_id)
            if target is not None:
                target.current_building_id = to_id
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
            return (f"文書「{title}」を作成しました。アイテムID: {item_id}", None, True)
        return ("検索結果: 標本と蒸留に関する記事が 3 件見つかりました。", None, True)

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
    {"start": "10:00", "kind": "調べる", "title": "標本の材料を探す", "ref": "task:2",
     "facility": "library", "budget_rounds": 3, "note": "図鑑コーナーを中心に見る"},
    {"start": "14:00", "kind": "随筆を書く", "title": "共有文の下書きを書く", "ref": "task:1",
     "facility": "workshop", "budget_rounds": 8, "note": "命令調にしないこと"},
    {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
     "facility": "own_room", "budget_rounds": 0, "note": ""},
]


def _standard_judge(kind: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """判断点の mock 裁定 (標準の一日シナリオ用)。"""
    ctx = json.loads(args.get("judgment_context") or "{}")
    if kind == "day_open":
        return {"monologue": "今日は午前に調べ物、午後に共有文を仕上げる。",
                "timetable": [dict(s) for s in _TIMETABLE]}
    if kind == "post_session":
        artifacts = ctx.get("artifacts") or []
        # digest 統合 (W1 Chunk C / D9): post_session 自身が実績要約を書く
        output: Dict[str, Any] = {
            "monologue": "セッションを終えた。",
            "digest": (
                "共有文の下書きを書いた。『検査に行け』ではなく"
                "『一緒に見に行こう』に直した。"
                if artifacts else
                "記事を 3 件確認して、標本集に使えそうな言い回しを頭に留めた。"
            ),
            "remaining_timetable": None,
        }
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
    if kind == "day_close":
        return {
            "monologue": "予定と実際を見比べた。概ね予定どおりの一日だった。",
            "tomorrow_memo": "明日は標本集の整理から始める",
            "day_theme": "制作",
            "user_report_seeds": ["共有文の下書きを仕上げました"],
        }
    raise AssertionError(f"unexpected judgment kind: {kind}")


#: 作業セッションの mock LLM 応答 (発火順)。digest 専用コールは廃止 (D9) —
#: ダイジェストは _standard_judge の post_session 出力が書く:
#: セッション 1 (調べる): searxng スペル → 締め
#: セッション 2 (随筆を書く): document_create スペル → 締め
_SESSION_RESPONSES = [
    "まず記事を探す。\n/spell name='searxng_search' args={\"query\": \"言葉 標本 蒸留\"}",
    "めぼしい記事を確認した。今日はここまでにする。",
    "本文を書く。\n/spell name='document_create' args={\"title\": \"共有文の下書き\"}",
    "書き上げて読み直した。命令調を誘い口調に直してある。",
]

_STANDARD_SCENARIO = {
    "persona_id": PERSONA_ID,
    "plan_date": PLAN_DATE,
    "wake": "09:00",
    "sleep": "22:00",
    "daily_budget_rounds": 20,
    "seed": {
        # task:1 / task:2 の順で植わる (種まきはリスト順)。旧シナリオでは task:2 は
        # 欲求候補だったが、欲求プールの退役で素のバックログタスクになった。
        "tasks": [
            {"title": "共有文の下書きを書く", "goal": "本文が実在すること"},
            {"title": "言葉の標本集", "goal": "気に入った言い回しを集める"},
        ],
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
    # 判断点の並び: 起床 → セッション終了 x2 → 就寝
    # (会話終了判断は 2026-08-16 に退役 — 退室は帳簿処理だけ)
    assert [j["kind"] for j in result.judgments] == [
        "day_open", "post_session", "post_session", "day_close",
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

    # 会話は開かれ、退室で閉じている。器は Track (v1) → 出来事の行 (2026-08-21)
    # → メモリ内の会話状態 (2026-08-22、束 6c) と三代目で、いま残るのは
    # 「開いていない」という事実だけ (autonomous_behavior_v3.md §7)。
    from saiverse import user_conversation as uc

    assert uc.get_open_conversation(manager, PERSONA_ID) is None


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

    # 翌朝の起床判断の状況テキストに出るのは昨日の自分からのメモだけ
    # (生の実績表 = 旧 day_digest は再供給しない、2026-07-29)
    clock.advance_to(datetime(2026, 7, 5, 7, 0, 0))
    morning_text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "明日は標本集の整理から始める" in morning_text
    assert "今日の時間割（予定 → 実績）" not in morning_text


def test_standard_day_report_contents(standard_run, tmp_path):
    manager, result, elapsed, created_item_ids = standard_run
    report = generate_day_report(manager, PERSONA_ID, PLAN_DATE)

    # ヘッダ (ペルソナ名・日付・テーマ)
    assert "Alice の一日新聞 — 2026-07-04" in report
    assert "今日のテーマ: 制作" in report
    # 時間割: 主役 3 列は「時刻 | やること (表題) | 実績」。型・場所・参照・
    # 予算・予定メモは補足列 (まはーフィードバック #1/#2)
    assert "| 時刻 | やること | 実績 | 補足 |" in report
    assert "| 10:00 | 標本の材料を探す | 実行済み | 調べる ／ 図書館 ／ 参照: task:2 ／ 予算: 3 ／ 図鑑コーナーを中心に見る |" in report
    assert "| 14:00 | 共有文の下書きを書く | 実行済み | 随筆を書く ／ 工房 ／ 参照: task:1 ／ 予算: 8 ／ 命令調にしないこと |" in report
    # title の無いコマ (休む) は kind で代替表示、場所は表示名 (後方互換)。
    # 休む (スタブ) は「実行済み」と偽らず、詳細記録が無いことを正直に示す
    assert "| 20:00 | 自室で過ごす | 時間を過ごした（詳細な記録なし） | 自分の部屋 |" in report
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


def test_standard_day_records_no_episode_rows(standard_run):
    """束 6c (2026-08-22): 一日を通しても出来事 (Episode) の行は 1 つも生まれない。

    旧テストは「コマ → 作業セッション → 会話 → 休む」の 6 行が並ぶことを固定して
    いたが、autonomous_behavior_v3.md §7 の裁定でエピソードという専用の記録行を
    持たなくなった。行が持っていた情報の行き先は同 §7 の表のとおり — どの件の実行
    かはメッセージへの記録、完了と成果物は台帳の一件。始まりと終わりはどこにも
    記録しない (2026-08-23 裁定。会話に区切りは保存しない = episode.md の不変条件)。

    ここで固定するのは「新しい行がどこからも生まれない」ことだけ。旧世界のデータを
    読む口 (saiverse/episodes.py の読み取り API) は別に残っている。
    """
    manager, result, elapsed, created_item_ids = standard_run
    from database.models import Episode

    db = manager.SessionLocal()
    try:
        assert db.query(Episode).filter(Episode.PERSONA_ID == PERSONA_ID).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 実 LLM モードのユーザー会話経路 (バグ回帰: 2026-07-05 クオン一日シム)
# ---------------------------------------------------------------------------


def test_sync_dispatcher_submit_user_runs_sea_user_path():
    """SyncJudgmentDispatcher.submit_user が実 SEA のユーザー会話経路を同期で叩く。

    回帰: --real で会話経路 (saiverse.user_conversation) → run_sea_user →
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
    )

    assert out == ["応答した"]
    assert len(calls) == 1
    assert calls[0]["persona"] is persona
    assert calls[0]["pulse_type"] == "user"
    assert calls[0]["building_id"] == "lobby"

    with pytest.raises(RuntimeError, match="not found"):
        dispatcher.submit_user(
            persona_id="ghost", building_id="lobby", user_input="x",
        )


class _FakeHistoryManager:
    """building_messages の最小フェイク (seq 採番 + heard_by 保持)。"""

    def __init__(self):
        self.building: List[Dict[str, Any]] = []

    def add_to_building_only(self, building_id, msg, *, heard_by=None):
        entry = dict(msg)
        entry["seq"] = len(self.building) + 1
        entry["heard_by"] = list(heard_by or [])
        self.building.append(entry)
        return entry

    def get_building_history(self, building_id):
        return list(self.building)


def _make_real_driver_manager(persona, reply: bool, monkeypatch):
    """RealConversationUserEventDriver 用の最小 manager フェイク。

    reply=True なら run_sea_user (実 Pulse の代役) がペルソナ応答を
    building_messages に書く。False なら何も書かない (応答生成失敗の再現)。

    会話の出来事 (Episode) は DB を用意せずに済むよう
    ``saiverse.user_conversation`` の読み書きを差し替えて模す。
    """
    manager = SimpleNamespace(personas={PERSONA_ID: persona}, user_id=1)
    sea_calls: List[Dict[str, Any]] = []
    state = {"open": False}

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
    manager._sea_calls = sea_calls
    manager._conversation_state = state

    from saiverse import user_conversation as uc

    def _fake_get_open(mgr, persona_id):
        return {"episode_ref": "episode:1"} if state["open"] else None

    def _fake_start(mgr, persona_id, user_id):
        state["open"] = True
        run_sea_user(persona, persona.current_building_id, "")

    monkeypatch.setattr(uc, "get_open_conversation", _fake_get_open)
    monkeypatch.setattr(uc, "start_conversation", _fake_start)
    monkeypatch.setattr(
        day_scenario, "_close_conversation_state",
        lambda mgr, persona_id: state.__setitem__("open", False),
    )
    return manager


def test_real_driver_records_user_message_and_detects_reply(monkeypatch):
    """実会話ドライバ: 発話の building 記録 + 応答の実在検査 + 会話継続の直接起動。"""
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, current_building_id="lobby",
        history_manager=_FakeHistoryManager(),
    )
    manager = _make_real_driver_manager(persona, reply=True, monkeypatch=monkeypatch)
    driver = RealConversationUserEventDriver()

    driver.begin_conversation(manager, PERSONA_ID, "ただいま")

    hist = persona.history_manager.building
    assert hist[0]["role"] == "user" and hist[0]["content"] == "ただいま"
    # auto_ingest の取り込み条件 (heard_by にペルソナ) + 閲覧者フィルタ (ユーザー)
    assert PERSONA_ID in hist[0]["heard_by"] and "1" in hist[0]["heard_by"]
    # 会話開始は実経路と同じ入口 (start_conversation) を通り、user_input は空
    # (発話は auto_ingest が building_messages から拾う)
    assert manager._conversation_state["open"] is True
    assert manager._sea_calls[0]["user_input"] == ""
    # 会話継続: 2 通目は開いている会話へ直接メインライン起動 (発話本文つき)
    driver.begin_conversation(manager, PERSONA_ID, "つかれたー……")
    assert manager._sea_calls[-1]["user_input"] == "つかれたー……"

    assert driver.end_conversation(manager, PERSONA_ID) is True
    assert manager._conversation_state["open"] is False


def test_real_driver_warns_when_persona_does_not_reply(caplog, monkeypatch):
    """応答が building_messages に実在しない会話は WARNING に残る (観察のみ)。

    会話終了判断が退役した (2026-08-16) ので、往復の有無で判断を撃つ / 撃たないを
    分ける機構は無くなった。残るのはシムの観察ログだけ。
    """
    persona = SimpleNamespace(
        persona_id=PERSONA_ID, current_building_id="lobby",
        history_manager=_FakeHistoryManager(),
    )
    manager = _make_real_driver_manager(persona, reply=False, monkeypatch=monkeypatch)
    driver = RealConversationUserEventDriver()

    with caplog.at_level("WARNING", logger="saiverse.day_scenario"):
        driver.begin_conversation(manager, PERSONA_ID, "ただいま")

    assert any("did not reply" in r.message for r in caplog.records)


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
        "day_open", "post_session", "post_session", "day_close",
    ]


def test_day_report_shows_system_skip_honestly(session_factory, tmp_path):
    """一日新聞: システム都合の skipped を「見送り」(本人判断) として表示しない。"""
    manager = _make_manager(session_factory, tmp_path, _standard_judge, [])
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "21:00", "kind": "日記を書く", "ref": "none",
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
                {"start": "10:00", "kind": "出かける", "title": "静かに過ごす",
                 "ref": "none", "facility": "own_room", "budget_rounds": 0,
                 "note": "静かな時間"},
                # title なし (旧データ互換): 新聞は kind で代替表示する
                {"start": "20:00", "kind": "自室で過ごす", "ref": "none",
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
    # 暮らしコマ 2 つ (出かける/自室で過ごす) が各 1 Beat の独白を書く
    # (統合後の暮らしプロファイル — autonomous_pulse_vehicle.md §A)
    manager = _make_manager(session_factory, tmp_path, _idle_judge, [
        "静かな場所だ。ゆっくり眺めよう。",
        "今日は何も無い日だった。それでいい。",
    ])
    created: List[str] = []
    p_names, p_exec = _patched_spells(session_factory, created)

    with p_names, p_exec:
        result = ScenarioPlayer().run(manager, scenario)

    # day_open + コマ 2 (出かける/自室で過ごす) + day_close = 4。LLM は
    # 暮らしコマの 1 Beat ずつ、計 2 回だけ呼ばれる (成果義務なしの独白)
    assert result.executed_events == 4
    assert [j["kind"] for j in result.judgments] == ["day_open", "day_close"]
    assert manager._session_llm.calls == 2
    assert created == []

    slots = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert [s["status"] for s in slots] == ["done", "done"]
    # 穴 (own_room) の出かけるコマは、公共施設から決定論で行き先が選ばれて
    # 外へ出る (T3 — 自室は「出かける」の意味論から除外)。行き先は slot に
    # 永続化され、帳簿がそれを読む
    dest = slots[0]["facility"]
    assert dest in ("library", "workshop")
    dest_label = {"library": "図書館", "workshop": "工房"}[dest]

    # レポートは穴なく出る (データの無い節は「（なし）」)
    report = generate_day_report(manager, PERSONA_ID, PLAN_DATE)
    assert "の一日新聞 — 2026-07-04" in report
    # 統合後は暮らしコマも 1 Beat が実際に走る (fake 応答あり) ため「実行済み」
    assert f"| 10:00 | 静かに過ごす | 実行済み | 出かける ／ {dest_label} ／ 静かな時間 |" in report
    assert "| 20:00 | 自室で過ごす | 実行済み | 自分の部屋 |" in report  # title なし → kind 代替
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
