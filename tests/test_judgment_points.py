"""判断点 (on_event = イベント到着判断) のテスト (judgment_points.md §7)。

一時 DB (in-memory SQLite) + mock LLM 出力 (構造化出力 dict) + 仮想クロックで検証:

- on_event: reaction の 4 分岐、alert での engage_now 縮退 (スキーマ + finalize
  二重ガード)、add_task のタスク帳への積み込み、note_only は判断の記録だけ
- 起動経路のガード (契約違反は畳まず上げる / LLM 開始前の離脱は席を放棄する /
  結末の語彙と代替経路の可否)
- 実行台帳フロー (証跡ベースの成功判定・finalize の台帳化)
- 生成スキーマに additionalProperties が含まれない (プロバイダ正規化層に任せる)
- LLM の生 JSON がメインキャッシュ (SAIMemory 記録) に混入しない (不変条件 v2-A)

v2 の他の判断点 (起床 day_open / セッション終了 post_session / 就寝 day_close)
のテストは、判断点ごと退役した autonomous_behavior_v04_plan.md 段 1-4 で消した。
会話終了判断 (post_conversation) は 2026-08-16 に退役済み。

teardown で engine.dispose() + clock.disable_virtual() を必ず行う。
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    AI,
    Base,
    City,
    ExecutionOutboxItem,
    User,
)
from saiverse import clock
from saiverse import judgment_points as jp
from saiverse.event_scheduler import EventScheduler
from tool_loader import load_builtin_tool

PERSONA_ID = "alice"
PLAN_DATE = "2026-07-04"
BASE = datetime(2026, 7, 4, 7, 0, 0)  # 仮想時刻


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
    """SAIMemory adapter の最小スタブ (append_persona_message の記録)。"""

    def __init__(self):
        self.messages: List[Dict[str, Any]] = []

    def append_persona_message(self, payload):
        self.messages.append(payload)
        # 実 adapter と同じく message id を返す
        return f"m{len(self.messages)}"


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
    )
    return SimpleNamespace(
        SessionLocal=session_factory,
        personas={PERSONA_ID: persona},
        event_scheduler=EventScheduler(),  # start() しない (同期検証のみ)
        buildings=[
            SimpleNamespace(building_id="library", name="図書館"),
            SimpleNamespace(building_id="workshop", name="工房"),
        ],
        pulse_controller=FakePulseController(),
    )


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


# ---------------------------------------------------------------------------
# 起動経路のガード (席の放棄・結末の語彙)
# ---------------------------------------------------------------------------


def test_args_build_failure_fails_the_ledger_row(manager, monkeypatch):
    """引数の組み立てが落ちても、例外は入口まで漏れず席が終端化すること。

    2026-07-30 Codex 三巡目。この関数の契約は「起動できなければ理由つきの結果
    dict」で、呼び出し側 (on_event の direct fallback) はその戻り値で分岐する —
    例外を素通しすると席が prepared のまま残り、呼び出し側の代替経路も回復 tick
    の再発火も両方が動きうる。
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

    monkeypatch.setattr(jp, "build_on_event_situation_text", boom)

    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
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
                    "applied": True, "extras": ["reaction=engage_now"],
                })

    manager.execution_ledger = _ledger_stub(get_execution=_boom)
    manager.pulse_controller = _FinalizeEmitting()
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
    )
    assert result["submitted"] is True
    assert result["applied_events"][0]["extras"] == ["reaction=engage_now"]


def test_unreadable_ledger_status_rejects_a_failed_finalize_event(manager):
    """finalize が **適用できなかった** イベントを成功の証拠にしない。

    judgment_finalize は適用に失敗しても ``applied=False`` で judgment_applied を
    emit する。type だけ見ると「適用に失敗した」を「適用した」と読む
    (2026-08-14 Codex 三巡目)。
    """
    def _boom(eid):
        raise RuntimeError("ledger down")

    class _FailedFinalize(FakePulseController):
        def submit_meta_judgment(self, persona_id, building_id, meta_playbook,
                                 args=None, event_callback=None):
            super().submit_meta_judgment(
                persona_id, building_id, meta_playbook, args, event_callback,
            )
            if event_callback:
                event_callback({
                    "type": "judgment_applied", "kind": "on_event",
                    "applied": False, "extras": [],
                })

    manager.execution_ledger = _ledger_stub(get_execution=_boom)
    manager.pulse_controller = _FailedFinalize()
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id="exec-1",
    )
    assert result["submitted"] is False
    assert result["outcome"] == jp.OUTCOME_INDETERMINATE


def test_contract_violation_raises_even_when_persona_is_missing(manager):
    """契約検査は環境の状態より前 — 配線ミスが環境の問題に化けて隠れない。"""
    manager.personas = {}
    with pytest.raises(ValueError, match="event_text"):
        jp.run_judgment_point(manager, PERSONA_ID, "on_event", {})


