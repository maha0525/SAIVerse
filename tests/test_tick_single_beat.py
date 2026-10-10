"""ティックの器と単発制御 (一回で閉じる Beat) の回帰。

設計: docs/intent/autonomous_behavior_v3.md §5「ティックは 1 Beat で閉じる」/
docs/intent/autonomous_behavior_v04_plan.md 段 2。

固定する契約:

1. 自律の Pulse (``Aspect.AUTONOMOUS``) は標準モデル・メインライン・committed。
2. ``single_beat`` の LLM ノードは、生成に含まれたスペルをテキスト順に一度だけ
   実行し、結果を続きの生成に回さない (再呼び出しが起きない)。
3. 結果は記憶にも続きの材料にも入れない。帰結は成功も失敗も全部、一通の知覚
   (``[システム通知]``) として次の Pulse の頭へ届く (失敗した行には印)。
   スペル行と結果は pulse_logs (PulseContext) にも残る。
3b. スペル行に挟まれた散文は、記憶・Pulse ログ・建物の記録の本文のどれからも
   落ちない (通常の完走でも、割り込みでも)。
3c. 帰結の知覚を積めなかった回は黙って正常完了にしない。完走した周は Beat を
   失敗 (``SpellOutcomeDeliveryError`` → PulseController の error) として
   閉じ、スペルは再実行しない。止まった周は取消の例外をすり替えずに ERROR を
   残し、「届けた」の印を立てない。
3d. スペルの結果の添付 (``meta.media``) は、完走でも中断でも帰結の知覚に
   載り、次の Pulse の頭の知覚消費の media まで届く。
4. ティックの器 (``tick`` Playbook) の出力は建物へ書かれない。会話の器
   (``track_user_conversation``) は従来どおり書く。
5. ``fire_tick`` は ``tick`` を auto Pulse として ``run_sea_auto`` へ流す。
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from sai_memory.perception_buffer import (
    format_perception_message,
    init_perception_buffer_table,
    list_pending,
    push_perception,
)
from sea import runtime_llm
from sea.playbook_models import PlaybookSchema
from sea.pulse_context import Aspect, PulseContext, aspect_from_pulse_type

REPO = Path(__file__).resolve().parents[1]
SPELL_A = "note_add"
SPELL_B = "memo_add"


# ---------------------------------------------------------------------------
# フェイク
# ---------------------------------------------------------------------------


class _Adapter:
    """知覚バッファだけを本物の SQLite で持つ SAIMemory の最小フェイク。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        init_perception_buffer_table(self.conn)

    def push_perception(self, kind, content, *, reduce_key=None, salient=False,
                        media=None, metadata=None):
        push_perception(
            self.conn, kind, content, reduce_key=reduce_key, salient=salient,
            media=media, metadata=metadata,
        )

    def pending(self):
        return list_pending(self.conn)


class _LoopRuntime:
    """_run_spell_loop が触る SEARuntime の最小フェイク。"""

    def __init__(self):
        self.stored_calls: List[Dict[str, Any]] = []
        self.manager = SimpleNamespace()
        self.session_lifecycle = SimpleNamespace(
            touch_anchor_after_llm_call=lambda persona, usage, anchor_id=None: None,
        )

    def _effective_building_id(self, persona, fallback):
        return fallback

    def _store_memory(self, persona, text, **kwargs):
        self.stored_calls.append({"text": text, **kwargs})
        return "mem-1" if kwargs.get("return_message_id") else True

    def _default_temperature(self, persona):
        return None

    def _get_cache_kwargs(self, persona_id=None):
        return {}

    def _dump_llm_io(self, *args, **kwargs):
        return None

    def _accumulate_usage(self, *args, **kwargs):
        return None


class _NoCallClient:
    """呼ばれたら落ちる LLM クライアント (再呼び出しが起きないことの検査)。"""

    def __init__(self):
        self.calls = 0

    def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("single-beat node must not re-invoke the LLM")

    def generate_stream(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("single-beat node must not re-invoke the LLM")


def _scripted_spell(results: Dict[str, tuple], order: List[str]):
    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        order.append(tool_name)
        return results[tool_name]
    return fake


class _FailingAdapter(_Adapter):
    """知覚を積む呼び出しが例外を投げる SAIMemory (配送の失敗の注入)。"""

    def push_perception(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


class _NotReadyAdapter(_Adapter):
    """DB に繋がっていない SAIMemory (本物は push を黙って空振りする)。"""

    def is_ready(self):
        return False


def _run_single_beat_loop(text: str, results: Dict[str, tuple], *,
                          adapter: Any = ...):
    runtime = _LoopRuntime()
    if adapter is ...:
        adapter = _Adapter()
    persona = SimpleNamespace(persona_id="p1", sai_memory=adapter)
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    state = {"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
             "_cancellation_token": None}
    node_def = SimpleNamespace(id="tick", memorize=True, speak=False, single_beat=True)
    playbook = SimpleNamespace(name="tick")
    messages: List[Dict[str, Any]] = []
    order: List[str] = []
    client = _NoCallClient()
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell(results, order)):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=text, spell_enabled=True, llm_client=client, runtime=runtime,
            persona=persona, building_id="b1", state=state, messages=messages,
            playbook=playbook, event_callback=None, node_def=node_def,
        ))
    return SimpleNamespace(
        result=result, runtime=runtime, adapter=adapter, pulse_ctx=pulse_ctx,
        messages=messages, order=order, client=client,
    )


TWO_SPELLS = (
    "少し片付けよう。\n"
    f"/spell name='{SPELL_A}' args={{\"text\": \"一つ目\"}}\n"
    f"/spell name='{SPELL_B}' args={{\"text\": \"二つ目\"}}\n"
    "これで終わり。"
)


# ---------------------------------------------------------------------------
# 1. AUTONOMOUS の導出
# ---------------------------------------------------------------------------


def test_autonomous_aspect_is_standard_main_line_committed():
    aspect = aspect_from_pulse_type("auto")
    assert aspect is Aspect.AUTONOMOUS
    assert (aspect.line_role, aspect.scope, aspect.model_tier) == (
        "main_line", "committed", "standard",
    )


# ---------------------------------------------------------------------------
# 2. 単発制御 — 一巡だけ実行し、続きの生成をしない
# ---------------------------------------------------------------------------


def test_single_beat_runs_each_spell_once_in_order_and_never_reinvokes():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("二つ目を記録しました", None, True),
    })

    assert run.order == [SPELL_A, SPELL_B]  # テキスト順に一度ずつ
    assert run.client.calls == 0             # 続きの生成が走らない
    assert run.result.loop_count == 1
    assert run.result.final_continuation == ""
    assert len(run.result.segments) == 1


def test_single_beat_memorizes_the_body_once_and_keeps_results_out_of_memory():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("二つ目を記録しました", None, True),
    })

    # 本人の記憶に入るのは周の本文 (スペル行込み) の一件だけ。結果の要約
    # (system 行) は書かれない — 帰結は知覚で届く (下の検査)。
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    body = run.runtime.stored_calls[0]["text"]
    assert "少し片付けよう。" in body and "これで終わり。" in body
    assert f"name='{SPELL_A}'" in body and f"name='{SPELL_B}'" in body
    assert not any("記録しました" in c["text"] for c in run.runtime.stored_calls)
    # 続きの材料にも結果を積まない
    assert not any("[Spell Result" in str(m.get("content")) for m in run.messages)


