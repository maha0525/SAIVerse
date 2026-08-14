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
- day_close: 今日触れた欲求のみの desire_reviews enum、tomorrow_memo が
  翌朝 day_open の状況テキストに現れる (連結。生の実績表 = 旧 day_digest は
  再供給しない、2026-07-29)、apply_desire_reviews の適用
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

from database.models import (
    AI,
    Base,
    City,
    ExecutionOutboxItem,
    PersonaTask,
    PersonaTaskHistory,
    User,
)
from saiverse import clock
from saiverse import day_plan
from saiverse import judgment_points as jp
from saiverse.event_scheduler import EventScheduler
from saiverse.persona_task_manager import (
    STAGE_CANDIDATE,
    PersonaTaskManager,
    TaskConflictError,
)
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
    """SAIMemory adapter の最小スタブ (append_persona_message の記録 +
    W1 Chunk C: get_messages_by_origin_episode の canned 応答)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []
        # get_messages_by_origin_episode が返す原本行 (テストが仕込む)
        self.transcript_rows: List[Dict[str, Any]] = []
        self.transcript_queries: List[str] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)
        # 実 adapter と同じく message id を返す (digest 直書き経路が使う)
        return f"m{len(self.messages)}"

    def get_messages_by_origin_episode(self, episode_ref):
        self.transcript_queries.append(episode_ref)
        return list(self.transcript_rows)


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
    return {"start": start, "kind": "自室で過ごす", "ref": "none",
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
    # 状況テキスト: 昨日の自分からのメモ・予算 (= その朝にしか意味がない情報)。
    # 静的な一覧 (バックログ・欲求・施設) は head へ移設済み
    # (docs/issues/judgment_static_lists_to_head.md、2026-07-30)。
    text = args["situation_text"]
    assert "明日は標本集の続きから" in text
    assert "日次予算: 40" in text
    assert "言葉の標本集" not in text, "バックログが状況テキストに再送されている"
    assert "- library:" not in text, "施設一覧が状況テキストに再送されている"
    assert "やりたいこと候補:" not in text, "欲求一覧が状況テキストに再送されている"

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
    無関係な task:1 に滑った。表示と enum の整合そのものを固定する。

    2026-07-30 の移設後、表示側は head の PurposeBacklogSection、enum 側は
    従来どおり判断点。**別々の場所になったからこそ**この整合は壊れうるので、
    検査は移設先を跨いで続ける。
    """
    from sea.head_pipeline.sections.purpose_backlog import PurposeBacklogSection

    jp.run_judgment_point(manager, PERSONA_ID, "day_open")
    args = manager.pulse_controller.submissions[0]["args"]
    slot = args["response_schema"]["properties"]["timetable"]["items"]
    ref_enum = set(slot["properties"]["ref"]["enum"])

    section = PurposeBacklogSection()
    ctx = SimpleNamespace(persona_id=PERSONA_ID, manager=manager)
    text = section.render(section.capture(ctx)).text

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


def test_head_backlog_refs_are_all_selectable(manager, task_refs):
    """head に並ぶ ref が、判断で選べる ref の集合と一致すること。

    2026-07-30 Codex 指摘 high2 の回帰。**新しく立てた関心を含む**ことが要点:
    会話終了判断の「新しい関心として立てる」で作られる Track は
    ``track_manager.create`` の既定 = unstarted で生まれる。これが選択肢から
    落ちると、立てたばかりの関心に翌朝コマを割り当てられない。コマの検証
    (sanitize_timetable) は元から生きた Track を受理していたので、狭かったのは
    選択肢の側だった。

    初回の修正は逆に head を狭めて揃えてしまい、この既存の欠陥を隠していた。
    集合の一致は「判断が指し示せるものすべて」へ揃える。
    """
    import re

    from sea.head_pipeline.sections.purpose_backlog import PurposeBacklogSection

    manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="立てたばかりの関心",
    )
    manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="進行中の関心",
        initial_status="running",
    )

    section = PurposeBacklogSection()
    text = section.render(
        section.capture(SimpleNamespace(persona_id=PERSONA_ID, manager=manager))
    ).text

    slot_ref_enum = set(jp.collect_slot_ref_enum(manager, PERSONA_ID))
    track_enum = set(jp.collect_pickable_track_refs(manager, PERSONA_ID))

    shown = set(re.findall(r"(?:track|task):\d+", text))
    assert shown, "head の一覧に ref が 1 つも無い (フィクスチャの前提が崩れた)"
    for ref in shown:
        assert ref in slot_ref_enum, f"head の {ref} がコマの ref enum に無い"
    shown_tracks = {r for r in shown if r.startswith("track:")}
    assert shown_tracks == track_enum, "head の Track と選べる Track が食い違う"

    # 立てたばかりの関心 (unstarted) も、進行中の関心も、両方が揃う
    assert "進行中の関心" in text
    assert "立てたばかりの関心" in text


def test_db_failure_while_building_args_fails_the_ledger_row(manager):
    """引数の組み立てで DB が落ちても、例外は入口まで漏れず席が終端化すること。

    2026-07-30 Codex 三巡目。判断が指し示せる Track の取得は握りつぶしを外した
    ので、DB 障害はここまで上がってくる。この関数の契約は「起動できなければ
    理由つきの結果 dict」で、呼び出し側 (on_event の direct fallback /
    schedule の backoff) はその戻り値で分岐する — 例外を素通しすると席が
    prepared のまま残り、呼び出し側の代替経路も回復 tick の再発火も両方が
    動きうる。
    """
    abandoned: list = []
    manager.execution_ledger = SimpleNamespace(
        mark_running=lambda eid: None,
        # 席の放棄は prepared 限定 CAS で行う (mark_failed は running を上書き
        # しうるので LLM 開始前の離脱には使えない)
        abandon_prepared=lambda eid, reason: (
            abandoned.append((eid, reason)) or True
        ),
    )

    def boom(*a, **k):
        raise RuntimeError("db is down")

    manager.track_manager = SimpleNamespace(list_for_persona=boom, get_running=boom)

    result = jp.run_judgment_point(
        manager, PERSONA_ID, "post_conversation", execution_id="exec-1",
    )

    assert result["submitted"] is False
    assert "args build failed" in result["reason"]
    assert result["outcome"] == jp.OUTCOME_ABORTED
    assert len(abandoned) == 1 and abandoned[0][0] == "exec-1"
    assert "db is down" in abandoned[0][1]
    # LLM は開始していない
    assert manager.pulse_controller.submissions == []


def test_seat_that_cannot_be_abandoned_is_reported_indeterminate(manager):
    """席を放棄できなかったら「起動できなかった」ではなく結末不明として返す。

    2026-07-30 Codex 四巡目。放棄に失敗した (= 別の claimant が走らせている /
    台帳が応答しない) のに submitted=False だけを返すと、呼び出し側の代替経路と
    回復 tick の再発火が両方走り、同じイベントが二度処理される。
    """
    manager.execution_ledger = SimpleNamespace(
        mark_running=lambda eid: None,
        abandon_prepared=lambda eid, reason: False,  # 既に他者の所有
    )
    manager.personas = {}  # ペルソナ未ロード = pre-dispatch の離脱

    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
    )
    assert result["submitted"] is False
    assert result["outcome"] == jp.OUTCOME_INDETERMINATE