# ---------------------------------------------------------------------------
# 共通: 起動経路のガード
# ---------------------------------------------------------------------------


def test_unknown_kind_raises(manager):
    with pytest.raises(ValueError, match="unknown judgment kind"):
        jp.run_judgment_point(manager, PERSONA_ID, "nap_time")


def test_missing_persona_returns_unsubmitted(manager):
    result = jp.run_judgment_point(
        manager, "nobody", "on_event", {"event_text": "来客"},
    )
    assert result["submitted"] is False
    assert manager.pulse_controller.submissions == []

# ---------------------------------------------------------------------------
# on_event: 起動 (4 分岐スキーマ / alert 縮退)
# ---------------------------------------------------------------------------


def _reaction_types(schema):
    variants = schema["properties"]["reaction"]["anyOf"]
    return [v["properties"]["type"]["const"] for v in variants]


def test_on_event_dispatch_schema_four_branches(manager):
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event",
        {"event_text": "来訪: ボブが訪ねてきた"},
    )
    args = result["args"]
    schema = args["response_schema"]
    _assert_no_additional_properties(schema)
    assert _reaction_types(schema) == [
        "engage_now", "add_task", "note_only", "ignore",
    ]
    # add_task は時間割に依存しない (コマの欄を持たず、積む一件の中身だけ)
    add_task = schema["properties"]["reaction"]["anyOf"][1]
    assert set(add_task["properties"]) == {"type", "task"}
    assert add_task["required"] == ["type", "task"]

    text = args["situation_text"]
    assert "ボブが訪ねてきた" in text
    assert "07:00" in text
    assert "手すき" in text  # 開いている会話なし

    ctx = json.loads(args["judgment_context"])
    assert ctx["is_alert"] is False
    assert result["playbook"] == "judgment_on_event"


def test_on_event_alert_collapses_to_engage_now(manager):
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


# ⚠ 「いまの活動」に会話以外の作業を出すテスト 3 本
# (``..._shows_open_episode_activity`` / ``..._activity_survives_stale_open_cache`` /
# ``..._running_track_alone_is_idle``) は 2026-08-22 (束 6c) に削除した。会話以外の
# 活動を答える器そのものが供給源ごと消えたため — 出来事の書き手も Track ランタイム
# も退役し (v3 §7)、「いま何に取り組んでいるか」に答えられる集合が v0.3 には無い
# (作り直しは v0.4 のティック設計、v3 §9-3)。残る二値は「会話中か、手すきか」だけ。


def _open_user_conversation(manager) -> None:
    """本番の入口を通さずに会話状態だけ立てる (前提条件のセットアップ用)。

    「会話中か」の器は三代目 (Track の status → 会話の出来事 → メモリ内の会話状態、
    2026-08-22 の束 6c)。前の二代を作る手はどちらも退役したので、いまの器を直に立てる。
    """
    from saiverse import user_conversation as uc

    uc._set_open_conversation(
        manager, PERSONA_ID, building_id="alice_room",
        participants=[PERSONA_ID, "1"],
    )


def test_on_event_situation_says_in_conversation_when_conversation_open(manager):
    """開いている会話があるときだけ「ユーザーと会話中です」と伝える。"""
    _open_user_conversation(manager)
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    assert "ユーザーと会話中です" in result["args"]["situation_text"]


def test_on_event_situation_is_idle_when_no_conversation_open(manager):
    """回帰 (2026-07-29): 会話が終わっていれば「会話中」と言わない。

    旧実装は「対ユーザー会話 Track の種別」で判定していたため、何日も前に終わった
    会話について「ユーザーと会話中です」をペルソナへ渡していた。「取り組んでいます」
    への読み替えもやはり嘘なので、手すき扱いのままにする。
    """
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "システム通知"},
    )
    situation_text = result["args"]["situation_text"]
    assert "ユーザーと会話中です" not in situation_text
    assert "手すきです" in situation_text


# ---------------------------------------------------------------------------
# on_event: finalize (4 分岐 + alert 二重ガード + 時刻整合)
# ---------------------------------------------------------------------------


def test_on_event_finalize_engage_now(manager, finalize_mod, tmp_path):
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


def test_on_event_finalize_add_task_adds_one_system_task(
    manager, finalize_mod, tmp_path
):
    """add_task はタスク帳にシステムタスクを一件積む (時間割には触れない)。"""
    from saiverse import task_book

    output = {
        "monologue": "今は手が離せないから、あとで資料を読もう。",
        "reaction": {"type": "add_task", "task": "届いた資料を読んで要点をまとめる"},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "資料が届いた", "stimulus_id": "feed:item:9"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert "reaction=add_task" in summary
    assert "applied=True" in summary

    entries = task_book.list_open_system_tasks(manager, PERSONA_ID)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["origin"] == task_book.ORIGIN_SYSTEM
    assert entry["due_at"] is None
    assert entry["origin_ref"] == "feed:item:9"
    assert "届いた資料を読んで要点をまとめる" in entry["content"]
    assert "資料が届いた" in entry["content"]
    # 判断の記録にも積んだことが残る
    assert "タスク帳へ積む" in manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]

    # 同じ刺激への finalize の再実行 (台帳なしの直呼び) でも一件のまま
    with _persona_ctx(manager, tmp_path):
        finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert len(task_book.list_open_system_tasks(manager, PERSONA_ID)) == 1