def test_single_beat_delivers_successful_outcomes_too():
    """成功の帰結も知覚で届く — 帰結が戻り値にしか無い読む系スペルのため。

    旧裁定「成功は世界の記録が運ぶ」は memory_read・検索・read_url_content の
    ような読む系で破綻した (2026-10-10 Codex 敵対レビュー 2 巡目 high)。
    """
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("読んだ中身: とても長い本文", None, True),
    })
    pending = run.adapter.pending()
    assert len(pending) == 1
    item = pending[0]
    assert item.kind == runtime_llm.SINGLE_BEAT_SPELL_OUTCOMES_KIND
    assert item.content.startswith("直前の自分の時間に唱えたスペルの帰結です。")
    # 唱えた順に、正規化したスペル行 + 結果の文面
    assert item.content.index(f"name='{SPELL_A}'") < item.content.index(f"name='{SPELL_B}'")
    assert "→ 一つ目を記録しました" in item.content
    assert "→ 読んだ中身: とても長い本文" in item.content
    assert runtime_llm.SPELL_OUTCOME_FAILURE_MARK not in item.content
    meta = json.loads(item.metadata)
    assert meta["spells"] == [SPELL_A, SPELL_B]
    assert meta["failed"] == [] and meta["not_run"] == []
    assert format_perception_message(pending).startswith("[システム通知]\n")


def test_single_beat_delivers_a_long_result_without_truncation():
    long_result = "あ" * 20000
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: (long_result, None, True),
        SPELL_B: ("ok", None, True),
    })
    (item,) = run.adapter.pending()
    assert long_result in item.content


def test_single_beat_keeps_spell_lines_and_results_in_pulse_logs():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("二つ目を記録しました", None, True),
    })

    logs = [(e.role, e.content) for e in run.pulse_ctx.logs]
    assert any(role == "assistant" and f"name='{SPELL_A}'" in content
               for role, content in logs)
    results = [content for role, content in logs if role == "system"]
    assert len(results) == 1
    assert "一つ目を記録しました" in results[0]
    assert "二つ目を記録しました" in results[0]


def test_without_single_beat_the_loop_still_reinvokes():
    """欄の無いノードは従来どおり結果を積んで続きを生成する (既定は不変)。"""
    runtime = _LoopRuntime()
    persona = SimpleNamespace(persona_id="p1", sai_memory=_Adapter())
    calls: List[list] = []

    class _Client:
        def generate(self, messages, tools=None, temperature=None, **kwargs):
            calls.append(list(messages))
            return "続きです。"

        def consume_usage(self):
            return None

        def consume_reasoning(self):
            return []

        def consume_reasoning_details(self):
            return None

    order: List[str] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell({SPELL_A: ("ok", None, True),
                                           SPELL_B: ("ok", None, True)}, order)):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=TWO_SPELLS, spell_enabled=True, llm_client=_Client(),
            runtime=runtime, persona=persona, building_id="b1",
            state={"_pulse_id": "p", "_pulse_context": None,
                   "_cancellation_token": None},
            messages=[], playbook=SimpleNamespace(name="pb"), event_callback=None,
            node_def=SimpleNamespace(id="llm", memorize=None, speak=False),
        ))
    assert len(calls) == 1
    assert result.final_continuation == "続きです。"


# ---------------------------------------------------------------------------
# 3. 帰結は成功も失敗も一通の知覚で次の Pulse の頭へ届く (失敗には印)
# ---------------------------------------------------------------------------

FAIL_MARK = runtime_llm.SPELL_OUTCOME_FAILURE_MARK


def test_a_failed_spell_reaches_the_next_pulse_as_a_system_notice():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("Spell error (memo_add): ValueError: boom", None, False),
    })

    pending = run.adapter.pending()
    assert len(pending) == 1
    item = pending[0]
    assert item.kind == runtime_llm.SINGLE_BEAT_SPELL_OUTCOMES_KIND
    # 両方の行が載り、失敗した行の結果にだけ印が付く
    assert f"name='{SPELL_A}'" in item.content
    assert "→ 一つ目を記録しました" in item.content
    assert f"name='{SPELL_B}'" in item.content
    assert f"→ {FAIL_MARK}Spell error (memo_add): ValueError: boom" in item.content
    assert item.content.count(FAIL_MARK) == 1
    meta = json.loads(item.metadata)
    assert meta["spells"] == [SPELL_A, SPELL_B]
    assert meta["failed"] == [SPELL_B]
    assert meta["pulse_id"] == "pulse-1"
    # 本人に見える形は機構の名義 ([システム通知])
    rendered = format_perception_message(pending)
    assert rendered.startswith("[システム通知]\n")
    # 失敗しても続きの生成は走らない
    assert run.client.calls == 0
    # 失敗も記憶 (結果の要約) には書かない — 知覚と二重に届かない
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]


def test_a_logical_failure_declared_by_the_tool_also_reaches_the_persona():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("該当なし", {"error": True}, True),
        SPELL_B: ("二つ目を記録しました", None, True),
    })
    pending = run.adapter.pending()
    assert len(pending) == 1
    assert f"→ {FAIL_MARK}該当なし" in pending[0].content
    assert json.loads(pending[0].metadata)["failed"] == [SPELL_A]


def test_a_logical_failure_is_recorded_as_a_failure_in_the_round_record():
    """meta.error の宣言は周の記録の success 欄に写る — 表示・activity_trace・
    Pulse ログの札が同じ真実を読む (Codex 敵対レビュー 2 巡目 high)。"""
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("該当なし", {"error": True}, True),
        SPELL_B: ("二つ目を記録しました", None, True),
    })
    # 建物の記録の本文: 失敗の装い (spellResultError) は A の一件だけ
    seg_text = run.result.segments[0].text
    assert seg_text.count("spellResultError") == 1
    assert seg_text.index("spellResultError") < seg_text.index(f"name='{SPELL_B}'")
    results_log = [e.content for e in run.pulse_ctx.logs if e.role == "system"]
    assert f"[Spell Error: {SPELL_A}]" in results_log[0]
    assert f"[Spell Result: {SPELL_B}]" in results_log[0]


def test_an_unknown_spell_counts_as_a_failure():
    text = "やってみる。\n/spell name='no_such_spell' args={}"
    run = _run_single_beat_loop(text, {})
    assert run.order == []  # 実行はされない
    pending = run.adapter.pending()
    assert len(pending) == 1
    assert "no_such_spell" in pending[0].content
    assert FAIL_MARK in pending[0].content
    assert run.client.calls == 0


# ---------------------------------------------------------------------------
# 3b. スペル行に挟まれた散文を捨てない
# ---------------------------------------------------------------------------

PROSE_BETWEEN = (
    "本文の頭。\n"
    f"/spell name='{SPELL_A}' args={{\"text\": \"一つ目\"}}\n"
    "あいだの散文。\n"
    f"/spell name='{SPELL_B}' args={{\"text\": \"二つ目\"}}\n"
    "末尾の散文。"
)


def _assert_prose_in_order(body: str):
    keys = ["本文の頭。", f"name='{SPELL_A}'", "あいだの散文。",
            f"name='{SPELL_B}'", "末尾の散文。"]
    positions = [body.index(k) for k in keys]
    assert positions == sorted(positions), body