def test_pre_dispatch_abort_releases_the_claimed_seat(manager):
    """claim 済みの席は、pre-dispatch のどの離脱経路でも放棄される。

    2026-07-30 Codex 四巡目 (指摘2)。ペルソナ未ロード / 現在地なし /
    pulse_controller なしは席を prepared のまま残していたので、回復 tick の
    再発火と呼び出し側の代替経路が二重に走りえた。
    """
    for setup, reason in (
        (lambda m: setattr(m, "personas", {}), "persona not loaded"),
        (lambda m: setattr(m.personas[PERSONA_ID], "current_building_id", None),
         "no current building"),
        (lambda m: setattr(m, "pulse_controller", None), "no pulse_controller"),
    ):
        abandoned: list = []
        manager.execution_ledger = SimpleNamespace(
            mark_running=lambda eid: None,
            abandon_prepared=lambda eid, r: (abandoned.append((eid, r)) or True),
        )
        # フィクスチャを毎回組み直す (直前のケースの破壊を引きずらない)
        persona = SimpleNamespace(
            persona_id=PERSONA_ID, current_building_id="alice_room",
            private_room_id="alice_room",
        )
        manager.personas = {PERSONA_ID: persona}
        manager.pulse_controller = FakePulseController()
        setup(manager)

        result = jp.run_judgment_point(
            manager, PERSONA_ID, "on_event", {"event_text": "来客"},
            execution_id="exec-1",
        )
        assert result["submitted"] is False
        assert result["reason"] == reason
        assert result["outcome"] == jp.OUTCOME_ABORTED
        assert abandoned == [("exec-1", reason)]


def test_direct_fallback_allowed_defaults_to_refusing(caplog):
    """代替経路の可否表 (2026-08-14 F3)。**結末の無い結果は拒否**。

    「submitted=False かつ indeterminate でなければ起動できなかった」と読む形は、
    判断が走った後の失敗まで「起動できなかった」に含めてしまう。可否は結末の
    語彙で明示し、書き忘れ (結末なし) は拒否側 + WARNING に倒す。
    """
    allowed = jp.direct_fallback_allowed
    assert allowed({"submitted": False, "outcome": jp.OUTCOME_ABORTED}) is True
    assert allowed({"submitted": False, "outcome": jp.OUTCOME_NO_EFFECT}) is True
    assert allowed({"submitted": False, "outcome": jp.OUTCOME_RAN}) is False
    assert allowed({"submitted": False, "outcome": jp.OUTCOME_INDETERMINATE}) is False

    with caplog.at_level("WARNING", logger="saiverse.judgment_points"):
        assert allowed({"submitted": False, "reason": "未知の経路"}) is False
    assert any("no outcome" in r.message for r in caplog.records)


def test_runtime_exception_outcome_follows_the_ledger_terminal(manager):
    """実行時例外の結末は「台帳へ何を書けたか」から導く。

    副作用ゼロ確定 (LLM エラー) → failed → no_effect (代替経路 OK)。
    それ以外 → unknown → ran (代替経路 NG)。台帳遷移自体が失敗したら
    indeterminate (書けなかったことを成功と読まない)。
    """
    from llm_clients.exceptions import LLMError

    def _run(exc, ledger):
        manager.execution_ledger = ledger
        manager.pulse_controller = SimpleNamespace(
            submit_meta_judgment=lambda **kw: (_ for _ in ()).throw(exc),
        )
        return jp.run_judgment_point(
            manager, PERSONA_ID, "on_event", {"event_text": "来客"},
            execution_id="exec-1",
        )

    marks: list = []
    ok_ledger = SimpleNamespace(
        mark_running=lambda eid: None,
        try_mark_running=lambda eid: True,
        mark_failed=lambda eid, r: marks.append(("failed", r)),
        mark_unknown=lambda eid, r: marks.append(("unknown", r)),
        get_execution=lambda eid: {"status": "prepared"},
    )
    assert _run(LLMError("down"), ok_ledger)["outcome"] == jp.OUTCOME_NO_EFFECT
    assert marks[-1][0] == "failed"
    assert _run(RuntimeError("boom"), ok_ledger)["outcome"] == jp.OUTCOME_RAN
    assert marks[-1][0] == "unknown"

    def _boom(*a, **k):
        raise RuntimeError("ledger down")

    dead_ledger = SimpleNamespace(
        mark_running=lambda eid: None, try_mark_running=lambda eid: True,
        mark_failed=_boom, mark_unknown=_boom,
        get_execution=lambda eid: {"status": "prepared"},
    )
    assert _run(LLMError("down"), dead_ledger)["outcome"] == jp.OUTCOME_INDETERMINATE


def _ledger_stub(**overrides):
    """run_judgment_point が触る台帳 API だけを持つスタブ。"""
    base = dict(
        mark_running=lambda eid: None,
        try_mark_running=lambda eid: True,
        mark_failed=lambda eid, r: None,
        mark_unknown=lambda eid, r: None,
        get_execution=lambda eid: {"status": "applied"},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_unreadable_ledger_status_is_not_treated_as_success(manager):
    """台帳 status が読めないとき、成功へ倒さない (2026-08-14 Codex 二巡目)。

    旧実装は「legacy success verdict」として submitted=True のまま通し、結末も
    付かなかった — 呼び出し側は代替経路の可否すら判定できない。finalize の証跡が
    どこにも無いなら indeterminate。
    """
    def _boom(eid):
        raise RuntimeError("ledger down")

    manager.execution_ledger = _ledger_stub(get_execution=_boom)
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
    )
    assert result["submitted"] is False
    assert result["outcome"] == jp.OUTCOME_INDETERMINATE
    assert jp.direct_fallback_allowed(result) is False


def test_unreadable_ledger_status_accepts_the_captured_finalize_event(manager):
    """台帳が読めなくても、callback が finalize を捕まえていれば成功でよい。

    judgment_applied イベントは台帳とは独立した一次証跡 — 台帳の読み取り失敗
    だけを理由に、実際に下された判断を捨てない。
    """
    def _boom(eid):
        raise RuntimeError("ledger down")

    class _FinalizeEmitting(FakePulseController):
        def submit_meta_judgment(self, persona_id, building_id, meta_playbook,
                                 args=None, event_callback=None):
            super().submit_meta_judgment(
                persona_id, building_id, meta_playbook, args, event_callback,
            )
            if event_callback:
                event_callback({
                    "type": "judgment_applied", "kind": "on_event",
                    "extras": ["reaction=engage_now"],
                })

    manager.execution_ledger = _ledger_stub(get_execution=_boom)
    manager.pulse_controller = _FinalizeEmitting()
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
    )
    assert result["submitted"] is True
    assert result["applied_events"][0]["extras"] == ["reaction=engage_now"]


def test_post_session_without_session_result_raises(manager):
    """起きていないセッションの裁定を走らせない (契約違反は畳まず上げる)。

    session_result 無しで組み立てると、成果物ゼロ・0 ラウンド・終了理由不明の
    「起きていないセッション」を前提に裁定と時間割変更が永続化される。
    """
    with pytest.raises(ValueError, match="session_result"):
        jp.run_judgment_point(manager, PERSONA_ID, "post_session", {})


def test_contract_violation_raises_even_when_persona_is_missing(manager):
    """契約検査は環境の状態より前 — 配線ミスが環境の問題に化けて隠れない。"""
    manager.personas = {}
    with pytest.raises(ValueError, match="event_text"):
        jp.run_judgment_point(manager, PERSONA_ID, "on_event", {})