def test_on_event_finalize_add_task_rejects_empty_task(
    manager, finalize_mod, tmp_path, caplog
):
    from saiverse import task_book

    output = {"monologue": "……", "reaction": {"type": "add_task", "task": "  "}}
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "資料が届いた", "stimulus_id": "feed:item:9"})
    with caplog.at_level("WARNING"):
        with _persona_ctx(manager, tmp_path):
            summary, _, _ = finalize_mod.judgment_finalize(
                judgment_output=output, kind="on_event", judgment_context=ctx,
            )
    assert any("add_task rejected" in r.message for r in caplog.records)
    assert "applied=False" in summary
    assert task_book.list_open_system_tasks(manager, PERSONA_ID) == []


def test_on_event_finalize_note_only_stays_in_the_judgment_record(
    manager, finalize_mod, tmp_path
):
    """note_only の覚え書きは判断の記録に載るだけで、別の置き場に書かない
    (2026-10-09 決定 — 旧実装は day_plan の meta.event_memos にも積んでいた)。"""
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
    assert "reaction=note_only" in summary
    assert "applied=True" in summary
    record = manager.personas[PERSONA_ID].sai_memory.messages[0]["content"]
    assert "新しい展示が始まったらしい。今度見に行く" in record


def test_on_event_finalize_ignore_is_discardable(
    manager, finalize_mod, tmp_path
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
    manager, finalize_mod, tmp_path, caplog
):
    """alert ではスキーマ縮退に加えて finalize でも engage_now 以外を棄却する。"""
    output = {
        "monologue": "後回しにしたい。",
        "reaction": {"type": "add_task", "task": "あとで返事を考える"},
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


# ---------------------------------------------------------------------------
# 実行台帳フロー (W1 Chunk A / A7: 証跡ベース成功判定)
# ---------------------------------------------------------------------------


def _ledgered_execution(manager, session_factory, kind="on_event"):
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
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
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
        execution_id=eid,
    )
    assert result["submitted"] is False
    assert ledger.get_execution(eid)["status"] == XL.STATUS_UNKNOWN


def test_run_judgment_without_ledger_degrades(manager):
    """台帳の無い manager では従来挙動 (execution_id=None、遷移なし)。"""
    result = jp.run_judgment_point(
        manager, PERSONA_ID, "on_event", {"event_text": "来客"},
    )
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
    manager, finalize_mod, tmp_path, session_factory,
):
    """A8: (1) 世界更新後の配送失敗 → applied 維持 + pending 残存 + 直書きなし、
    (2) 修復後 flush → 判断行 1 件だけ + completed、(3) 再 finalize は無効。"""
    from saiverse import execution_ledger as XL

    ledger, eid = _tracked_execution(manager, "on_event")
    adapter = manager.personas[PERSONA_ID].sai_memory
    adapter.fail_append = True

    from saiverse import task_book

    output = {
        "monologue": "今すぐでなくていい。あとで見に行こう。",
        "reaction": {"type": "add_task", "task": "新しい展示を見に行く"},
    }
    ctx = json.dumps({"plan_date": PLAN_DATE, "is_alert": False,
                      "event_text": "掲示板の告知", "execution_id": eid,
                      "stimulus_id": "board:notice:1"})
    with _persona_ctx(manager, tmp_path):
        summary, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )

    # (1) 世界更新 (タスク帳への一件) は 1 回だけ適用され、summary は applied を維持
    assert "applied=True" in summary
    assert len(task_book.list_open_system_tasks(manager, PERSONA_ID)) == 1
    # 台帳は applied (「適用済み・記録待ち」)、判断行は pending に凍結
    entry = ledger.get_execution(eid)
    assert entry["status"] == XL.STATUS_APPLIED
    assert entry["result"]["kind"] == "on_event"
    assert entry["result"]["reaction"] == "add_task"
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
    assert "タスク帳へ積む" in msg["content"]
    assert ledger.get_execution(eid)["status"] == XL.STATUS_COMPLETED

    # (3) 同じ execution_id で再 finalize → 世界更新は走らない
    with _persona_ctx(manager, tmp_path):
        summary2, _, _ = finalize_mod.judgment_finalize(
            judgment_output=output, kind="on_event", judgment_context=ctx,
        )
    assert "already finalized" in summary2
    assert len(task_book.list_open_system_tasks(manager, PERSONA_ID)) == 1
    assert len(adapter.ledger_messages) == 1


def test_finalize_untracked_with_ledger_but_no_execution_id(
    manager, finalize_mod, tmp_path, session_factory,
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