def test_prose_between_spells_survives_a_completed_single_beat_round():
    run = _run_single_beat_loop(PROSE_BETWEEN, {
        SPELL_A: ("ok-a", None, True), SPELL_B: ("ok-b", None, True),
    })
    # 記憶
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    _assert_prose_in_order(run.runtime.stored_calls[0]["text"])
    # Pulse ログ
    (assistant_log,) = [e.content for e in run.pulse_ctx.logs if e.role == "assistant"]
    _assert_prose_in_order(assistant_log)
    # 建物の記録になる本文 (Beat のセグメント)
    _assert_prose_in_order(run.result.segments[0].text)


def test_prose_between_spells_survives_a_completed_multi_round_loop():
    """単発でない通常の周 (続きを生成する) でも、記憶・続きの材料・本文が全量を持つ。"""
    runtime = _LoopRuntime()
    persona = SimpleNamespace(persona_id="p1", sai_memory=_Adapter())
    pulse_ctx = PulseContext(pulse_id="p")
    pulse_ctx.push_line(aspect=Aspect.CONVERSATION)

    class _Client:
        def generate(self, messages, tools=None, temperature=None, **kwargs):
            return "続きです。"

        def consume_usage(self):
            return None

        def consume_reasoning(self):
            return []

        def consume_reasoning_details(self):
            return None

    messages: List[Dict[str, Any]] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell({SPELL_A: ("ok", None, True),
                                           SPELL_B: ("ok", None, True)}, [])):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=PROSE_BETWEEN, spell_enabled=True, llm_client=_Client(),
            runtime=runtime, persona=persona, building_id="b1",
            state={"_pulse_id": "p", "_pulse_context": pulse_ctx,
                   "_cancellation_token": None},
            messages=messages, playbook=SimpleNamespace(name="pb"),
            event_callback=None,
            node_def=SimpleNamespace(id="llm", memorize=None, speak=False),
        ))
    _assert_prose_in_order(runtime.stored_calls[0]["text"])
    _assert_prose_in_order(messages[0]["content"])
    _assert_prose_in_order(result.segments[0].text)
    (assistant_log,) = [
        e.content for e in pulse_ctx.logs
        if e.role == "assistant" and e.node_id == "spell_round_1"
    ]
    _assert_prose_in_order(assistant_log)


def test_compose_stopped_round_keeps_prose_between_spells():
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}):
        composed = runtime_llm._compose_stopped_round(
            PROSE_BETWEEN, [("ok-a", None, True)],
        )
    _assert_prose_in_order(composed["memory_text"])
    _assert_prose_in_order(composed["text"])
    assert composed["form"] == runtime_llm.SAVED_FORM_SPELL_UNFINISHED


def test_prose_between_spells_survives_an_interrupt_after_generation():
    """生成の直後の割り込み (周の頭の取消) でも、記憶と Pulse ログに全量が残る。"""
    run = _run_cancelled_loop(PROSE_BETWEEN)
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    _assert_prose_in_order(run.runtime.stored_calls[0]["text"])
    (assistant_log,) = [e.content for e in run.pulse_ctx.logs if e.role == "assistant"]
    _assert_prose_in_order(assistant_log)


def test_a_stop_in_the_middle_of_a_single_beat_round_keeps_prose_and_reports_outcomes():
    """スペルの実行中に止まった周: 本文は全量残り、帰結は受け取った分 + 未着の事実。"""
    from sea.cancellation import ExecutionCancelledException

    runtime = _LoopRuntime()
    adapter = _Adapter()
    persona = SimpleNamespace(persona_id="p1", sai_memory=adapter)
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    three = PROSE_BETWEEN + f"\n/spell name='{SPELL_A}' args={{\"text\": \"三つ目\"}}"
    calls: List[str] = []

    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        calls.append(tool_name)
        if tool_name == SPELL_B:
            raise ExecutionCancelledException(interrupted_by="user")
        return ("ok-a", None, True)

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=fake):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=three, spell_enabled=True, llm_client=_NoCallClient(),
            runtime=runtime, persona=persona, building_id="b1",
            state={"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
                   "_cancellation_token": None},
            messages=[], playbook=SimpleNamespace(name="tick"),
            event_callback=None, node_def=SimpleNamespace(**TICK_NODE),
        ))
    assert calls == [SPELL_A, SPELL_B]
    assert isinstance(result.stop_error, ExecutionCancelledException)
    # 記憶 (周の頭で書いた本文) と建物の記録の本文
    _assert_prose_in_order(runtime.stored_calls[0]["text"])
    _assert_prose_in_order(result.segments[-1].text)
    # 帰結: A は結果、B は実行中に区切られた事実、3 行目は実行されていない事実
    (item,) = adapter.pending()
    assert "→ ok-a" in item.content
    assert "結果を受け取る前にその時間が区切られました" in item.content
    assert "その時間が区切られたため、実行されていません。" in item.content
    assert FAIL_MARK not in item.content  # 来ていない結果を「失敗」と書かない
    meta = json.loads(item.metadata)
    assert meta["spells"] == [SPELL_A, SPELL_B, SPELL_A]
    assert meta["not_run"] == [SPELL_B, SPELL_A]
    assert meta["failed"] == []


# ---------------------------------------------------------------------------
# 4. ティックの器は建物へ書かない / 会話の器は書く
# ---------------------------------------------------------------------------


def _load_playbook(name: str) -> PlaybookSchema:
    path = REPO / "builtin_data" / "playbooks" / "public" / f"{name}.json"
    return PlaybookSchema(**json.loads(path.read_text(encoding="utf-8")))


class _TextClient:
    config_key = None

    def __init__(self, text: str):
        self.text = text

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        return self.text

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        return iter([self.text])

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None

    def consume_stream_error(self):
        return None


def _run_node(monkeypatch, playbook: PlaybookSchema, text: str, *,
              streaming: bool, state_extra: Optional[Dict[str, Any]] = None,
              spell_results: Optional[Dict[str, tuple]] = None,
              client: Any = None, expect_exc: Any = None,
              adapter: Any = None):
    runtime = MagicMock()
    runtime.manager.occupants = {"b1": ["p1"]}
    runtime._effective_building_id.return_value = "b1"
    runtime._process_structured_output.return_value = False
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime._emit_say.return_value = {"message_id": "say-1"}
    runtime._emit_speak_start.return_value = "msg-1"
    runtime.select_llm_client.return_value = (client or _TextClient(text), "model-a")

    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: streaming)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))

    if adapter is None:
        adapter = _Adapter()
    persona = SimpleNamespace(
        persona_id=None, persona_name="p", history_manager=MagicMock(),
        sai_memory=adapter,
    )
    events: List[Dict[str, Any]] = []
    node_def = playbook.nodes[0]
    node = runtime_llm.lg_llm_node(
        runtime, node_def, persona, "b1", playbook, events.append,
    )
    pulse_ctx = PulseContext(pulse_id="pl-1")
    state: Dict[str, Any] = {
        "_messages": [], "_pulse_id": "pl-1", "_pulse_context": pulse_ctx,
        "_spell_enabled": True, "_realtime_spells_executed": True,
        "assignment": "このティックは庭の水やり。",
    }
    state.update(state_extra or {})
    order: List[str] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell(spell_results or {}, order)):
        if expect_exc is not None:
            with pytest.raises(expect_exc):
                asyncio.run(node(state))
        else:
            asyncio.run(node(state))
    state["_test_spell_order"] = order
    return runtime, events, state, adapter


