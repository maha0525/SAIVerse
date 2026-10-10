"""ティックの器と単発制御 (一回で閉じる Beat) の回帰。

設計: docs/intent/autonomous_behavior_v3.md §5「ティックは 1 Beat で閉じる」/
docs/intent/autonomous_behavior_v04_plan.md 段 2。

固定する契約:

1. 自律の Pulse (``Aspect.AUTONOMOUS``) は標準モデル・メインライン・committed。
2. ``single_beat`` の LLM ノードは、生成に含まれたスペルをテキスト順に一度だけ
   実行し、結果を続きの生成に回さない (再呼び出しが起きない)。
3. 成功の帰結には何もしない (記憶にも続きの材料にも結果を入れない)。失敗だけが
   知覚 (``[システム通知]``) として次の Pulse の頭へ届く。スペル行と結果は
   pulse_logs (PulseContext) には残る。
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


def _run_single_beat_loop(text: str, results: Dict[str, tuple]):
    runtime = _LoopRuntime()
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
    # (system 行) は書かれない — 成功の帰結は世界の記録が運ぶ。
    assert [c["role"] for c in run.runtime.stored_calls] == ["assistant"]
    body = run.runtime.stored_calls[0]["text"]
    assert "少し片付けよう。" in body and "これで終わり。" in body
    assert f"name='{SPELL_A}'" in body and f"name='{SPELL_B}'" in body
    assert not any("記録しました" in c["text"] for c in run.runtime.stored_calls)
    # 続きの材料にも結果を積まない
    assert not any("[Spell Result" in str(m.get("content")) for m in run.messages)
    # 成功だけなら知覚は届かない
    assert run.adapter.pending() == []


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
# 3. 失敗だけが知覚で次の Pulse の頭へ届く
# ---------------------------------------------------------------------------


def test_a_failed_spell_reaches_the_next_pulse_as_a_system_notice():
    run = _run_single_beat_loop(TWO_SPELLS, {
        SPELL_A: ("一つ目を記録しました", None, True),
        SPELL_B: ("Spell error (memo_add): ValueError: boom", None, False),
    })

    pending = run.adapter.pending()
    assert len(pending) == 1
    item = pending[0]
    assert item.kind == runtime_llm.SINGLE_BEAT_SPELL_FAILURE_KIND
    # 失敗した行と失敗の文面だけ。成功した行は載らない。
    assert f"name='{SPELL_B}'" in item.content
    assert "ValueError: boom" in item.content
    assert f"name='{SPELL_A}'" not in item.content
    meta = json.loads(item.metadata)
    assert meta["spells"] == [SPELL_B]
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
    assert "該当なし" in pending[0].content


def test_an_unknown_spell_counts_as_a_failure():
    text = "やってみる。\n/spell name='no_such_spell' args={}"
    run = _run_single_beat_loop(text, {})
    assert run.order == []  # 実行はされない
    pending = run.adapter.pending()
    assert len(pending) == 1
    assert "no_such_spell" in pending[0].content
    assert run.client.calls == 0


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
              client: Any = None, expect_exc: Any = None):
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


# ---------------------------------------------------------------------------
# 6. 生成の直後の割り込み — 生成し終えた本文は消さずに席を譲る
# ---------------------------------------------------------------------------

TICK_NODE = dict(id="tick", memorize=True, speak=False, single_beat=True)
ACTION_TEXT = "いまは自分の時間です。\n\nこのティックは水やり。"


def _run_cancelled_loop(text: str, **node_fields):
    """周の頭の取消評価で区切られる _run_spell_loop を走らせる。

    取消は生成が終わった後 (ループに入る前) に届いている — 呼び出し元の
    同期 generate の最中にユーザーが割り込んだ回と同じ形。
    """
    from sea.cancellation import CancellationToken, ExecutionCancelledException

    runtime = _LoopRuntime()
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


def test_a_successful_tell_sends_no_failure_notice(tmp_path):
    adapter, tell_runtime = _run_tell_in_a_tick(
        tmp_path,
        "/spell name='tell' args={\"target\": \"user\", \"message\": \"まはー、聞いて。\"}",
        emit_result={"role": "assistant", "message_id": "b:1"},
    )
    assert tell_runtime.emitted == ["まはー、聞いて。"]
    assert adapter.pending() == []