def test_new_track_from_conversation_is_selectable_for_timetable(manager):
    """会話終了判断が立てた関心に、翌朝コマを割り当てられること。

    judgment_finalize は initial_status 未指定で Track を作る (= unstarted)。
    その Track がコマの ref enum に載らなければ、立てた関心に時間を割けない。
    """
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="会話から立てた関心",
    )
    ref = f"track:{manager.track_manager.get(track_id).short_id}"
    assert ref in jp.collect_slot_ref_enum(manager, PERSONA_ID)

    # 検証側 (sanitize) も同じ Track を受理する = enum と検証が一致
    slots, warnings = jp.sanitize_timetable(manager, PERSONA_ID, [{
        "start": "10:00", "kind": "随筆を書く", "title": "関心に取り組む",
        "ref": ref, "facility": "own_room", "note": "",
    }])
    assert warnings == []
    assert slots and slots[0]["ref"] == ref


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
            {"start": "09:00", "kind": "調べる", "ref": "task:1",
             "facility": "library", "budget_rounds": 5, "note": "記事の続き"},
            {"start": "14:00", "kind": "随筆を書く", "ref": "task:99",  # 実在しない
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
            {"start": "9時", "kind": "調べる", "ref": "task:1",
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


def test_day_open_finalize_all_excluded_keeps_existing_plan_atomically(
    manager, task_refs, finalize_mod, tmp_path,
):
    """A1 (原子的全置換): 提出コマがライフ範囲で全除外 → replace_day_plan が
    ValueError。旧 plan・旧予約とも一切変更されず、エコーは「既存を維持」と
    実状態に一致する (旧実装は「先に cancel → save が raise」で予約だけ孤児化し、
    報告も「編成されていません」と実状態に反していた)。"""
    day_plan.save_lives(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "07:00", "end": "22:00", "budget_pulses": 20, "mode": "free"},
    ])
    # 既存 plan (09:00 のコマ) を編成して予約まで作る
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "09:00", "kind": "調べる", "ref": task_refs["task"],
         "facility": "library", "budget_rounds": 5, "note": "旧予定"},
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    before = day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE)
    slot0_key = day_plan._slot_key(PERSONA_ID, PLAN_DATE, before[0]["id"])
    assert manager.event_scheduler.has_key(slot0_key)

    output = {
        "monologue": "全部夜更かしの予定にしてしまった。",
        # 23:00 は就寝 (22:00) より後 — 丸めようが無く全除外
        "timetable": [_rest_slot("23:00")],
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "daily_budget_rounds": 40})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="day_open",
            judgment_context=ctx, situation_text="[起床判断] ...",
        )

    # 旧 plan / 旧予約とも不変
    assert day_plan.load_day_plan(manager, PERSONA_ID, PLAN_DATE) == before
    assert manager.event_scheduler.has_key(slot0_key)
    # timetable 由来では applied=True にならない (promotions も無い)
    assert "applied=True" not in summary
    # エコーが実状態 (維持) に一致
    content = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "既存の時間割を維持しました" in content
    assert "編成されていません" not in content


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
            {"start": "07:30", "kind": "調べる", "ref": task_refs["task"],
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


def test_post_session_done_normalizes_persona_facing_artifact_ref(
    manager, ptm, task_refs, finalize_mod, tmp_path
):
    """ペルソナが書く ``item:N`` を生 Item ID に正規化してから接地検証する。

    セッションの成果物一覧は生 Item ID を持つが、ペルソナが目にする成果物参照は
    世界の表示語彙 ``item:N``。書式のまま突き合わせると、実際に作った成果物が
    「やったフリ」として棄却され、タスクが完了しないまま WARNING だけが残る
    (2026-07-23、document_create の戻り値を item:N に変えた際に開いた穴)。
    """
    raw_id = "e6164a23-4c43-40de-8ab2-9ff428fa6f29"
    manager.resolve_item_ref_for_persona = (
        lambda persona_id, ref: raw_id if ref == "item:404" else ref
    )
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    output = {
        "monologue": "設計書を書けた。",
        "task_verdict": {"status": "done", "artifact_ref": "item:404",
                         "desk_memo": "設計書完成"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [raw_id],
                      "task_ref": "task:1", "track_id": track_id})
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    task = ptm.get_task(ptm.resolve_task_ref(PERSONA_ID, "task:1"), persona_id=PERSONA_ID)
    assert task["status"] == "completed", (
        "item:N 形式の成果物参照が正規化されず、本物の成果物が棄却された"
    )
    assert task["artifact_refs"] == [raw_id], "台帳には生 Item ID で刻むこと"


def test_post_session_unresolvable_artifact_ref_still_rejected(
    manager, ptm, task_refs, finalize_mod, tmp_path, caplog
):
    """解決できない参照は素通しした上で、接地検証で棄却されること。

    正規化はあくまで前処理であって検証ではない。解決器が例外を投げても
    判断全体を落とさず、存在しない成果物は後段の照合で落ちる。
    """
    def _boom(persona_id, ref):
        raise RuntimeError("resolver exploded")

    manager.resolve_item_ref_for_persona = _boom
    track_id = manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="調べ物",
    )
    output = {
        "monologue": "できたはず。",
        "task_verdict": {"status": "done", "artifact_ref": "item:999",
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
    assert task["status"] == "pending"
    assert task["artifact_refs"] == []


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
        {"start": "14:00", "kind": "調べる", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "調べもの"},
        _rest_slot("20:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)
    assert manager.event_scheduler.pending_count() == 2  # index 1, 2

    output = {
        "monologue": "残りは 16 時に一本にまとめる。",
        "task_verdict": {"status": "continue", "desk_memo": "続きから"},
        "remaining_timetable": [
            {"start": "16:00", "kind": "調べる", "ref": "task:1",
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
    # 新コマ (16:00) は予約済み、旧コマの残骸は cancel 済み (予約は 1 件だけ)
    assert manager.event_scheduler.has_key(
        day_plan._slot_key(PERSONA_ID, PLAN_DATE, slots[1]["id"])
    )
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
        {"start": "13:30", "kind": "随筆を書く", "ref": "task:1",
         "facility": "workshop", "budget_rounds": 6, "note": "済んだコマ",
         "status": "done"},
        _rest_slot("17:00"),
    ])
    day_plan.schedule_day_plan(manager, PERSONA_ID, PLAN_DATE)

    output = {
        "monologue": "13:30 のコマは対象を直してやり直す。",
        "remaining_timetable": [
            {"start": "13:30", "kind": "随筆を書く", "ref": "task:2",
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
            {"start": "13:00", "kind": "随筆を書く", "ref": "task:1",
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
            {"start": "15:00", "kind": "随筆を書く", "ref": "task:99",  # 実在しない
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
        {"start": "10:00", "kind": "随筆を書く", "ref": "task:1",
         "facility": "workshop", "budget_rounds": 4, "note": "完了済みを指す"},
        {"start": "12:00", "kind": "随筆を書く", "ref": "task:2",
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
    # track_ref の選択材料 (どの track:N が何か) と既存タスク・欲求の一覧は
    # head の PurposeBacklogSection に常駐する。一日に何度も走るこの判断が、
    # 同じ台帳を毎回貼り直さないことを固定する (2026-07-30 移設)。
    assert "調べ物" not in text
    assert "言葉の標本集" not in text
    # 一覧が消えても「重複して作るな」という要求そのものは残る
    assert "重ねて作らないでください" in text

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
        {"start": "14:00", "kind": "調べる", "ref": "task:1",
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
            {"start": "16:00", "kind": "調べる", "ref": "task:1",
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
        {"start": "06:30", "kind": "調べる", "ref": "task:1",
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
    assert inserted["kind"] == "調べる"           # 元コマから引き継ぎ
    assert inserted["ref"] == "task:1"
    assert inserted["facility"] == "library"    # 元コマから引き継ぎ
    assert inserted["budget_rounds"] == 5       # 元コマから引き継ぎ
    assert manager.event_scheduler.has_key(
        day_plan._slot_key(PERSONA_ID, PLAN_DATE, inserted["id"])
    )
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


def test_on_event_situation_shows_open_episode_activity(manager, task_refs):
    """「いまの活動」は開いている出来事 (会話以外) から導出する
    (track_retirement.md §7.4 — 旧 running Track 読みの付け替え)。"""
    from saiverse import episodes

    episodes.open_episode(
        manager, PERSONA_ID, episodes.KIND_WORK_SESSION,
        building_id="alice_room", participants=[PERSONA_ID],
        meta={"title": "標本集の整理"},
    )
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    assert "標本集の整理" in result["args"]["situation_text"]


def test_on_event_situation_activity_survives_stale_open_cache(manager, task_refs):
    """回帰 (2026-08-14 Codex 指摘 F2): 「いまの活動」は仲裁の判定と同じ集合
    (開いている会話以外の出来事) を DB から直に引く。

    層0タグ用の open キャッシュに stale な会話 dict が残っていると、
    「最後に開いた 1 件」を読む旧実装は会話を見て打ち切り、実際には開いている
    作業セッションを「手すきです」と偽って LLM へ渡していた。
    """
    from saiverse import episodes

    episodes.open_episode(
        manager, PERSONA_ID, episodes.KIND_WORK_SESSION,
        building_id="alice_room", participants=[PERSONA_ID],
        meta={"title": "標本集の整理"},
    )
    episodes._cache_set_open(
        manager, PERSONA_ID,
        {"episode_id": "stale", "kind": episodes.KIND_CONVERSATION},
    )
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    situation_text = result["args"]["situation_text"]
    assert "標本集の整理" in situation_text
    assert "手すきです" not in situation_text


def test_on_event_situation_running_track_alone_is_idle(manager, task_refs):
    """running Track が残っていても、開いている出来事が無ければ「手すき」。
    案 Y 以降 Track の running は残留するため、活動の根拠にしない。"""
    manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="autonomous", title="標本集の整理",
        initial_status="running",
    )
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    assert "手すきです" in result["args"]["situation_text"]
    assert "標本集の整理" not in result["args"]["situation_text"]


def _running_user_conversation_track(manager) -> str:
    """対ユーザー会話 Track を running で作る (会話の出来事は開かない)。"""
    return manager.track_manager.create(
        persona_id=PERSONA_ID, track_type="user_conversation",
        title="対 tester 会話", is_persistent=True, initial_status="running",
    )


def test_on_event_situation_says_in_conversation_when_episode_open(manager, task_refs):
    """開いている会話の出来事があるときだけ「ユーザーと会話中です」と伝える。"""
    from saiverse import episodes

    _running_user_conversation_track(manager)
    episodes.open_conversation_episode(
        manager, PERSONA_ID, building_id="alice_room",
        participants=[PERSONA_ID, "1"],
    )
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    assert "ユーザーと会話中です" in result["args"]["situation_text"]


def test_on_event_situation_not_in_conversation_when_episode_closed(manager, task_refs):
    """回帰 (2026-07-29): 会話が閉じていれば running のままでも「会話中」と言わない。

    案 Y (life.md §7) 以降、対ユーザー会話 Track は会話終了後も running のまま残る。
    種別で判定していた旧実装は、何日も前に終わった会話について「ユーザーと会話中です」
    をペルソナへ渡していた。「取り組んでいます」への読み替えもやはり嘘なので、
    手すき扱いのままにする。
    """
    _running_user_conversation_track(manager)  # 会話の出来事は開かない = 終了済み
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    situation_text = result["args"]["situation_text"]
    assert "ユーザーと会話中です" not in situation_text
    assert "対 tester 会話" not in situation_text
    assert "手すきです" in situation_text


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
            "start": "15:00", "kind": "調べる", "ref": "task:1",
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
            "start": "06:00", "kind": "調べる", "ref": "task:1",
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
            "start": "15:00", "kind": "調べる", "ref": "task:1",
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
        {"start": "09:00", "kind": "調べる", "ref": "task:1",
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
        {"start": "09:00", "kind": "調べる", "ref": "task:1",
         "facility": "library", "budget_rounds": 5, "note": "記事の続き",
         "status": "done"},
        {"start": "21:00", "kind": "日記を書く", "ref": task_refs["desire"],
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
    """詳細記録の無い done (presence スタブ) を「実行済み」として提示しない。

    2026-07-05 の実 LLM シム回帰 (異常 #4): スタブで何も実行していない暮らし
    コマが「→ 実行済み」と提示され、ペルソナが「食事の選定を行った」等、
    していない活動を自分の成果としてふりかえった (soft-confabulation)。
    """
    day_plan.save_day_plan(manager, PERSONA_ID, PLAN_DATE, [
        {"start": "12:00", "kind": "出かける", "ref": "none",
         "facility": "cafe", "budget_rounds": 0, "note": "昼の時間",
         "status": "done",
         "record_level": day_plan.RECORD_LEVEL_PRESENCE_ONLY},
        # マーカーの無い done (旧データ / セッション系) は従来どおり (後方互換)
        {"start": "14:00", "kind": "調べる", "ref": "task:1",
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
        {"start": "09:00", "kind": "調べる", "ref": "task:1",
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

    # meta_json に明日へのメモ・テーマ・報告種が保存される
    # (旧 day_digest の保存コピーは 2026-07-29 撤去 — 朝はメモだけを受け取る)
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert meta["tomorrow_memo"] == "朝一は標本集の整理から始める"
    assert meta["day_theme"] == "収集"
    assert meta["user_report_seeds"] == ["蒸留記事の要点を覚え書きにまとめた"]
    assert "day_digest" not in meta

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

    # --- 連結: 翌朝の起床判断が受け取るのは昨夜のメモだけ (生の実績表は
    # 再供給しない — 圧縮段の下流へ生材料を流さない、2026-07-29) -----------
    clock.advance_to(datetime(2026, 7, 5, 7, 0, 0))
    morning_text = jp.build_day_open_situation_text(manager, PERSONA_ID, {})
    assert "朝一は標本集の整理から始める" in morning_text  # tomorrow_memo
    assert "昨日のふりかえり" not in morning_text
    assert "今日の時間割（予定 → 実績）" not in morning_text


def test_day_close_finalize_empty_memo_warns(
    manager, task_refs, finalize_mod, tmp_path, caplog
):
    """メモ・テーマ・報告種が全て空のとき、偽の成功エコーを残さない。

    旧実装は day_digest の保存が常に updates を非空にしていたため
    「（今日のふりかえりを記録した）」が必ず出た。day_digest 撤去後に
    空 updates で同じエコーを出すと、引き継ぎ消失が成功の顔で残る
    (2026-07-29 Codex 指摘 high2)。"""
    output = {"monologue": "……", "tomorrow_memo": ""}
    ctx = json.dumps({"plan_date": PLAN_DATE, "touched_desire_refs": []})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="day_close", judgment_context=ctx,
            )
    assert any("tomorrow_memo が空" in r.message for r in caplog.records)
    meta = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)
    assert "tomorrow_memo" not in meta
    assert "day_digest" not in meta  # 保存コピーは撤去済み (2026-07-29)
    assert "applied=False" in summary  # 何も保存していないのに成功を名乗らない
    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert "今日のふりかえりを記録した" not in recorded["content"]
    assert "明日へのメモは残さなかった" in recorded["content"]


# ---------------------------------------------------------------------------
# 実行台帳フロー (W1 Chunk A / A7: 証跡ベース成功判定)
# ---------------------------------------------------------------------------


def _ledgered_execution(manager, session_factory, kind="day_close"):
    """実台帳を manager に取り付け、claim 済みの prepared 行を返す。"""
    from saiverse import execution_ledger as XL

    ledger = XL.ExecutionLedger(session_factory)
    manager.execution_ledger = ledger
    eid, runnable, _st = ledger.claim_execution(
        f"judgment.{kind}", idempotency_key=None, persona_id=PERSONA_ID,
    )
    assert runnable
    return ledger, eid


def test_run_judgment_embeds_execution_id_in_context(manager, session_factory):
    """execution_id が judgment_context JSON に同乗して finalize へ届く (D4)。"""
    ledger, eid = _ledgered_execution(manager, session_factory)
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["execution_id"] == eid
    args = manager.pulse_controller.submissions[0]["args"]
    ctx = json.loads(args["judgment_context"])
    assert ctx["execution_id"] == eid


def test_double_claim_loser_leaves_without_ledger_writes(
    manager, session_factory,
):
    """二重 claim の敗者は LLM を起動せず、台帳にも一切書かずに離脱する。

    claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重 claim は
    同じ execution_id を両方へ runnable として返す。勝者の一意化は
    try_mark_running (prepared 限定 CAS) — 敗者も submit へ進むと有料 LLM 呼び
    出しと finalize の適用が二重になる
    (docs/issues/judgment_seat_contention_and_event_loss.md ①)。
    """
    from saiverse import execution_ledger as XL

    ledger, eid = _ledgered_execution(manager, session_factory)
    # 勝者が先に席を取った (もう一人の claimant の try_mark_running 成功)
    assert ledger.try_mark_running(eid)

    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    assert result["reason"] == "seat taken by another claimant"
    # 勝者が同じ判断を処理するので、呼び出し側は代替経路を走らせない
    assert result["outcome"] == jp.OUTCOME_INDETERMINATE
    # 敗者は LLM を起動していない
    assert manager.pulse_controller.submissions == []
    # 勝者の running 台帳は無傷 (敗者は mark_failed 等を呼ばない)
    assert ledger.get_execution(eid)["status"] == XL.STATUS_RUNNING


def test_run_judgment_no_finalize_evidence_marks_unknown(
    manager, session_factory,
):
    """Chunk B: finalize が mark_applied を呼ばずにメタレーンが戻る (この
    FakePulseController は finalize を実行しない) → running のままの実行は
    unknown 化 + submitted=False (「成功 = finalize 完了の永続証跡」A7)。"""
    from saiverse import execution_ledger as XL

    ledger, eid = _ledgered_execution(manager, session_factory)
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    entry = ledger.get_execution(eid)
    assert entry["status"] == XL.STATUS_UNKNOWN
    assert "finalize evidence" in entry["error"]
    assert any(
        "no finalize evidence" in (e.get("message") or "")
        for e in result["errors"]
    )


def test_run_judgment_runtime_error_marks_unknown(manager, session_factory):
    """汎用例外 (LLM が動いたか不明) → mark_unknown + submitted=False (D4)。"""
    from saiverse import execution_ledger as XL

    ledger, eid = _ledgered_execution(manager, session_factory)

    def _boom(**kwargs):
        raise RuntimeError("meta lane down")

    manager.pulse_controller.submit_meta_judgment = _boom
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    assert result["execution_id"] == eid
    assert ledger.get_execution(eid)["status"] == XL.STATUS_UNKNOWN


def test_run_judgment_beat_gate_closed_marks_failed(manager, session_factory):
    """BeatGateClosedError (実行未開始・副作用ゼロ) → mark_failed (refire 安全)。"""
    from saiverse import execution_ledger as XL
    from sea.beat_gate import BeatGateClosedError

    ledger, eid = _ledgered_execution(manager, session_factory)

    def _gate(**kwargs):
        raise BeatGateClosedError(PERSONA_ID, "meta_judgment")

    manager.pulse_controller.submit_meta_judgment = _gate
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    entry = ledger.get_execution(eid)
    assert entry["status"] == XL.STATUS_FAILED
    assert "beat gate closed" in entry["error"]


def test_run_judgment_llm_error_marks_failed(manager, session_factory):
    """LLMError (出力なし = 適用前) → mark_failed (D4)。"""
    from llm_clients.exceptions import LLMError
    from saiverse import execution_ledger as XL

    ledger, eid = _ledgered_execution(manager, session_factory)

    def _llm_down(**kwargs):
        raise LLMError("provider exploded")

    manager.pulse_controller.submit_meta_judgment = _llm_down
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    entry = ledger.get_execution(eid)
    assert entry["status"] == XL.STATUS_FAILED
    assert "llm error" in entry["error"]


def test_run_judgment_cancelled_marks_unknown(manager, session_factory):
    """ExecutionCancelledException → mark_unknown (LLM が動いたか不明)。"""
    from saiverse import execution_ledger as XL
    from sea.cancellation import ExecutionCancelledException

    ledger, eid = _ledgered_execution(manager, session_factory)

    def _cancel(**kwargs):
        raise ExecutionCancelledException(interrupted_by="user")

    manager.pulse_controller.submit_meta_judgment = _cancel
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "day_close", execution_id=eid,
    )
    assert result["submitted"] is False
    assert ledger.get_execution(eid)["status"] == XL.STATUS_UNKNOWN


def test_run_judgment_without_ledger_degrades(manager):
    """台帳の無い manager では従来挙動 (execution_id=None、遷移なし)。"""
    result = jp.run_judgment_point(manager, PERSONA_ID, "day_close")
    assert result["submitted"] is True
    assert result["execution_id"] is None
    args = manager.pulse_controller.submissions[0]["args"]
    assert "execution_id" not in json.loads(args["judgment_context"])


# ---------------------------------------------------------------------------
# 実行台帳フロー (W1 Chunk B / A8: finalize の台帳化 = outbox 経由の判断行)
# ---------------------------------------------------------------------------


class LedgerFakeAdapter(FakeAdapter):
    """台帳配送 (append_ledger_message / push_ledger_perception) 対応のスタブ。

    - ``fail_append=True`` で配送失敗 (例外) を注入できる (A8 の再現)。
    - 冪等: 同じ outbox_id の再配送は積まない (実 adapter の契約を忠実化)。
    """

    def __init__(self):
        super().__init__()
        self.ledger_messages: List[tuple] = []  # (outbox_id, message dict)
        self.perceptions: List[Dict[str, Any]] = []
        self.fail_append = False

    def append_ledger_message(self, message, *, execution_id, outbox_id,
                              building_id=None, thread_suffix=None):
        if self.fail_append:
            raise RuntimeError("memory.db down (injected)")
        for oid, _m in self.ledger_messages:
            if oid == outbox_id:
                return f"msg-{oid}"
        self.ledger_messages.append((outbox_id, dict(message)))
        return f"msg-{outbox_id}"

    def push_ledger_perception(self, *, execution_id, outbox_id, kind, content,
                               reduce_key=None, salient=False, media=None,
                               metadata=None):
        if any(p["outbox_id"] == outbox_id for p in self.perceptions):
            return False
        self.perceptions.append({
            "outbox_id": outbox_id, "execution_id": execution_id,
            "kind": kind, "content": content, "reduce_key": reduce_key,
            "salient": salient,
        })
        return True


def _tracked_execution(manager, kind: str):
    """実ハンドラ付きの台帳を manager に取り付け、running まで進めた実行を返す。"""
    from saiverse import execution_ledger_wiring as xlw

    manager.personas[PERSONA_ID].sai_memory = LedgerFakeAdapter()
    ledger = xlw.build_execution_ledger(manager)
    manager.execution_ledger = ledger
    eid, runnable, _st = ledger.claim_execution(
        f"judgment.{kind}", idempotency_key=None, persona_id=PERSONA_ID,
    )
    assert runnable
    ledger.mark_running(eid)
    return ledger, eid


def _pending_outbox(session_factory, eid):
    db = session_factory()
    try:
        return (
            db.query(ExecutionOutboxItem)
            .filter(
                ExecutionOutboxItem.EXECUTION_ID == eid,
                ExecutionOutboxItem.STATUS == "pending",
            )
            .count()
        )
    finally:
        db.close()


def test_finalize_tracked_delivery_failure_keeps_applied_then_repairs(
    manager, task_refs, finalize_mod, tmp_path, session_factory,
):
    """A8: (1) 世界更新後の配送失敗 → applied 維持 + pending 残存 + 直書きなし、
    (2) 修復後 flush → 判断行 1 件だけ + completed、(3) 再 finalize は無効。"""
    from saiverse import execution_ledger as XL

    ledger, eid = _tracked_execution(manager, "on_event")
    adapter = manager.personas[PERSONA_ID].sai_memory
    adapter.fail_append = True

    output = {
        "monologue": "今すぐでなくていい。覚えておこう。",
        "reaction": {"type": "note_only", "memo": "新しい展示が始まったらしい"},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "掲示板の告知", "execution_id": eid})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )

    # (1) 世界更新 (event memo) は 1 回だけ適用され、summary は applied を維持
    assert "applied=True" in summary
    memos = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)["event_memos"]
    assert len(memos) == 1
    # 台帳は applied (「適用済み・記録待ち」)、判断行は pending に凍結
    entry = ledger.get_execution(eid)
    assert entry["status"] == XL.STATUS_APPLIED
    assert entry["result"]["kind"] == "on_event"
    assert entry["result"]["reaction"] == "note_only"
    assert _pending_outbox(session_factory, eid) == 1
    # 直書き経路は使われていない
    assert adapter.messages == []
    assert adapter.ledger_messages == []

    # (2) 配送修復後 flush → 判断行が 1 件だけ書かれ completed
    adapter.fail_append = False
    assert ledger.flush_pending_for_persona(PERSONA_ID) is True
    assert len(adapter.ledger_messages) == 1
    msg = adapter.ledger_messages[0][1]
    assert msg["line_role"] == "meta_judgment"
    assert msg["scope"] == "committed"
    assert "覚え書きに留める" in msg["content"]
    assert ledger.get_execution(eid)["status"] == XL.STATUS_COMPLETED

    # (3) 同じ execution_id で再 finalize → 世界更新は走らない
    with _persona_ctx(manager, tmp_path):
        summary2, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert "already finalized" in summary2
    memos = day_plan.load_plan_meta(manager, PERSONA_ID, PLAN_DATE)["event_memos"]
    assert len(memos) == 1
    assert len(adapter.ledger_messages) == 1


def test_finalize_untracked_with_ledger_but_no_execution_id(
    manager, task_refs, finalize_mod, tmp_path, session_factory,
):
    """A8 (4): execution_id が無い呼び出しは台帳があっても従来挙動 (直書き)。"""
    ledger, eid = _tracked_execution(manager, "on_event")
    adapter = manager.personas[PERSONA_ID].sai_memory
    output = {"monologue": "関係のない通知だ。", "reaction": {"type": "ignore"}}
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "無関係な通知"})  # execution_id なし
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert "applied=False" in summary
    assert len(adapter.messages) == 1  # 直書き
    assert adapter.ledger_messages == []
    assert _pending_outbox(session_factory, eid) == 0


# ---------------------------------------------------------------------------
# 実行台帳フロー (W1 Chunk B / A11: SpellOutcome)
# ---------------------------------------------------------------------------


def _desire_output(*titles):
    return {
        "monologue": "やりたいことが増えた。",
        "task_verdict": None,
        "new_desires": [
            {"type": "作る", "title": t, "source_quote": "会話の引用"}
            for t in titles
        ],
        "remaining_timetable": None,
    }


def test_spell_failure_is_not_committed_and_notifies(
    manager, task_refs, finalize_mod, tmp_path, session_factory, caplog,
):
    """A11 (1): tool 例外 → applied=False / 正準 /spell 行なし /
    judgment_apply_failure が outbox に積まれ perception へ届く。"""
    from saiverse import execution_ledger as XL

    ledger, eid = _tracked_execution(manager, "post_session")
    adapter = manager.personas[PERSONA_ID].sai_memory

    def broken_purpose_seed(**kwargs):
        raise RuntimeError("seed store down")

    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1", "execution_id": eid})
    import tools as tools_pkg
    with caplog.at_level("WARNING"):
        with patch.dict(tools_pkg.TOOL_REGISTRY,
                        {"purpose_seed": broken_purpose_seed}):
            with _persona_ctx(manager, tmp_path):
                summary, _, _ = finalize_mod.judgment_finalize(
                    judgment_output=_desire_output("語源メモ"),
                    kind="post_session", judgment_context=ctx,
                )

    assert "applied=False" in summary
    assert "spells=1/0/1" in summary
    assert "scope=discardable" in summary
    assert any("purpose_seed" in r.message for r in caplog.records)

    entry = ledger.get_execution(eid)
    assert entry["result"]["spells"] == {
        "attempted": 1, "succeeded": 0, "failed": 1,
    }
    assert entry["result"]["committed"] is False

    # 配送: 判断行 (成功形 /spell なし) + システム名義の適用失敗通知
    assert ledger.flush_pending_for_persona(PERSONA_ID) is True
    assert len(adapter.ledger_messages) == 1
    content = adapter.ledger_messages[0][1]["content"]
    assert "/spell" not in content
    assert adapter.ledger_messages[0][1]["scope"] == "discardable"
    assert len(adapter.perceptions) == 1
    notice = adapter.perceptions[0]
    assert notice["kind"] == "judgment_apply_failure"
    assert notice["reduce_key"] == "judgment_apply_failure:post_session"
    assert notice["salient"] is True
    assert "purpose_seed" in notice["content"]
    assert "世界には反映されていません" in notice["content"]


def test_spell_partial_success_separates_counts_and_lines(
    manager, task_refs, finalize_mod, tmp_path, session_factory,
):
    """A11 (2): 一部成功 → 成功分だけ正準 /spell 形、件数は attempted/succeeded/
    failed に分離、scope は committed (成功 spell あり)。"""
    ledger, eid = _tracked_execution(manager, "post_session")
    adapter = manager.personas[PERSONA_ID].sai_memory

    def flaky_purpose_seed(**kwargs):
        if kwargs.get("title") == "壊れる方":
            raise RuntimeError("boom")
        return "added"

    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1", "execution_id": eid})
    import tools as tools_pkg
    with patch.dict(tools_pkg.TOOL_REGISTRY,
                    {"purpose_seed": flaky_purpose_seed}):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=_desire_output("成功する方", "壊れる方"),
                kind="post_session", judgment_context=ctx,
            )

    assert "applied=True" in summary
    assert "spells=2/1/1" in summary
    assert "scope=committed" in summary
    entry = ledger.get_execution(eid)
    assert entry["result"]["spells"] == {
        "attempted": 2, "succeeded": 1, "failed": 1,
    }

    assert ledger.flush_pending_for_persona(PERSONA_ID) is True
    content = adapter.ledger_messages[0][1]["content"]
    assert content.count("/spell") == 1
    assert "成功する方" in content
    # 失敗した方は /spell 行に載らず、通知に載る
    spell_line = [ln for ln in content.splitlines() if ln.startswith("/spell")][0]
    assert "壊れる方" not in spell_line
    assert len(adapter.perceptions) == 1
    assert "壊れる方" in adapter.perceptions[0]["content"] or \
        "purpose_seed" in adapter.perceptions[0]["content"]


def test_spell_tool_not_found_untracked_warns_without_spell_line(
    manager, task_refs, finalize_mod, tmp_path, caplog,
):
    """A11 (3): tool 不在も failure。untracked では warnings ログのみ
    (perception 通知なし) で、判断行に成功形 /spell は残らない。"""
    output = _desire_output("行き場のない欲求")
    ctx = json.dumps({"plan_date": PLAN_DATE, "artifacts": [],
                      "task_ref": "task:1"})
    import tools as tools_pkg
    with caplog.at_level("WARNING"):
        with patch.dict(tools_pkg.TOOL_REGISTRY, {}, clear=True):
            with _persona_ctx(manager, tmp_path):
                summary, _, _ = finalize_mod.judgment_finalize(
                    judgment_output=output, kind="post_session",
                    judgment_context=ctx,
                )
    assert "applied=False" in summary
    assert "spells=1/0/1" in summary
    assert any("ツールが見つかりません" in r.message for r in caplog.records)
    recorded = manager.personas[PERSONA_ID].sai_memory.messages[0]
    assert recorded["scope"] == "discardable"
    assert "/spell" not in recorded["content"]


# ---------------------------------------------------------------------------
# W1 Chunk B / A9: complete_with_artifact (単一トランザクションの完了+接地)
# ---------------------------------------------------------------------------


def _task_history(session_factory, task_id):
    db = session_factory()
    try:
        rows = (
            db.query(PersonaTaskHistory)
            .filter(PersonaTaskHistory.task_id == task_id)
            .order_by(PersonaTaskHistory.created_at.asc(),
                      PersonaTaskHistory.id.asc())
            .all()
        )
        return [
            (r.event_type, json.loads(r.payload) if r.payload else {})
            for r in rows
        ]
    finally:
        db.close()


def test_complete_with_artifact_single_commit(
    manager, ptm, task_refs, session_factory,
):
    """A9 (1): 正常系 — 単一 commit で completed + artifact + 履歴 2 件。"""
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    task = ptm.complete_with_artifact(
        task_id, "item-abc", persona_id=PERSONA_ID, actor="judgment_post_session",
        execution_id="exec-1", reason="session verdict: done",
    )
    assert task["status"] == "completed"
    assert task["artifact_refs"] == ["item-abc"]
    assert task["completed_at"] is not None
    assert task["stage"] == "completed"

    history = _task_history(session_factory, task_id)
    events = {(e, p.get("execution_id"), p.get("via")) for e, p in history
              if e in ("update_task_status", "append_artifact_ref")}
    # 2 行とも同一 commit のため created_at が同時刻 — 順序は問わない
    assert events == {
        ("update_task_status", "exec-1", "complete_with_artifact"),
        ("append_artifact_ref", "exec-1", "complete_with_artifact"),
    }


def test_complete_with_artifact_is_atomic_on_failure(
    manager, ptm, task_refs, session_factory, monkeypatch,
):
    """A9 (2): commit 前の例外で全項目未変更 (completed になっていない)。"""
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    original = PersonaTaskManager._insert_history

    def failing_insert(self, db, *, task_id, step_id, event_type, payload, actor):
        if event_type == "append_artifact_ref":
            raise RuntimeError("history table down (injected)")
        return original(self, db, task_id=task_id, step_id=step_id,
                        event_type=event_type, payload=payload, actor=actor)

    monkeypatch.setattr(PersonaTaskManager, "_insert_history", failing_insert)
    with pytest.raises(RuntimeError):
        ptm.complete_with_artifact(
            task_id, "item-abc", persona_id=PERSONA_ID, actor="t",
            execution_id="exec-1",
        )
    monkeypatch.undo()

    task = ptm.get_task(task_id, persona_id=PERSONA_ID)
    assert task["status"] == "pending", "部分 commit で completed が確定している"
    assert task["artifact_refs"] == []
    assert task["completed_at"] is None
    history = _task_history(session_factory, task_id)
    assert all(e not in ("update_task_status", "append_artifact_ref")
               for e, _p in history)


def test_complete_with_artifact_repairs_same_execution(
    manager, ptm, task_refs, session_factory,
):
    """A9 (3): 同一 execution の補修 — completed + artifact 空の状態から
    artifact だけ追記。全部済みなら no-op 成功。"""
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.complete_with_artifact(
        task_id, "item-abc", persona_id=PERSONA_ID, actor="t",
        execution_id="exec-1",
    )
    # 旧 2 連 commit 時代の座礁状態を再現: artifact だけ剥がす
    db = session_factory()
    try:
        row = db.query(PersonaTask).filter(PersonaTask.id == task_id).first()
        row.artifact_refs = None
        db.commit()
    finally:
        db.close()

    repaired = ptm.complete_with_artifact(
        task_id, "item-abc", persona_id=PERSONA_ID, actor="t",
        execution_id="exec-1",
    )
    assert repaired["status"] == "completed"
    assert repaired["artifact_refs"] == ["item-abc"]
    history = _task_history(session_factory, task_id)
    repair_rows = [(e, p) for e, p in history if p.get("repair")]
    assert [e for e, _p in repair_rows] == ["append_artifact_ref"]

    # 全部済み → no-op 成功 (履歴も増えない)
    before = len(_task_history(session_factory, task_id))
    again = ptm.complete_with_artifact(
        task_id, "item-abc", persona_id=PERSONA_ID, actor="t",
        execution_id="exec-1",
    )
    assert again["artifact_refs"] == ["item-abc"]
    assert len(_task_history(session_factory, task_id)) == before


def test_complete_with_artifact_rejects_other_execution(
    manager, ptm, task_refs,
):
    """A9 (4): 別 execution / execution_id なしからの再 done は棄却。"""
    task_id = ptm.resolve_task_ref(PERSONA_ID, "task:1")
    ptm.complete_with_artifact(
        task_id, "item-abc", persona_id=PERSONA_ID, actor="t",
        execution_id="exec-1",
    )
    with pytest.raises(TaskConflictError):
        ptm.complete_with_artifact(
            task_id, "item-xyz", persona_id=PERSONA_ID, actor="t",
            execution_id="exec-2",
        )
    with pytest.raises(TaskConflictError):
        ptm.complete_with_artifact(
            task_id, "item-xyz", persona_id=PERSONA_ID, actor="t",
        )
    task = ptm.get_task(task_id, persona_id=PERSONA_ID)
    assert task["artifact_refs"] == ["item-abc"]


# ---------------------------------------------------------------------------
# W1 Chunk C / D9: digest 統合 (post_session が原本を見て digest を書く)
# ---------------------------------------------------------------------------


def _ws_episode(manager):
    """work_session の出来事を開いて閉じる (digest_ref=None のまま)。"""
    from saiverse import episodes as ep_mod
    ep = ep_mod.open_episode(
        manager, PERSONA_ID, ep_mod.KIND_WORK_SESSION, building_id="alice_room",
    )
    ep_mod.close_episode(manager, PERSONA_ID, ep["episode_ref"])
    return ep["episode_ref"]


def _ws_meta_fixture():
    return {
        "task_ref": "task:1", "artifacts": [], "rounds_used": 4,
        "budget_rounds": 8, "ended_reason": "finished",
        "started_at": "2026-07-04T10:00:00", "ended_at": "2026-07-04T10:30:00",
    }


def test_post_session_schema_requires_digest(manager, task_refs):
    ctx = {"session_result": _session_result(artifacts=[]), "task_ref": "task:1"}
    result = jp.run_judgment_point(manager, PERSONA_ID, "post_session", ctx)
    schema = result["args"]["response_schema"]
    _assert_no_additional_properties(schema)
    assert "digest" in schema["properties"]
    assert schema["properties"]["digest"]["type"] == "string"
    assert "digest" in schema["required"]
    # 指示部に digest の出典の規律 (実際に起きたことだけ) がある
    assert "digest 欄には" in result["args"]["situation_text"]


def test_post_session_transcript_is_call_local(manager, task_refs):
    """原本は situation_text だけに載る (D9-2/3)。

    - situation_text: 「セッションの記録 (原本):」+ 原本全文
    - paired_situation_text (保存用): episode 参照 + /spell episode_read の
      一行のみ — 原本を含まない (コールローカル注入)
    - ws_meta / episode_ref が judgment_context で finalize まで届く
    """
    episode_ref = _ws_episode(manager)
    adapter = manager.personas[PERSONA_ID].sai_memory
    adapter.transcript_rows = [
        {"role": "assistant",
         "content": (
             "本文を書く。\n"
             "/spell name='document_create' args={\"title\": \"下書き\"}"
         ),
         "created_at": int(BASE.timestamp())},
        {"role": "system",
         "content": "文書「下書き」を作成しました。",
         "created_at": int(BASE.timestamp())},
    ]
    ctx = {"session_result": _session_result(episode_ref=episode_ref),
           "task_ref": "task:1", "budget_rounds": 8}
    result = jp.run_judgment_point(manager, PERSONA_ID, "post_session", ctx)

    args = result["args"]
    text = args["situation_text"]
    assert "セッションの記録 (原本):" in text
    assert "本文を書く。" in text
    assert "文書「下書き」を作成しました。" in text
    assert "ダイジェスト:" not in text  # 旧欄は廃止
    assert adapter.transcript_queries == [episode_ref]

    jctx = json.loads(args["judgment_context"])
    paired = jctx["paired_situation_text"]
    assert episode_ref in paired
    assert "episode_read" in paired
    assert "本文を書く。" not in paired          # 原本は保存側に載らない
    assert "文書「下書き」" not in paired

    # digest 配送の材料が finalize まで届く
    assert jctx["episode_ref"] == episode_ref
    assert jctx["ws_meta"]["rounds_used"] == 4
    assert jctx["ws_meta"]["budget_rounds"] == 8
    assert jctx["ws_meta"]["ended_reason"] == "finished"


def test_post_session_transcript_unavailable_is_explicit(manager, task_refs):
    """原本が引けない (0 件 / 読み口なし) ときは取得不能を明示する。"""
    episode_ref = _ws_episode(manager)
    # FakeAdapter の transcript_rows は空のまま = 原本 0 件
    ctx = {"session_result": _session_result(episode_ref=episode_ref),
           "task_ref": "task:1"}
    result = jp.run_judgment_point(manager, PERSONA_ID, "post_session", ctx)
    assert "(セッション原本を取得できませんでした)" in result["args"]["situation_text"]


def test_post_session_finalize_writes_digest_untracked(
    manager, ptm, task_refs, finalize_mod, tmp_path, session_factory,
):
    """untracked degrade: digest 直書き (判断行より先) + set_digest_ref 直呼び。"""
    from database.models import Episode
    from sea.work_session import DIGEST_TAG

    episode_ref = _ws_episode(manager)
    ws_meta = _ws_meta_fixture()
    output = {
        "monologue": "一区切り。",
        "digest": "下書きを 1 本書いた。",
        "task_verdict": {"status": "continue", "desk_memo": "続きは明日"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({
        "plan_date": PLAN_DATE, "artifacts": [], "task_ref": "task:1",
        "episode_ref": episode_ref, "ws_meta": ws_meta,
    })
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    msgs = manager.personas[PERSONA_ID].sai_memory.messages
    assert len(msgs) == 2
    digest_msg, judgment_msg = msgs  # digest が先 (tracked の FIFO と同順)
    assert digest_msg["content"] == "下書きを 1 本書いた。"
    assert DIGEST_TAG in digest_msg["metadata"]["tags"]
    assert digest_msg["scope"] == "committed"
    assert digest_msg["line_role"] == "main_line"
    assert digest_msg["metadata"]["work_session"] == ws_meta
    assert digest_msg["metadata"]["origin_episode"] == episode_ref
    assert "meta_judgment" in judgment_msg["metadata"]["tags"]

    # episode の再訪の鍵が message:{id} で後段確定 (FakeAdapter は m1 を返す)
    db = session_factory()
    try:
        ep = db.query(Episode).filter(Episode.PERSONA_ID == PERSONA_ID).first()
        assert ep.DIGEST_REF == "message:m1"
    finally:
        db.close()


def test_post_session_finalize_empty_digest_warns(
    manager, ptm, task_refs, finalize_mod, tmp_path, session_factory, caplog,
):
    """digest 欠落 (スキーマ違反相当) は WARN + digest なしで判断行のみ。"""
    from database.models import Episode

    episode_ref = _ws_episode(manager)
    output = {
        "monologue": "終えた。",
        "task_verdict": {"status": "continue", "desk_memo": "続きは明日"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({
        "plan_date": PLAN_DATE, "artifacts": [], "task_ref": "task:1",
        "episode_ref": episode_ref, "ws_meta": _ws_meta_fixture(),
    })
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            finalize_mod.judgment_finalize(
                judgment_output=output, kind="post_session", judgment_context=ctx,
            )
    assert any("digest が空です" in r.message for r in caplog.records)
    msgs = manager.personas[PERSONA_ID].sai_memory.messages
    assert len(msgs) == 1  # 判断行のみ
    assert "meta_judgment" in msgs[0]["metadata"]["tags"]
    db = session_factory()
    try:
        ep = db.query(Episode).filter(Episode.PERSONA_ID == PERSONA_ID).first()
        assert ep.DIGEST_REF is None
    finally:
        db.close()


def test_post_session_finalize_tracked_digest_outbox_first(
    manager, task_refs, finalize_mod, tmp_path, session_factory,
):
    """tracked: digest outbox が第 1 項目 (判断行より前 = FIFO で digest が先)。

    配送後: digest が DIGEST_TAG committed で書かれ、RESULT_JSON に
    episode_ref が載り、再 finalize は無効 (二重 digest なし)。
    """
    from saiverse import execution_ledger as XL
    from saiverse import execution_ledger_wiring as xlw
    from sea.work_session import DIGEST_TAG

    episode_ref = _ws_episode(manager)
    ledger, eid = _tracked_execution(manager, "post_session")
    adapter = manager.personas[PERSONA_ID].sai_memory
    output = {
        "monologue": "終えた。",
        "digest": "資料を 3 件読んだ。",
        "task_verdict": {"status": "continue", "desk_memo": "続き"},
        "remaining_timetable": None,
    }
    ctx = json.dumps({
        "plan_date": PLAN_DATE, "artifacts": [], "task_ref": "task:1",
        "episode_ref": episode_ref, "ws_meta": _ws_meta_fixture(),
        "execution_id": eid,
    })
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )

    entry = ledger.get_execution(eid)
    assert entry["status"] in (XL.STATUS_APPLIED, XL.STATUS_COMPLETED)
    assert entry["result"]["episode_ref"] == episode_ref

    # outbox の並び: digest → 判断行 (OUTBOX_ID 昇順 = FIFO)
    db = session_factory()
    try:
        rows = (
            db.query(ExecutionOutboxItem)
            .filter(ExecutionOutboxItem.EXECUTION_ID == eid)
            .order_by(ExecutionOutboxItem.OUTBOX_ID.asc())
            .all()
        )
        targets = [r.TARGET for r in rows]
    finally:
        db.close()
    assert targets == [
        xlw.TARGET_SAIMEMORY_APPEND_DIGEST, xlw.TARGET_SAIMEMORY_APPEND,
    ]

    # 配送 (mark_applied(deliver=True) が即時配送済みなら flush は no-op)
    ledger.flush_pending_for_persona(PERSONA_ID)
    assert len(adapter.ledger_messages) == 2
    digest_delivered = adapter.ledger_messages[0][1]
    assert digest_delivered["content"] == "資料を 3 件読んだ。"
    assert DIGEST_TAG in digest_delivered["metadata"]["tags"]
    assert digest_delivered["scope"] == "committed"
    judgment_delivered = adapter.ledger_messages[1][1]
    assert judgment_delivered["line_role"] == "meta_judgment"

    # 再 finalize は無効 (二重 digest なし)
    with _persona_ctx(manager, tmp_path):
        summary2, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="post_session", judgment_context=ctx,
        )
    assert "already finalized" in summary2
    assert len(adapter.ledger_messages) == 2