def _building_writes(runtime) -> int:
    return (
        runtime._emit_say.call_count
        + runtime._emit_speak_start.call_count
        + runtime._emit_speak_finalize.call_count
    )


@pytest.mark.parametrize("streaming", [True, False])
def test_the_tick_vessel_writes_nothing_to_the_building(monkeypatch, streaming):
    tick = _load_playbook("tick")
    text = (
        "水をやろう。\n"
        f"/spell name='{SPELL_A}' args={{\"text\": \"水やり\"}}\n"
        "あとで様子を見る。"
    )
    runtime, events, state, adapter = _run_node(
        monkeypatch, tick, text, streaming=streaming,
        spell_results={SPELL_A: ("記録しました", None, True)},
    )
    assert _building_writes(runtime) == 0
    assert not [e for e in events if e.get("type") in ("say", "streaming_chunk")]
    # 本物のノード経路でも単発制御が効く (スペルは一度だけ、続きの生成なし)
    assert state["_test_spell_order"] == [SPELL_A]
    assert state["last"] == ""
    # 本文は本人の記憶 (メインラインの memorize) に入る
    stored = [c.args[1] for c in runtime._store_memory.call_args_list]
    assert any("水をやろう。" in s for s in stored)
    # 割り当ての確定情報がプロンプトに入っている
    prompts = [c.kwargs.get("paired_action_text") for c in runtime._store_memory.call_args_list]
    assert any(p and "このティックは庭の水やり。" in p for p in prompts)


def test_the_tick_vessel_without_spells_memorizes_and_stays_silent(monkeypatch):
    tick = _load_playbook("tick")
    runtime, events, state, adapter = _run_node(
        monkeypatch, tick, "今日は静かに過ごそう。", streaming=True,
    )
    assert _building_writes(runtime) == 0
    stored = [c.args[1] for c in runtime._store_memory.call_args_list]
    assert stored == ["今日は静かに過ごそう。"]


def test_the_conversation_vessel_still_writes_to_the_building(monkeypatch):
    conv = _load_playbook("track_user_conversation")
    runtime, events, state, adapter = _run_node(
        monkeypatch, conv, "こんにちは。", streaming=False,
    )
    assert runtime._emit_say.call_count == 1
    assert [e for e in events if e.get("type") == "say"]


def test_the_tick_playbook_declares_its_template_variable_and_free_default():
    tick = _load_playbook("tick")
    node = tick.nodes[0]
    assert "{assignment}" in node.action
    params = {p.name: p for p in tick.input_schema}
    assert "assignment" in params
    assert params["assignment"].required is False
    assert "自分のための時間" in params["assignment"].default
    assert node.single_beat is True
    assert node.speak is False
    assert node.memorize is True
    assert tick.router_callable is False and tick.user_selectable is False


# ---------------------------------------------------------------------------
# 5. fire_tick の配線
# ---------------------------------------------------------------------------


class _RecordingController:
    """submit された ExecutionRequest を記録し、観測欄を台本どおりに書くフェイク。"""

    def __init__(self, action="execute", outcome="completed", outputs=None, exc=None):
        self.requests: List[Any] = []
        self.action, self.outcome, self.outputs, self.exc = action, outcome, outputs, exc

    def submit(self, request):
        self.requests.append(request)
        request.dispatch_action = self.action
        request.runtime_outcome = self.outcome
        if self.exc is not None:
            raise self.exc
        return self.outputs if self.outputs is not None else []


def _fake_manager(controller=None):
    from saiverse.saiverse_manager import SAIVerseManager

    persona = SimpleNamespace(persona_id="p1", current_building_id="room-7")
    mgr = SimpleNamespace(
        personas={"p1": persona},
        occupants={"room-7": ["p1"]},
        pulse_controller=controller or _RecordingController(),
    )
    mgr._submit_tick = SAIVerseManager._submit_tick.__get__(mgr)
    mgr.fire_tick = SAIVerseManager.fire_tick.__get__(mgr)
    return mgr, persona


def test_fire_tick_runs_the_tick_playbook_as_an_auto_pulse_in_the_current_room():
    mgr, persona = _fake_manager()
    out = mgr.fire_tick("p1", assignment_text="  このティックは挿絵の続き。 ")
    assert out == {
        "action": "execute", "runtime_outcome": "completed", "error": None,
        "outputs": [],
    }
    (request,) = mgr.pulse_controller.requests
    assert request.type == "auto"
    assert (request.persona_id, request.building_id) == ("p1", "room-7")
    assert request.meta_playbook == "tick"
    assert request.args == {"assignment": "このティックは挿絵の続き。"}


@pytest.mark.parametrize("assignment", [None, "", "   "])
def test_fire_tick_without_assignment_leaves_the_free_time_default(assignment):
    mgr, persona = _fake_manager()
    mgr.fire_tick("p1", assignment_text=assignment)
    assert mgr.pulse_controller.requests[0].args is None


def test_fire_tick_rejects_an_unknown_persona():
    mgr, _ = _fake_manager()
    with pytest.raises(KeyError):
        mgr.fire_tick("nobody")
    assert mgr.pulse_controller.requests == []


def test_fire_tick_reports_an_exception_with_the_observed_outcome():
    """submit が例外を投げても握って型付きで返す (受付の裁定は残す)。"""
    from llm_clients.exceptions import LLMError

    mgr, _ = _fake_manager(_RecordingController(
        outcome="error", exc=LLMError("provider down"),
    ))
    out = mgr.fire_tick("p1")
    assert out["action"] == "execute"
    assert out["runtime_outcome"] == "error"
    assert "provider down" in out["error"]


def test_fire_tick_skips_a_discord_visitor_without_submitting():
    mgr, persona = _fake_manager()
    persona.is_discord_visitor = True
    out = mgr.fire_tick("p1")
    assert out["action"] == "unavailable" and out["runtime_outcome"] is None
    assert mgr.pulse_controller.requests == []


def test_run_sea_auto_returns_the_submit_result():
    from saiverse.saiverse_manager import SAIVerseManager

    pc = MagicMock()
    pc.submit_auto.return_value = ["done"]
    mgr = SimpleNamespace(pulse_controller=pc)
    persona = SimpleNamespace(persona_id="p1")
    out = SAIVerseManager.run_sea_auto(
        mgr, persona, "b1", [], meta_playbook="tick", args={"assignment": "x"},
    )
    assert out == ["done"]
    pc.submit_auto.assert_called_once_with(
        persona_id="p1", building_id="b1", meta_playbook="tick",
        args={"assignment": "x"},
    )
    # 見送り (席が埋まっている) は None のまま返る
    pc.submit_auto.return_value = None
    assert SAIVerseManager.run_sea_auto(mgr, persona, "b1", [], meta_playbook="tick") is None


def test_the_tick_route_calls_fire_tick():
    from api.routes.people.tick import TickRequest, fire_tick

    mgr = SimpleNamespace(fire_tick=MagicMock(return_value={
        "action": "execute", "runtime_outcome": "completed", "error": None,
        "outputs": [],
    }))
    resp = fire_tick("p1", TickRequest(assignment="このティックは日記。"), manager=mgr)
    assert resp.executed is True and resp.persona_id == "p1"
    assert resp.outcome == "completed"
    mgr.fire_tick.assert_called_once_with("p1", assignment_text="このティックは日記。")

    # 席が埋まって見送られた回: 顛末が無いので受付の裁定が outcome になる
    mgr.fire_tick.return_value = {
        "action": "skipped", "runtime_outcome": None, "error": None, "outputs": None,
    }
    resp = fire_tick("p1", None, manager=mgr)
    assert resp.executed is False and resp.outcome == "skipped"

    from fastapi import HTTPException

    mgr.fire_tick.side_effect = KeyError("p1")
    with pytest.raises(HTTPException) as exc_info:
        fire_tick("p1", None, manager=mgr)
    assert exc_info.value.status_code == 404


def _real_controller_manager(behavior):
    """本物の PulseController を通す manager (run_meta_user が behavior() を返す/投げる)。

    構成は tests/test_schedule_dispatch_outcome.py の ``_make_controller`` と同型。
    """
    from saiverse.saiverse_manager import SAIVerseManager
    from sea.pulse_controller import PulseController

    persona = SimpleNamespace(persona_id="p1", current_building_id="room-7")

    def run_meta_user(**kwargs):
        return behavior()

    sea_runtime = SimpleNamespace(
        manager=SimpleNamespace(all_personas={"p1": persona}),
        run_meta_user=run_meta_user,
    )
    mgr = SimpleNamespace(
        personas={"p1": persona}, occupants={"room-7": ["p1"]},
        pulse_controller=PulseController(sea_runtime),
    )
    mgr._submit_tick = SAIVerseManager._submit_tick.__get__(mgr)
    mgr.fire_tick = SAIVerseManager.fire_tick.__get__(mgr)
    return mgr


def _raise(exc):
    def _behavior():
        raise exc
    return _behavior


@pytest.mark.parametrize("behavior,outcome,executed", [
    # 正常に閉じた無発声のティック: 出力は空でも走りきっている
    (lambda: [], "completed", True),
    (lambda: ["ok"], "completed", True),
    # 関所の閉鎖: Playbook は走っていない (PulseController は [] を返す)
    ("gate_closed", "gate_closed", False),
    # 文脈水位の不足: Playbook は走っていない (同じく [])
    ("floor_unmet", "floor_unmet", False),
    # 取消: 席を譲った (同じく [])
    ("cancelled", "cancelled", False),
])
def test_the_tick_route_reports_whether_the_tick_ran_to_completion(
    behavior, outcome, executed,
):
    """API の executed は「受付 execute かつ顛末 completed」のときだけ True。

    PulseController は関所閉鎖・水位不足・取消のどれも空配列を返し、正常な
    無発声のティックも空配列を返しうる — 配列の空判定では区別できない
    (Codex 敵対レビュー 2026-10-10 medium)。
    """
    from api.routes.people.tick import fire_tick
    from sea.beat_gate import BeatGateClosedError
    from sea.cancellation import ExecutionCancelledException
    from sea.runtime_context import WindowFloorUnmetError

    if behavior == "gate_closed":
        behavior = _raise(BeatGateClosedError("p1", "auto"))
    elif behavior == "floor_unmet":
        behavior = _raise(WindowFloorUnmetError("floor unmet"))
    elif behavior == "cancelled":
        behavior = _raise(ExecutionCancelledException(interrupted_by="user"))
    mgr = _real_controller_manager(behavior)

    resp = fire_tick("p1", None, manager=mgr)

    assert resp.executed is executed
    assert resp.outcome == outcome


def test_the_tick_route_reports_a_runtime_error_as_not_executed():
    from api.routes.people.tick import fire_tick
    from llm_clients.exceptions import LLMError

    mgr = _real_controller_manager(_raise(LLMError("provider down")))
    resp = fire_tick("p1", None, manager=mgr)
    assert resp.executed is False
    assert resp.outcome == "error"
    assert "provider down" in resp.error


def _real_loader_world(tmp_path, rows: List[Dict[str, Any]]):
    """本物の Playbook ローダー (SEARuntime._load_playbook_for → DB) と本物の
    PulseController を通す manager。Playbook 表だけを持つ SQLite を使う。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.models import Base
    from database.models import Playbook as PlaybookModel
    from saiverse.saiverse_manager import SAIVerseManager
    from sea.pulse_controller import PulseController
    from sea.runtime import SEARuntime

    engine = create_engine(f"sqlite:///{tmp_path / 'playbooks.db'}")
    Base.metadata.create_all(engine, tables=[PlaybookModel.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as session:
        for row in rows:
            session.add(PlaybookModel(**row))
        session.commit()

    persona = SimpleNamespace(
        persona_name="p", persona_id="p1", model="m", llm_client=object(),
        current_building_id="room-7",
        history_manager=SimpleNamespace(add_message=MagicMock(),
                                        add_to_persona_only=MagicMock()),
        execution_state={},
    )
    mgr = SimpleNamespace(
        building_histories={"room-7": []}, SessionLocal=Session,
        personas={"p1": persona}, all_personas={"p1": persona},
        occupants={"room-7": ["p1"]},
    )
    runtime = SEARuntime(mgr)
    # 窓の前処理は本件の外 (床・読み戻し・非常畳み・応答後の代謝)。
    runtime.session_lifecycle.maybe_run_window_refill = MagicMock()
    runtime.session_lifecycle.ensure_window_floor = MagicMock(return_value="skip")
    runtime.session_lifecycle.maybe_run_emergency_precompaction = MagicMock()
    runtime.session_lifecycle.maybe_run_metabolism = MagicMock()
    mgr.sea_runtime = runtime
    mgr.pulse_controller = PulseController(runtime)
    mgr._submit_tick = SAIVerseManager._submit_tick.__get__(mgr)
    mgr.fire_tick = SAIVerseManager.fire_tick.__get__(mgr)
    return mgr, runtime, persona


def _tick_row(**overrides) -> Dict[str, Any]:
    path = REPO / "builtin_data" / "playbooks" / "public" / "tick.json"
    nodes_json = path.read_text(encoding="utf-8")
    row = dict(name="tick", description="", scope="public",
               schema_json="{}", nodes_json=nodes_json)
    row.update(overrides)
    return row


@pytest.mark.parametrize("rows", [
    [],                                                       # 未登録
    [_tick_row(scope="personal", created_by_persona_id="someone-else")],  # 可視性で除外
], ids=["unregistered", "not_visible"])
def test_the_tick_route_reports_an_unavailable_tick_playbook_as_an_error(tmp_path, rows):
    """器の Playbook が取れない回は completed ではなく error (原因つき)。

    以前は SEARuntime がエラー文字列の list を正常に返し、PulseController が
    completed と記帳 → API が executed=true を返していた (Codex 敵対レビュー
    2 巡目 medium)。本物のローダー境界を通して確かめる。
    """
    from api.routes.people.tick import fire_tick

    mgr, runtime, persona = _real_loader_world(tmp_path, rows)
    # 境界が本物であることの確認: ローダーは None に畳む
    assert runtime._load_playbook_for("tick", persona, "room-7") is None

    resp = fire_tick("p1", None, manager=mgr)

    assert resp.executed is False
    assert resp.outcome == "error"
    assert resp.error == "playbook 'tick' is unavailable (not found or not visible)"


def test_the_real_loader_returns_a_visible_tick_playbook(tmp_path):
    """上の二件の対照: 公開の行なら同じローダーが Playbook を返す。"""
    _mgr, runtime, persona = _real_loader_world(tmp_path, [_tick_row()])
    pb = runtime._load_playbook_for("tick", persona, "room-7")
    assert pb is not None and pb.name == "tick"


def test_an_unavailable_playbook_in_a_user_pulse_keeps_the_chat_error(tmp_path):
    """会話の経路: ユーザーに届く形 (error イベント + 文面の戻り値) は従来どおり。
    顛末だけが completed から error に正される。"""
    from sea.pulse_controller import ExecutionRequest

    mgr, _runtime, _persona = _real_loader_world(tmp_path, [])
    events: List[Dict[str, Any]] = []
    request = ExecutionRequest(
        type="user", persona_id="p1", building_id="room-7", user_input="こんにちは",
        meta_playbook="no_such_playbook", event_callback=events.append,
    )
    out = mgr.pulse_controller.submit(request)

    assert out == ["指定されたプレイブック 'no_such_playbook' が見つかりません。プレイブックIDを確認してください。"]
    assert {"type": "error", "code": "playbook_not_found",
            "meta_playbook": "no_such_playbook"} in events
    assert request.runtime_outcome == "error"
    assert "no_such_playbook" in (request.runtime_error or "")


# ---------------------------------------------------------------------------
# 6. 生成の直後の割り込み — 生成し終えた本文は消さずに席を譲る
# ---------------------------------------------------------------------------

TICK_NODE = dict(id="tick", memorize=True, speak=False, single_beat=True)
ACTION_TEXT = "いまは自分の時間です。\n\nこのティックは水やり。"


def _run_cancelled_loop(text: str, *, adapter: Any = None, **node_fields):
    """周の頭の取消評価で区切られる _run_spell_loop を走らせる。

    取消は生成が終わった後 (ループに入る前) に届いている — 呼び出し元の
    同期 generate の最中にユーザーが割り込んだ回と同じ形。
    """
    from sea.cancellation import CancellationToken, ExecutionCancelledException

    runtime = _LoopRuntime()
    if adapter is None:
        adapter = _Adapter()
    persona = SimpleNamespace(persona_id="p1", sai_memory=adapter)
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    token = CancellationToken()
    token.cancel(interrupted_by="user")
    state = {"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
             "_cancellation_token": token}
    order: List[str] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell({SPELL_A: ("ok", None, True),
                                           SPELL_B: ("ok", None, True)}, order)):
        with pytest.raises(ExecutionCancelledException):
            asyncio.run(runtime_llm._run_spell_loop(
                text=text, spell_enabled=True, llm_client=_NoCallClient(),
                runtime=runtime, persona=persona, building_id="b1", state=state,
                messages=[], playbook=SimpleNamespace(name="tick"),
                event_callback=None,
                node_def=SimpleNamespace(**{**TICK_NODE, **node_fields}),
                action_text=ACTION_TEXT,
            ))
    return SimpleNamespace(
        runtime=runtime, adapter=adapter, pulse_ctx=pulse_ctx, order=order,
        state=state,
    )


def test_interrupt_after_generation_keeps_the_tick_body_and_reports_unrun_spells():
    run = _run_cancelled_loop(TWO_SPELLS)

    # スペルは一つも実行されない (席を譲る — ユーザー発話最優先は変えない)
    assert run.order == []
    # 本文は周の頭の書き込みと同じ形で一件だけ記憶に入る
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    stored = run.runtime.stored_calls[0]
    assert "少し片付けよう。" in stored["text"] and "これで終わり。" in stored["text"]
    assert f"name='{SPELL_A}'" in stored["text"]
    assert f"name='{SPELL_B}'" in stored["text"]
    assert stored["tags"] == ["conversation"]
    assert stored["paired_action_text"] == ACTION_TEXT
    # Pulse ログにも本文が残る
    assert any(e.role == "assistant" and "少し片付けよう。" in e.content
               for e in run.pulse_ctx.logs)
    # 「唱えたのに起きていない」は機構の名義の知覚で次の Pulse へ届く
    pending = run.adapter.pending()
    assert len(pending) == 1
    assert "どれも実行されていません" in pending[0].content
    assert f"name='{SPELL_A}'" in pending[0].content
    assert f"name='{SPELL_B}'" in pending[0].content
    meta = json.loads(pending[0].metadata)
    assert meta["source"] == "single_beat_spells_not_run"
    assert meta["spells"] == [SPELL_A, SPELL_B]
    assert format_perception_message(pending).startswith("[システム通知]\n")


def test_interrupt_after_generation_keeps_a_tick_body_without_spells():
    run = _run_cancelled_loop("今日は静かに過ごそう。")

    assert [c["text"] for c in run.runtime.stored_calls] == ["今日は静かに過ごそう。"]
    assert run.runtime.stored_calls[0]["paired_action_text"] == ACTION_TEXT
    # 呼び出し元の memorize と同じ関数で書いた印 (二重書きの鍵)
    assert run.state.get("_beat_memorized") is True
    assert run.adapter.pending() == []


def test_interrupt_after_generation_reports_nothing_extra_without_single_beat():
    run = _run_cancelled_loop(TWO_SPELLS, single_beat=False)
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    assert run.adapter.pending() == []


def test_interrupt_after_generation_leaves_the_conversation_vessel_to_its_owner():
    """speak=true (会話の器) は建物の記録と記憶を対で持つ器。ここでは書かない。"""
    run = _run_cancelled_loop(TWO_SPELLS, speak=True, single_beat=False)
    assert run.runtime.stored_calls == []


class _CancellingClient(_TextClient):
    """生成し終えた瞬間にユーザーの割り込みが届くクライアント。"""

    def __init__(self, text: str, token):
        super().__init__(text)
        self.token = token

    def generate(self, messages, tools=None, temperature=None, **kwargs):
        self.token.cancel(interrupted_by="user")
        return self.text

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        yield self.text
        # 最後の chunk を渡し終えた直後に割り込みが届く
        self.token.cancel(interrupted_by="user")


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("text,expect_in_memory", [
    (
        "水をやろう。\n"
        f"/spell name='{SPELL_A}' args={{\"text\": \"水やり\"}}\n"
        "あとで様子を見る。",
        "水をやろう。",
    ),
    ("今日は静かに過ごそう。", "今日は静かに過ごそう。"),
])
def test_the_tick_node_keeps_its_body_when_interrupted_during_generation(
    monkeypatch, text, expect_in_memory, streaming,
):
    """本物のノード経路で、生成中の割り込みが本文を消さない (同期 / ストリーム)。"""
    from sea.cancellation import CancellationToken

    token = CancellationToken()
    runtime, events, state, adapter = _run_node(
        monkeypatch, _load_playbook("tick"), text, streaming=streaming,
        state_extra={"_cancellation_token": token},
        spell_results={SPELL_A: ("記録しました", None, True)},
        client=_CancellingClient(text, token),
        expect_exc=Exception,
    )
    stored = [c.args[1] for c in runtime._store_memory.call_args_list]
    assert len(stored) == 1 and expect_in_memory in stored[0]
    # 席は譲った — スペルは実行されず、建物にも何も出ない
    assert state["_test_spell_order"] == []
    assert _building_writes(runtime) == 0


# ---------------------------------------------------------------------------
# 7. tell の拒否・投函後の失敗が、本物のスペル経路を通って失敗の知覚に届く
# ---------------------------------------------------------------------------


def _run_tell_in_a_tick(tmp_path, spell_line: str, *, emit_result=None):
    from builtin_data.tools.tell import tell

    adapter = _Adapter()
    tell_runtime = SimpleNamespace(
        emitted=[],
        _emit_say=lambda persona, building_id, text, **kw: (
            tell_runtime.emitted.append(text) or emit_result
        ),
    )
    manager = SimpleNamespace(occupants={"cafe": ["p1"]}, sea_runtime=tell_runtime)
    persona = SimpleNamespace(
        persona_id="p1", persona_name="アリス", current_building_id="cafe",
        sai_memory=adapter, manager_ref=manager,
        persona_log_path=tmp_path / "log.json",
    )
    manager.personas = {"p1": persona}
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    state = {"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
             "_cancellation_token": None, "_persona_obj": persona}
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {"tell"}), \
         patch.object(runtime_llm, "TOOL_REGISTRY", {"tell": tell}), \
         patch("saiverse.day_plan.get_user_conversation_state", return_value=False):
        asyncio.run(runtime_llm._run_spell_loop(
            text=f"伝えよう。\n{spell_line}", spell_enabled=True,
            llm_client=_NoCallClient(), runtime=_LoopRuntime(), persona=persona,
            building_id="cafe", state=state, messages=[],
            playbook=SimpleNamespace(name="tick"), event_callback=None,
            node_def=SimpleNamespace(**TICK_NODE),
        ))
    return adapter, tell_runtime


def test_a_refused_tell_reaches_the_persona_as_a_failure(tmp_path):
    adapter, tell_runtime = _run_tell_in_a_tick(
        tmp_path,
        "/spell name='tell' args={\"target\": \"どこかの誰か\", \"message\": \"こんにちは\"}",
    )
    assert tell_runtime.emitted == []
    pending = adapter.pending()
    assert len(pending) == 1
    assert "この場所にいません" in pending[0].content
    assert FAIL_MARK in pending[0].content
    assert json.loads(pending[0].metadata)["failed"] == ["tell"]


def test_a_tell_that_went_out_but_was_not_recorded_reaches_the_persona(tmp_path):
    adapter, tell_runtime = _run_tell_in_a_tick(
        tmp_path,
        "/spell name='tell' args={\"target\": \"user\", \"message\": \"まはー、聞いて。\"}",
        emit_result={"role": "assistant"},  # 採番されなかった = 記録に残らず
    )
    assert tell_runtime.emitted == ["まはー、聞いて。"]
    pending = adapter.pending()
    assert len(pending) == 1
    assert "履歴に残せませんでした" in pending[0].content
    assert "慎重に決めてください" in pending[0].content


def test_a_successful_tell_reports_its_outcome_without_a_failure_mark(tmp_path):
    """成功した tell の帰結も届く (冗長でも全部届ける一本の規則を優先)。"""
    adapter, tell_runtime = _run_tell_in_a_tick(
        tmp_path,
        "/spell name='tell' args={\"target\": \"user\", \"message\": \"まはー、聞いて。\"}",
        emit_result={"role": "assistant", "message_id": "b:1"},
    )
    assert tell_runtime.emitted == ["まはー、聞いて。"]
    (item,) = adapter.pending()
    assert "に声をかけました。" in item.content
    assert FAIL_MARK not in item.content
    assert json.loads(item.metadata)["failed"] == []


# ---------------------------------------------------------------------------
# 7. 帰結の配送の失敗 — 黙って正常完了にしない (Codex 敵対レビュー 3 巡目 high)
# ---------------------------------------------------------------------------


OK_RESULTS = {
    SPELL_A: ("一つ目を記録しました", None, True),
    SPELL_B: ("二つ目を記録しました", None, True),
}


def test_a_delivery_failure_after_a_completed_round_fails_the_beat():
    """積む呼び出しが例外を投げたら、配送の失敗を stop_error で返す (再実行しない)。"""
    run = _run_single_beat_loop(TWO_SPELLS, OK_RESULTS, adapter=_FailingAdapter())

    assert run.order == [SPELL_A, SPELL_B]  # スペルは一度ずつだけ
    assert run.client.calls == 0
    err = run.result.stop_error
    assert isinstance(err, runtime_llm.SpellOutcomeDeliveryError)
    assert "スペルは実行済み" in str(err)
    assert "配送だけが失敗" in str(err)
    assert "database is locked" in str(err)
    assert isinstance(err.__cause__, sqlite3.OperationalError)
    # 届かなかった帰結の本文は例外が控えとして持つ
    assert "→ 一つ目を記録しました" in err.outcome_text
    assert "→ 二つ目を記録しました" in err.outcome_text
    # 結果つきの周のセグメントは呼び出し元が建物へ書く材料として残る
    (segment,) = run.result.segments
    assert "一つ目を記録しました" in segment.text
    assert segment.form == runtime_llm.SAVED_FORM_SPELL_RESULTS
    # 記憶は周の本文だけ (配送の失敗の回も結果を記憶へは書かない)
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]


@pytest.mark.parametrize("adapter,cause", [
    (None, "知覚バッファがありません"),
    (_NotReadyAdapter(), "接続されていません"),
])
def test_a_missing_or_unready_adapter_is_a_delivery_failure(adapter, cause):
    run = _run_single_beat_loop(TWO_SPELLS, OK_RESULTS, adapter=adapter)
    err = run.result.stop_error
    assert isinstance(err, runtime_llm.SpellOutcomeDeliveryError)
    assert cause in str(err)
    assert run.order == [SPELL_A, SPELL_B]


def test_a_streaming_delivery_failure_raises_after_leaving_the_round_body():
    """下書き行のある経路: 結果つきの周の本文を器に置いてから投げる。

    Beat の出口の保存 (``_save_draft_on_beat_death``) がその本文で下書き行を
    確定する — 止まった周と同じ口。周の本文はもう記憶にあるので記憶の本文は空。
    """
    runtime = _LoopRuntime()
    persona = SimpleNamespace(persona_id="p1", sai_memory=_FailingAdapter())
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    streaming_state: Dict[str, Any] = {
        "msg_id": "msg-1", "building_id": "b1", "sub_seq": 0,
        "finalized": False, "placeholder_round": 1, "cancellation_token": None,
    }
    order: List[str] = []
    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async",
                      new=_scripted_spell(OK_RESULTS, order)):
        with pytest.raises(runtime_llm.SpellOutcomeDeliveryError):
            asyncio.run(runtime_llm._run_spell_loop(
                text=TWO_SPELLS, spell_enabled=True, llm_client=_NoCallClient(),
                runtime=runtime, persona=persona, building_id="b1",
                state={"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
                       "_cancellation_token": None},
                messages=[], playbook=SimpleNamespace(name="tick"),
                event_callback=None, node_def=SimpleNamespace(**TICK_NODE),
                pipeline_streaming_state=streaming_state,
            ))
    assert order == [SPELL_A, SPELL_B]
    salvage = streaming_state["salvage"]
    assert salvage["msg_id"] == "msg-1"
    assert "一つ目を記録しました" in salvage["text"]
    assert salvage["form"] == runtime_llm.SAVED_FORM_SPELL_RESULTS
    assert salvage["memory_text"] == ""


def test_a_successful_delivery_still_closes_the_beat_normally():
    run = _run_single_beat_loop(TWO_SPELLS, OK_RESULTS)
    assert run.result.stop_error is None
    assert len(run.adapter.pending()) == 1


@pytest.mark.parametrize("streaming", [True, False])
def test_the_tick_node_fails_when_the_outcome_cannot_be_delivered(
    monkeypatch, streaming,
):
    """本物のノード経路: 配送の失敗は LLMError に包まれてノードから上がる。"""
    from llm_clients.exceptions import LLMError

    tick = _load_playbook("tick")
    text = f"水をやろう。\n/spell name='{SPELL_A}' args={{\"text\": \"水やり\"}}"
    runtime, events, state, adapter = _run_node(
        monkeypatch, tick, text, streaming=streaming,
        spell_results={SPELL_A: ("記録しました", None, True)},
        adapter=_FailingAdapter(), expect_exc=LLMError,
    )
    assert state["_test_spell_order"] == [SPELL_A]  # 再実行しない
    assert _building_writes(runtime) == 0


def test_the_tick_route_reports_an_undelivered_outcome_as_an_error(monkeypatch):
    """本物の PulseController を通すと runtime_outcome="error" + 理由の文面になる。"""
    from api.routes.people.tick import fire_tick

    tick = _load_playbook("tick")
    text = f"水をやろう。\n/spell name='{SPELL_A}' args={{\"text\": \"水やり\"}}"

    def behavior():
        _run_node(
            monkeypatch, tick, text, streaming=False,
            spell_results={SPELL_A: ("記録しました", None, True)},
            adapter=_FailingAdapter(),
        )
        return []

    resp = fire_tick("p1", None, manager=_real_controller_manager(behavior))
    assert resp.executed is False
    assert resp.outcome == "error"
    assert "SpellOutcomeDeliveryError" in resp.error
    assert "スペルは実行済み" in resp.error


def _run_stopped_mid_round(adapter, a_result: tuple):
    """SPELL_A を実行し終え、SPELL_B の実行中に取り消される単発の周を走らせる。"""
    from sea.cancellation import ExecutionCancelledException

    runtime = _LoopRuntime()
    persona = SimpleNamespace(persona_id="p1", sai_memory=adapter)
    pulse_ctx = PulseContext(pulse_id="pulse-1")
    pulse_ctx.push_line(aspect=Aspect.AUTONOMOUS)
    calls: List[str] = []

    async def fake(tool_name, tool_args, persona, state, playbook_name,
                   event_callback, messages=None):
        calls.append(tool_name)
        if tool_name == SPELL_B:
            raise ExecutionCancelledException(interrupted_by="user")
        return a_result

    with patch.object(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_A, SPELL_B}), \
         patch.object(runtime_llm, "_run_spell_tool_async", new=fake):
        result = asyncio.run(runtime_llm._run_spell_loop(
            text=PROSE_BETWEEN, spell_enabled=True, llm_client=_NoCallClient(),
            runtime=runtime, persona=persona, building_id="b1",
            state={"_pulse_id": "pulse-1", "_pulse_context": pulse_ctx,
                   "_cancellation_token": None},
            messages=[], playbook=SimpleNamespace(name="tick"),
            event_callback=None, node_def=SimpleNamespace(**TICK_NODE),
        ))
    return SimpleNamespace(result=result, runtime=runtime, calls=calls)


def test_a_delivery_failure_in_a_stopped_round_keeps_the_stop_and_logs_an_error(caplog):
    """止まった周: 取消の例外はすり替わらず、ERROR が残り、結果は記憶へ逃がさない。"""
    import logging

    from sea.cancellation import ExecutionCancelledException

    with caplog.at_level(logging.ERROR, logger=runtime_llm.LOGGER.name):
        run = _run_stopped_mid_round(_FailingAdapter(), ("ok-a", None, True))

    assert run.calls == [SPELL_A, SPELL_B]
    assert isinstance(run.result.stop_error, ExecutionCancelledException)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "stopped round" in r.getMessage() and "→ ok-a" in r.getMessage()
        for r in errors
    )
    # 「届けた」の印が立たなかった回でも、単発の Beat は結果を記憶へ書かない
    # (旧形は印を配送の前に立てていたので、失敗しても届けた扱いになっていた)。
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]


def test_a_delivery_failure_before_the_first_round_keeps_the_stop(caplog):
    import logging

    with caplog.at_level(logging.ERROR, logger=runtime_llm.LOGGER.name):
        run = _run_cancelled_loop(PROSE_BETWEEN, adapter=_FailingAdapter())

    # 本文は記憶に入り、取消の例外は _run_cancelled_loop の raises が受けている
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    assert any(
        "stop before the first round" in r.getMessage()
        and "実行されていません" in r.getMessage()
        for r in caplog.records if r.levelno >= logging.ERROR
    )


# ---------------------------------------------------------------------------
# 8. 帰結の知覚にスペル結果の添付 (media) を引き継ぐ (同 3 巡目 medium)
# ---------------------------------------------------------------------------


IMG_A = {"path": "/tmp/garden.png", "mime_type": "image/png"}
IMG_B = {"path": "/tmp/notes.png", "mime_type": "image/png"}


def test_a_completed_round_carries_the_result_media_into_the_perception():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("画像を作りました", {"media": [IMG_A]}, True),
        SPELL_B: ("メモを貼りました", {"media": [IMG_B]}, True),
    })
    (item,) = run.adapter.pending()
    assert item.media_list() == [IMG_A, IMG_B]


def test_a_round_without_media_pushes_no_media():
    run = _run_single_beat_loop(TWO_SPELLS, OK_RESULTS)
    (item,) = run.adapter.pending()
    assert item.media is None


def test_a_stopped_round_carries_the_received_result_media_into_the_perception():
    adapter = _Adapter()
    run = _run_stopped_mid_round(adapter, ("画像を作りました", {"media": [IMG_A]}, True))
    assert run.calls == [SPELL_A, SPELL_B]
    (item,) = adapter.pending()
    assert "→ 画像を作りました" in item.content
    assert item.media_list() == [IMG_A]


class _DummyEmbedder:
    def __init__(self, model=None, **kwargs):
        self.model_name = model

    def embed(self, texts, **kwargs):
        return [[0.0] * 3 for _ in texts]


def test_the_next_pulse_head_consumes_the_outcome_with_its_media(tmp_path):
    """消費の側 (次の Pulse の頭の知覚消費) まで media が同じ形で届く。

    本物の SAIMemoryAdapter の ``flush_perception_buffer_payload`` が返す
    ``media`` は、提示ブロックの ``metadata.media`` (LLM クライアントが添付と
    して読む形) にそのまま入る。
    """
    persona_dir = tmp_path / "personas" / "p1"
    persona_dir.mkdir(parents=True)
    with patch("saiverse_memory.adapter.Embedder", _DummyEmbedder):
        from saiverse_memory import SAIMemoryAdapter
        adapter = SAIMemoryAdapter("p1", persona_dir=persona_dir, resource_id="p1")
        try:
            run = _run_single_beat_loop(TWO_SPELLS, {
                SPELL_A: ("画像を作りました", {"media": [IMG_A]}, True),
                SPELL_B: ("ok", None, True),
            }, adapter=adapter)
            assert run.result.stop_error is None
            payload = adapter.flush_perception_buffer_payload(pulse_id="pulse-2")
        finally:
            adapter.close()
    assert payload is not None
    assert "→ 画像を作りました" in payload["content"]
    assert payload["media"] == [IMG_A]
