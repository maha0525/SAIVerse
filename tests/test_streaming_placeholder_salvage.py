"""下書き行 (placeholder) の孤児化防止 — Beat がどう死んでも発言を消さない。

docs/issues/orphaned_streaming_placeholder_cleanup.md 候補 1 (2026-08-27)。

下書き行は本文の器で、確定して初めて中身が入る。Beat が例外で死ぬと確定が走らず、
ペルソナが実際に喋った内容がどこにも残らない (2026-05-19〜08-26 の 3 ヶ月で 32 件)。
ここで固定する不変条件は一つ — **lg_llm_node の node() は、未確定の下書き行を
残して終わらない**。ユーザーの停止・LLM エラー・タスク破棄のどれで死んでも、
出口の後始末 (`_settle_placeholder_on_beat_death`) が下書き行を確定させる。
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from llm_clients.exceptions import LLMError
from sea import runtime_llm
from sea.cancellation import ExecutionCancelledException
from sea.runtime_llm import INTERRUPTED_METADATA_KEY, _settle_interrupted_utterance


# ---------------------------------------------------------------------------
# _settle_interrupted_utterance 単体 — 原因 (by_user) で変わるのは通告の文面だけ
# ---------------------------------------------------------------------------

def _settle(occupants=("1", "p1"), **overrides):
    runtime = MagicMock()
    # 通告の heard_by は在室者から組む。MagicMock のままだと list() で落ちて
    # 通告ごと握り潰される (それはそれでテストが赤くなるが、原因が読めない)。
    runtime.manager.occupants = {"b1": list(occupants)}
    persona = SimpleNamespace(persona_id="p1", history_manager=MagicMock())
    state = {}
    events: list = []
    params = dict(
        runtime=runtime,
        persona=persona,
        state=state,
        node_def=SimpleNamespace(id="llm"),
        playbook=SimpleNamespace(name="pb"),
        event_callback=events.append,
        building_id="b1",
        msg_id="m1",
        sub_seq=0,
        text="言いかけた本文",
        by_user=True,
    )
    params.update(overrides)
    seq = _settle_interrupted_utterance(**params)
    return runtime, persona, state, events, seq


def _notice_content(persona) -> str:
    call = persona.history_manager.add_to_building_only.call_args
    assert call is not None, "中断の通告が建物の記録に書かれていない"
    building_id, message = call.args
    assert building_id == "b1"
    assert message["role"] == "host"
    # heard_by 無しの通告は、取り込み (get_building_messages) が永遠にスキップ
    # するので誰の記憶にも届かない (2026-08-27 実機検証で発覚)。在室者が必須。
    assert call.kwargs.get("heard_by"), "通告に heard_by (在室者) が渡っていない"
    return message["content"]


def test_a_user_stop_writes_the_user_notice():
    runtime, persona, state, events, _ = _settle(by_user=True)
    assert "ユーザーの操作により" in _notice_content(persona)
    assert state[INTERRUPTED_METADATA_KEY] is True


def test_a_beat_error_writes_a_notice_without_naming_a_cause():
    """非ユーザー起点の中断 — 通告は原因を書かない一文 (「エラー」と括ると
    schedule/auto の割り込みの回に嘘になる。2026-08-27 まはー委任で採用)。"""
    runtime, persona, state, events, _ = _settle(by_user=False)
    notice = _notice_content(persona)
    assert notice == "(ここで発言が中断されました)"
    assert "ユーザーの操作" not in notice
    # 確定・印・記憶は原因によらず揃う
    runtime._emit_speak_finalize.assert_called_once()
    runtime._store_memory.assert_called_once()
    assert [e for e in events if e.get("interrupted")]


def test_an_empty_body_settles_quietly():
    """一言も出ないうちに死んだ回は、空文字で確定するだけで何も語らない。"""
    runtime, persona, state, events, _ = _settle(text="", by_user=False)
    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == ""
    runtime._store_memory.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()
    assert events == []
    assert INTERRUPTED_METADATA_KEY not in state


def test_the_notice_reaches_every_occupant_including_the_speaker():
    """通告の heard_by は在室者全員。在室者リストに発話者本人が欠けていても
    補う (emit_speak_start と同じ規律)。"""
    _, persona, _, _, _ = _settle()
    call = persona.history_manager.add_to_building_only.call_args
    assert call.kwargs["heard_by"] == ["1", "p1"]

    _, persona2, _, _, _ = _settle(occupants=("1",))
    call2 = persona2.history_manager.add_to_building_only.call_args
    assert call2.kwargs["heard_by"] == ["1", "p1"]


def test_a_failing_ui_event_does_not_stop_memory_and_notice():
    """印の配達に失敗しても、記憶と通告の書き込みは進む。"""

    def _raiser(event):
        raise RuntimeError("ui gone")

    runtime, persona, state, events, _ = _settle(event_callback=_raiser)
    runtime._store_memory.assert_called_once()
    persona.history_manager.add_to_building_only.assert_called_once()


def test_the_settle_marks_the_beat_as_memorized_on_success():
    """記憶へ書けた回は「この Beat の本文はもう記憶に書かれた」の印が立つ。
    Beat の出口の補填 (`_backfill_memory_on_beat_death`) がこの印を見て、
    同じ本文を二重に書かない。"""
    _, _, state, _, _ = _settle(by_user=False)
    assert state["_beat_memorized"] is True


def test_an_empty_body_leaves_no_memorized_mark():
    """一言も出ないうちに死んだ回は記憶に書かないので、印も立たない。"""
    _, _, state, _, _ = _settle(text="", by_user=False)
    assert "_beat_memorized" not in state


# ---------------------------------------------------------------------------
# node() 全体 — Beat の出口の不変条件
# ---------------------------------------------------------------------------

class _FakeStreamClient:
    """generate_stream だけを持つ最小の LLM クライアント。

    ``call_exc`` は呼び出しの瞬間に、``iter_exc`` は chunk を流し終えた後に
    例外を投げる。どちらも「LLM 呼び出し中に Beat が死ぬ」形の再現用。
    """

    config_key = None

    def __init__(self, chunks=None, call_exc=None, iter_exc=None):
        self._chunks = list(chunks or [])
        self._call_exc = call_exc
        self._iter_exc = iter_exc

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        if self._call_exc is not None:
            raise self._call_exc

        def _gen():
            yield from self._chunks
            if self._iter_exc is not None:
                raise self._iter_exc

        return _gen()

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


class _CancelDuringStream:
    """1 chunk 目を通した後に取り消しへ倒れる cancellation token。

    node() 冒頭の ``raise_if_cancelled`` は素通しし、ストリーム消費中の
    ``is_cancelled`` 判定が 2 回目から True になる — 「ストリームの途中で
    停止ボタンが押された」形の再現用。
    """

    interrupted_by = "user"

    def __init__(self):
        self._checks = 0

    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        self._checks += 1
        return self._checks > 1


def _node_def():
    return SimpleNamespace(
        id="llm",
        speak=True,
        action=None,
        available_tools=None,
        response_schema=None,
        response_schema_source=None,
        output_key=None,
        output_keys=None,
        metadata_key=None,
        memorize=None,
        important=False,
        label=None,
    )


def _build_node(monkeypatch, *, client, spell_loop, node_def=None):
    runtime = MagicMock()
    runtime.manager.occupants = {"b1": ["1"]}
    runtime._effective_building_id.return_value = "b1"
    runtime._emit_speak_start.return_value = "msg-1"
    # 確定は三値の結果を返す (sea/runtime_emitters.py の SpeakFinalizeResult)。
    # 素の MagicMock を返すと status が "saved" と一致せず、呼び出し元が
    # 「確定できなかった」側 (= salvage 続行) に倒れてテストの前提が崩れる。
    from sea.runtime_emitters import SpeakFinalizeResult
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="saved",
        building_msg={"message_id": "msg-1", "content": "こんにちは。"},
    )
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime._store_memory.return_value = "mem-1"
    runtime.select_llm_client.return_value = (client, "model-a")

    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: True)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))
    monkeypatch.setattr(runtime_llm, "_finalize_beat", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_run_spell_loop", spell_loop)

    # persona_id=None で node_with_persona_context の wrap を素通しし、
    # persona_context 依存なしで node 本体だけを走らせる
    # (tests/test_spell_auto_mode_w10.py と同じ手)。
    persona = SimpleNamespace(
        persona_id=None, persona_name="p", history_manager=MagicMock(),
    )
    events: list = []
    node = runtime_llm.lg_llm_node(
        runtime, node_def or _node_def(), persona, "b1", SimpleNamespace(name="pb"),
        events.append,
    )
    return runtime, persona, node, events


async def _spell_loop_raising(exc):
    raise exc


def test_a_stream_call_that_dies_leaves_no_empty_record(monkeypatch):
    """LLM 呼び出しが即死した回 — 本文ゼロの下書き行は確定せず、取り下げる。

    2026-09-13 以前はここで空文字の確定を打っていた。本文の無い記録が建物と
    ペルソナのログに永続し、下書きの印も倒れて孤児掃除の網から外れる —
    締めの Beat で塞いだ H-1 と同じ形だったので、Beat の出口でも同じ判定を
    通す (docs/issues/pulse_beats_merge_into_single_record.md の N-1)。
    """
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = True
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-1"
    # 本文が無いので、記憶にも建物の通告にも何も書かない
    runtime._store_memory.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_stop_before_the_first_token_leaves_no_empty_record(monkeypatch):
    """ストリームが一語も返さないうちの停止 — 空の記録を残さず行を取り下げる。

    ストリーム中の停止を受け止める経路 (`cancelled_during_stream`) も、
    Beat の出口と締めの Beat と同じ判定を通す (同族の三箇所目)。
    """
    # 最初の chunk を受け取る前に停止が立つ (chunk は捨てられ、本文は空)。
    client = _FakeStreamClient(chunks=["これから喋るはずだった。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._withdraw_speak_placeholder.return_value = True

    class _CancelImmediately:
        interrupted_by = "user"

        def raise_if_cancelled(self):
            return None

        def is_cancelled(self):
            return True

    asyncio.run(node({
        "_messages": [], "_pulse_id": "pl-1",
        "_cancellation_token": _CancelImmediately(),
    }))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-1"
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_refused_withdrawal_falls_back_to_confirming_the_row(monkeypatch):
    """取り下げを断られた回は空文字でも確定する — 未確定の行を残さない。"""
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._withdraw_speak_placeholder.return_value = False
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    args = runtime._emit_speak_finalize.call_args.args
    assert args[2] == "msg-1"
    assert args[3] == ""


def test_a_spell_loop_death_confirms_the_draft_row_with_the_spoken_text(monkeypatch):
    """喋り終えた後に Beat が死んだ回 — 本文つきで確定し、印・記憶・通告が揃う。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise RuntimeError("boom after generation")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[2] == "msg-1"
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    # sub-speak が 1 番まで出た後なので、final は 2 番 (連番の衝突なし)
    assert call.kwargs["final_sub_seq"] == 2

    runtime._store_memory.assert_called_once()
    assert persona.history_manager.add_to_building_only.call_args.args[1]["content"] == "(ここで発言が中断されました)"
    assert [e for e in events if e.get("type") == "streaming_complete" and e.get("interrupted")]


def test_a_late_stop_still_reads_as_a_user_interruption(monkeypatch):
    """生成し終えた直後の停止 (2026-08-26 実機の形) — 通告はユーザーの操作の文面。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("stopped", interrupted_by="user")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == "こんにちは。"
    assert "ユーザーの操作により" in persona.history_manager.add_to_building_only.call_args.args[1]["content"]


def test_a_schedule_preemption_is_not_reported_as_a_user_stop(monkeypatch):
    """schedule の割り込みで切られた回 — 通告を「ユーザーの操作により」と書かない。

    取り消しの例外型はユーザー停止と同じなので、型で判定すると機構起点の中断に
    ユーザー起点の通告が捏造される。判定は取り消しに刻まれた原因
    (`interrupted_by`) から行う (2026-08-27 Codex 指摘の固定)。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("preempted", interrupted_by="schedule")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    notice = persona.history_manager.add_to_building_only.call_args.args[1]["content"]
    assert "ユーザーの操作" not in notice


def test_a_server_shutdown_is_not_reported_as_a_user_stop(monkeypatch):
    """サーバー終了 (Ctrl+C / SIGTERM) で切られた回 — 取り消しには
    "server_shutdown" が刻まれる (PulseController.shutdown)。ユーザー起点では
    ないので、通告は原因を書かない非ユーザー形になる。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ExecutionCancelledException("shutdown", interrupted_by="server_shutdown")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    notice = persona.history_manager.add_to_building_only.call_args.args[1]["content"]
    assert notice == "(ここで発言が中断されました)"
    assert "ユーザーの操作" not in notice


def test_a_keyboard_interrupt_propagates_without_salvage_side_effects(monkeypatch):
    """KeyboardInterrupt はインタープリタ終了の道筋 — DB 書き込みの副作用を
    足さず、そのまま伝播させる (捕捉を CancelledError に絞った境界の固定)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise KeyboardInterrupt()

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_task_cancellation_confirms_the_draft_row_and_propagates(monkeypatch):
    """asyncio.CancelledError (サーバー停止等) は Exception に捕まらないが、
    下書き行は同じく確定させてから、そのままの型で伝播する。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise asyncio.CancelledError()

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[3] == "こんにちは。"


def test_a_mid_stream_death_confirms_the_draft_row_with_the_partial_text(monkeypatch):
    """chunk を流している最中に client が例外を投げた回。

    返り値の tuple は届かないが、途中経過の器 (`_stream_progress`) 経由で
    受信済みの部分文が回収され、下書き行はその本文で確定する。final の連番も
    発火済みの sub-speak (seq=1) と衝突しない (2026-08-27 Codex 指摘の固定)。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"], iter_exc=RuntimeError("died mid-stream"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[2] == "msg-1"
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["final_sub_seq"] == 2
    assert call.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    assert persona.history_manager.add_to_building_only.call_args.args[1]["content"] == "(ここで発言が中断されました)"


def test_a_stop_during_the_stream_settles_inside_and_not_again_at_the_exit(monkeypatch):
    """ストリーム途中の停止 — try 塊の中で確定した後、Beat の出口が二重確定しない。

    この経路だけは settle の後もコードが続く (spell round の頭で取り消しが
    見つかって例外で抜けるまで)。確定済みの印 (`pipeline_finalized`) が
    出口の後始末を止めることを、実物の `_run_spell_loop` ごと通して固定する。
    """
    client = _FakeStreamClient(chunks=["こんにちは。", "続きの文。"])

    runtime = MagicMock()
    runtime._effective_building_id.return_value = "b1"
    runtime._emit_speak_start.return_value = "msg-1"
    runtime._default_temperature.return_value = 0.7
    runtime._get_cache_kwargs.return_value = {}
    runtime._store_memory.return_value = "mem-1"
    runtime.select_llm_client.return_value = (client, "model-a")

    monkeypatch.setattr(
        runtime_llm, "resolve_execution_context",
        lambda persona, pulse_context, state=None: SimpleNamespace(model_key="model-a"),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: True)
    monkeypatch.setattr(runtime_llm, "_record_llm_usage", lambda *a, **k: None)
    monkeypatch.setattr(runtime_llm, "_consume_reasoning", lambda *a, **k: ("", None))
    monkeypatch.setattr(runtime_llm, "_finalize_beat", lambda *a, **k: None)
    # _run_spell_loop は実物 — round 頭の取り消し検査で
    # ExecutionCancelledException を投げる本物の経路を通す。

    persona = SimpleNamespace(
        persona_id=None, persona_name="p", history_manager=MagicMock(),
    )
    events: list = []
    node = runtime_llm.lg_llm_node(
        runtime, _node_def(), persona, "b1", SimpleNamespace(name="pb"),
        events.append,
    )
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_cancellation_token": _CancelDuringStream(),
        # spell を有効にして、実物の spell loop が round 頭の取り消し検査で
        # ExecutionCancelledException を投げる経路を通す (無効だと入り口で
        # 即 return し、Beat は例外なしで静かに閉じる — それはそれで正しい)。
        "_spell_enabled": True,
        "_realtime_spells_executed": True,
    }
    with pytest.raises(LLMError):
        asyncio.run(node(state))

    # 確定は 1 回だけ (途中停止の後片付けが行い、出口は印を見て手を出さない)
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    # 通告も 1 回だけで、文面はユーザーの操作
    persona.history_manager.add_to_building_only.assert_called_once()
    assert "ユーザーの操作により" in persona.history_manager.add_to_building_only.call_args.args[1]["content"]
    runtime._store_memory.assert_called_once()


def test_an_exception_after_the_finalize_does_not_settle_twice(monkeypatch):
    """通常確定の後で Beat が死んだ回 — 出口の後始末は確定済みの印を見て手を出さない。

    印 (`pipeline_finalized = True`) を消すとこのテストが落ちる。確定の後に
    もう一度 settle が走ると、確定済みの本文が古い値で上書きされ、中断の通告と
    記憶書き込みが捏造される。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    # 確定 (branch 3) の後に通る _dump_llm_io で Beat を落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after finalize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    extra = runtime._emit_speak_finalize.call_args.kwargs.get("extra_metadata") or {}
    assert INTERRUPTED_METADATA_KEY not in extra
    persona.history_manager.add_to_building_only.assert_not_called()
    runtime._store_memory.assert_not_called()


def test_a_failed_finalize_keeps_the_exit_cleanup_armed(monkeypatch):
    """確定が「保存できなかった」を返した回は、確定済みの印を立てない (Codex #2)。

    かつては確定を**呼んだだけ**で印を立てていた — 保存に失敗していても
    Beat 死亡時の救済 (この出口の後始末) が「もう確定した」と誤認し、下書き行が
    空のまま永久に残った。saved のときだけ印を立てれば、その後 Beat が死んだ
    回に救済がもう一度確定を試みる。
    """
    from sea.runtime_emitters import SpeakFinalizeResult

    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="failed", error="db down",
    )
    # 確定 (失敗) の後に通る _dump_llm_io で Beat を落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after finalize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 1 回目 = 通常確定 (失敗)、2 回目 = 出口の後始末による再試行。
    assert runtime._emit_speak_finalize.call_count == 2


def test_a_missing_draft_row_makes_the_exit_cleanup_a_no_op(monkeypatch):
    """下書き行をそもそも作れなかった回 — 出口の後始末は何もしない。"""
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    runtime._emit_speak_start.return_value = None

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


def test_the_fallback_emit_carries_the_event_callback_for_the_signal(monkeypatch):
    """下書き行を作れなかった回の fallback (_emit_say 直書き) も、保存完了
    イベントの口 (event_callback) を運ぶ (Codex #1)。

    信号の発火は emit_say 内の共通の口が行うので、ここで渡し忘れると
    この救済経路で保存された発言だけ信号が欠ける。
    """
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    # 下書き行の発番に失敗 → 正常完了時に _emit_say fallback が走る。
    runtime._emit_speak_start.return_value = None

    asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_not_called()
    runtime._emit_say.assert_called_once()
    assert runtime._emit_say.call_args.kwargs["event_callback"] is not None


def test_a_failing_cleanup_does_not_replace_the_original_exception(monkeypatch):
    """出口の後始末が自分で失敗しても、Beat を落とした元の例外がそのまま届く。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise ValueError("the original failure")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
    )

    def _settle_raises(**kwargs):
        raise RuntimeError("cleanup itself failed")

    monkeypatch.setattr(runtime_llm, "_settle_interrupted_utterance", _settle_raises)

    with pytest.raises(LLMError) as excinfo:
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    assert isinstance(excinfo.value.original_error, ValueError)


def test_a_normal_completion_finalizes_exactly_once(monkeypatch):
    """正常完了の回に出口の後始末が二重確定しないこと (確定済みの印の検算)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    state = {"_messages": [], "_pulse_id": "pl-1"}
    result = asyncio.run(node(state))

    assert result is state
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    extra = call.kwargs.get("extra_metadata") or {}
    assert INTERRUPTED_METADATA_KEY not in extra
    # 中断の通告も記憶書き込みも走らない
    persona.history_manager.add_to_building_only.assert_not_called()
    runtime._store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# Beat の出口の記憶補填 (`_backfill_memory_on_beat_death`) — 確定は済んだのに
# memorize (`_finalize_beat`) が走る前に Beat が死ぬと、建物の記録には全文が
# あるのに本人の記憶には何も残らない。下書き行の後始末は「確定済み」でスキップ
# するので、記憶の欠けは補填が塞ぐ。
# ---------------------------------------------------------------------------

def _memorize_node_def():
    node_def = _node_def()
    node_def.memorize = True
    return node_def


def test_a_death_after_finalize_but_before_memorize_backfills_the_memory(
    monkeypatch, caplog,
):
    """確定後・memorize 前の例外死 — 本人の記憶が本文つきで補填される。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    # 確定 (branch 3) の後、_finalize_beat の手前で通る _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom before memorize")

    with pytest.raises(LLMError), caplog.at_level(logging.INFO, logger="sea.runtime_llm"):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 補填の成功は INFO の印を残す — 実地での発火をログの grep で数えるための
    # 目印 (2026-08-29 まはー承認)。文言を変えるときは監視の grep も一緒に。
    assert any(
        "memory backfill on beat death succeeded" in rec.message
        for rec in caplog.records
    )

    # 確定は通常経路の 1 回だけ (出口の後始末は確定済みの印を見て手を出さない)
    runtime._emit_speak_finalize.assert_called_once()
    # 記憶は補填の 1 回だけ、本文つき — memorize=True の組み立て (tags=[]) で書く
    runtime._store_memory.assert_called_once()
    call = runtime._store_memory.call_args
    assert call.args[1] == "こんにちは。"
    assert call.kwargs["tags"] == []
    assert call.kwargs["playbook_name"] == "pb"
    # 言い切った発言なので、中断の通告は書かれず、印も載らない
    persona.history_manager.add_to_building_only.assert_not_called()
    assert INTERRUPTED_METADATA_KEY not in (call.kwargs.get("metadata") or {})


def test_a_beat_already_marked_memorized_is_not_written_again(monkeypatch):
    """「もう記憶に書かれた」の印が立った後の死 — 補填は追加で書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _mark_and_die(node_def_arg, text_arg, state_arg):
        state_arg["_beat_memorized"] = True
        raise RuntimeError("boom after the memory write")

    runtime._process_structured_output.side_effect = _mark_and_die

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


def test_a_settled_interruption_is_not_memorized_twice_by_the_backfill(monkeypatch):
    """停止の後始末 (settle) が記憶を書いた回の死 — 補填が重ねて書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _spell_loop(**kwargs):
        raise RuntimeError("boom after generation")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_spell_loop,
        node_def=_memorize_node_def(),
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # settle が中断の印つきで 1 回書き、補填は印を見て手を出さない
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] == {
        INTERRUPTED_METADATA_KEY: True,
    }


def test_a_death_inside_finalize_beat_still_backfills_the_memory(monkeypatch):
    """`_finalize_beat` は except 節の外で呼ばれる — その内部 (memorize の手前の
    組み立て) で死んだ回も「確定後の隙間」で、補填が要る。呼び出しを包む
    try が受け止めて補填してから、元の例外をそのまま通す。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _die_before_memorize(*args, **kwargs):
        raise RuntimeError("boom inside finalize beat")

    monkeypatch.setattr(runtime_llm, "_finalize_beat", _die_before_memorize)

    with pytest.raises(RuntimeError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_a_death_inside_finalize_beat_after_memorize_is_not_written_again(monkeypatch):
    """`_finalize_beat` が memorize を終えてから死んだ回 — 印が立っているので
    補填は黙り、同じ本文が二重に記憶へ入らない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )

    def _memorize_then_die(runtime_arg, beat):
        beat.state["_beat_memorized"] = True
        raise RuntimeError("boom after the memorize inside finalize beat")

    monkeypatch.setattr(runtime_llm, "_finalize_beat", _memorize_then_die)

    with pytest.raises(RuntimeError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


def test_a_stop_with_spells_disabled_memorizes_the_partial_text_only_once(monkeypatch):
    """ストリーム途中の停止 + スペル無効 — settle が書いた部分文を確定が重ねない。

    スペル無効のペルソナでは `_run_spell_loop` が入り口で即 return するので、
    停止された Beat は例外を出さずに完走し、`_finalize_beat` の memorize に
    到達する (Codex レビュー 2 巡目)。memorize が settle の印 (`_beat_memorized`)
    を見ないと、同じ部分文が同 Beat で二重に記憶へ入る。この 1 本だけは
    `_run_spell_loop` も `_finalize_beat` も実物で通す。
    """
    client = _FakeStreamClient(chunks=["こんにちは。", "続きの文。"])

    real_finalize_beat = runtime_llm._finalize_beat
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
        node_def=_memorize_node_def(),
    )
    # fixture が patch した _finalize_beat を実物へ戻す — 確定の memorize 節が
    # 印を読むところまで含めて本物の経路を固定する。
    monkeypatch.setattr(runtime_llm, "_finalize_beat", real_finalize_beat)
    # MagicMock の戻り値 (truthy) が structured output 扱いにならないように倒す。
    runtime._process_structured_output.return_value = False

    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_cancellation_token": _CancelDuringStream(),
        "_spell_enabled": False,
    }
    # 例外なしで完走する (spell loop はスペル無効の入り口で即 return し、
    # round 頭の取り消し検査に到達しない)
    result = asyncio.run(node(state))
    assert result is state

    # 確定は settle の 1 回だけ (通常確定は pipeline_finalized を見てスキップ)
    runtime._emit_speak_finalize.assert_called_once()
    call = runtime._emit_speak_finalize.call_args
    assert call.args[3] == "こんにちは。"
    assert call.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    # 記憶も settle の 1 回だけ、中断の印つき — ここが二重だった (二重保存の固定)
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.kwargs["metadata"] == {
        INTERRUPTED_METADATA_KEY: True,
    }
    # 通告はユーザーの操作の文面で 1 回
    persona.history_manager.add_to_building_only.assert_called_once()
    assert "ユーザーの操作により" in persona.history_manager.add_to_building_only.call_args.args[1]["content"]


# ---------------------------------------------------------------------------
# 締めの Beat が本文を生まなかった回 — 空の記録を残さない
# (docs/issues/pulse_beats_merge_into_single_record.md の H-1)
#
# スペルループは次の周の生成の直前に新しい下書き行を開ける。その生成が例外で
# 落ちるか空応答だと、締めの本文が無いまま行だけが残る。空文字で確定すると
# 「本文の無い発言」が建物とペルソナのログに永続し、下書きの印も倒れて孤児
# 掃除の網から外れる。
# ---------------------------------------------------------------------------

SPELL_NAME = "note_add"
SPELL_LINE = f"/spell name='{SPELL_NAME}' args={{}}"


class _ScriptedStreamClient:
    """呼び出しごとに別の応答を返す streaming クライアント。

    要素は「その回に流す chunk の列」か、``generate_stream`` の瞬間に投げる
    例外。列の末尾に例外を置くと、chunk を流し終えた後にストリームが死ぬ。
    """

    config_key = None

    def __init__(self, calls):
        self._calls = list(calls)

    def generate_stream(self, messages, tools=(), temperature=None, **kwargs):
        assert self._calls, "_ScriptedStreamClient: no scripted calls left"
        nxt = self._calls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt

        def _gen():
            for item in nxt:
                if isinstance(item, Exception):
                    raise item
                yield item

        return _gen()

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None

    def consume_stream_error(self):
        return None


class _CancelAtCheck:
    """``is_cancelled`` の N 回目から取り消しへ倒れる token。"""

    interrupted_by = "auto"

    def __init__(self, at: int):
        self._at = at
        self._checks = 0

    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        self._checks += 1
        return self._checks >= self._at


async def _ok_spell(tool_name, tool_args, persona, state, playbook_name,
                    event_callback, messages=None):
    return ("やりました", None, True)


def _build_spell_node(monkeypatch, *, calls, node_def=None):
    """実物のスペルループを通す node と、その周辺のフェイク一式。"""
    client = _ScriptedStreamClient(calls)
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=runtime_llm._run_spell_loop,
        node_def=node_def,
    )
    # Beat ごとに別の下書き行が発番される (2 周目は新しい部屋の新しい行)。
    runtime._emit_speak_start.side_effect = ["msg-1", "msg-2", "msg-3"]
    monkeypatch.setattr(runtime_llm, "SPELL_TOOL_NAMES", {SPELL_NAME})
    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _ok_spell)
    return runtime, persona, node, events


def _spell_state(**extra):
    state = {
        "_messages": [],
        "_pulse_id": "pl-1",
        "_spell_enabled": True,
        "_realtime_spells_executed": True,
    }
    state.update(extra)
    return state


def _withdrawal_marks_the_event_log(runtime, events) -> list:
    """取り下げを呼んだ瞬間のイベント数を記録し、取り下げは成功させる。

    取り消し (``streaming_discard``) は Beat 1 の確定でも流れるので、
    「取り下げ由来のものが流れたか」は種類と部屋だけでは区別できない。
    呼び出しの瞬間に線を引いて、その後ろに来たものだけを数える。
    """
    marks: list = []

    def _withdraw(persona, building_id, message_id):
        marks.append(len(events))
        return True

    runtime._withdraw_speak_placeholder.side_effect = _withdraw
    return marks


def _discards_after(events, index: int) -> list:
    return [
        e for e in events[index:]
        if e.get("type") == "streaming_discard"
    ]


def test_a_closing_beat_that_dies_leaves_no_empty_record(monkeypatch):
    """再呼び出しが例外で落ちた回 — 空の下書き行は確定せず、取り下げる。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[[f"やるね。\n{SPELL_LINE}"], RuntimeError("api down")],
    )
    marks = _withdrawal_marks_the_event_log(runtime, events)

    asyncio.run(node(_spell_state()))

    # 確定は Beat 1 の 1 回だけ。空文字での確定は起きない。
    runtime._emit_speak_finalize.assert_called_once()
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    # 締めの Beat のために開けた行は取り下げられる (行を作った部屋に対して)
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"
    # 宙吊りの吹き出しを消す取り消しは、**取り下げの後に** 流れたものであること。
    # Beat 1 の確定も取り消しを流すので、種類だけを数えると取り下げ由来が
    # 一つも流れていなくてもこの検査は通ってしまう。
    assert marks, "取り下げが呼ばれていない"
    after = _discards_after(events, marks[0])
    assert after, "取り下げた行の吹き出しを消す取り消しが流れていない"
    assert after[0]["pulse_id"] == "pl-1"
    assert after[0]["node_id"] == "llm"
    assert after[0]["building_id"] == "b1"
    # 取り下げた行の後に say は流れない (消したはずの発言が画面に残らない)
    assert not [e for e in events[marks[0]:] if e.get("type") == "say"]


def test_a_closing_beat_that_returns_nothing_leaves_no_empty_record(monkeypatch):
    """再呼び出しが空応答だった回も同じ — 空の記録を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    asyncio.run(node(_spell_state()))

    runtime._emit_speak_finalize.assert_called_once()
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"


def test_a_withdrawal_after_a_move_targets_the_room_that_holds_the_row(monkeypatch):
    """移動を挟んだ回の取り下げ先は、その行を作った部屋 (= 移動先)。

    2 周目の下書き行は移動後の部屋に作られる。取り下げが元の部屋を指すと、
    移動先に残った空の行は消えないまま、元の部屋に対して空振りの削除を投げる。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"移るね。\n{SPELL_LINE}"], []],
    )
    # スペルの実行でペルソナが b2 へ移る (現在地は引くたびに最新を返す)
    rooms = ["b1"]
    runtime._effective_building_id.side_effect = (
        lambda _persona, _fallback: rooms[-1]
    )

    async def _moving_spell(tool_name, tool_args, persona_, state, playbook_name,
                            event_callback, messages=None):
        rooms.append("b2")
        return ("移動しました", None, True)

    monkeypatch.setattr(runtime_llm, "_run_spell_tool_async", _moving_spell)
    runtime._withdraw_speak_placeholder.return_value = True

    asyncio.run(node(_spell_state()))

    # Beat 1 の確定先は作成時の部屋 (移動後に引き直さない)
    runtime._emit_speak_finalize.assert_called_once()
    assert runtime._emit_speak_finalize.call_args.args[1] == "b1"
    assert runtime._emit_speak_finalize.call_args.args[2] == "msg-1"
    # Beat 2 の下書き行は移動先の部屋に作られる
    assert runtime._emit_speak_start.call_args.args[1] == "b2"
    # 取り下げも、その行がある移動先の部屋に対して行う
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b2"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"


def test_a_closing_beat_that_dies_midway_keeps_what_it_said(monkeypatch):
    """再呼び出しが途中まで流してから死んだ回 — 部分文を印つきで確定する。

    画面と音声には既に流れた言葉なので、取り下げでは消えてしまう。既存の
    「言い切っていない」印つきの確定 (2026-08-25 裁定の型) で救う。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch,
        calls=[
            [f"やるね。\n{SPELL_LINE}"],
            ["途中まで喋った", RuntimeError("died mid-stream")],
        ],
    )
    runtime._withdraw_speak_placeholder.return_value = True

    asyncio.run(node(_spell_state()))

    # 行は取り下げない — 本文があるので確定側で救う
    runtime._withdraw_speak_placeholder.assert_not_called()
    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    assert second.args[3] == "途中まで喋った"
    assert second.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    # 中断の通告も、その部分文が流れた部屋へ書かれる
    assert persona.history_manager.add_to_building_only.call_args.args[1][
        "content"
    ] == "(ここで発言が中断されました)"


def test_a_refused_withdrawal_still_confirms_the_row(monkeypatch):
    """取り下げが断られた回は確定へ落ちる — 未確定の下書き行を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = False

    asyncio.run(node(_spell_state()))

    assert runtime._emit_speak_finalize.call_count == 2
    assert runtime._emit_speak_finalize.call_args_list[1].args[2] == "msg-2"


def test_a_death_in_the_second_beat_salvages_from_the_spell_side_progress(monkeypatch):
    """2 周目以降の下書き行の回収元は、スペルループ側の器。

    閉包の `text` は最初の周の本文で、それは**別の行として確定済み**。
    `placeholder_round > 1` の分岐がここを取り違えると、確定済みの本文が
    2 周目の行にも書かれて二重になる。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["二周目の言葉。"]],
    )
    # 取り消しの評価点: 1 = 1 周目の chunk、2 = 周の頭、3 = 2 周目の chunk、
    # 4 = 次の周の頭 (ここで倒れて Beat の出口へ抜ける)。
    state = _spell_state(_cancellation_token=_CancelAtCheck(4))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    # 2 周目の器の本文で確定する (1 周目の本文を持ち込まない)
    assert second.args[3] == "二周目の言葉。"
    assert "やるね。" not in second.args[3]
    assert second.kwargs["extra_metadata"] == {INTERRUPTED_METADATA_KEY: True}
    # 連番も 2 周目の器の値から進める (0 から積み直した行なので)
    assert second.kwargs["final_sub_seq"] == 2


def test_a_beat_that_dies_before_its_first_token_leaves_no_empty_record(monkeypatch):
    """再呼び出しの最初の一語が届く前に停止された回 — 空の記録を残さない。

    2 周目の下書き行は開いたが、その周のストリームが一語も返さないうちに
    ユーザーが止めた。ここで空文字のまま確定すると、本文の無い記録が建物と
    ペルソナのログに永続する (締めの Beat の H-1 と同じ形)。判定と取り下げは
    正常復帰側と同じ関数を通す。
    """
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    marks = _withdrawal_marks_the_event_log(runtime, events)
    # 取り消しの評価点: 1 = 1 周目の chunk、2 = 周の頭、2 周目のストリームは
    # 一語も流さないので chunk の評価は無く、3 = 次の周の頭 (ここで倒れる)。
    state = _spell_state(_cancellation_token=_CancelAtCheck(3))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    # 確定は Beat 1 の 1 回だけ — 空文字での確定は起きない
    assert runtime._emit_speak_finalize.call_count == 1
    assert "やるね。" in runtime._emit_speak_finalize.call_args.args[3]
    # 2 周目の空の行は取り下げられる (行を作った部屋に対して)
    runtime._withdraw_speak_placeholder.assert_called_once()
    assert runtime._withdraw_speak_placeholder.call_args.args[1] == "b1"
    assert runtime._withdraw_speak_placeholder.call_args.args[2] == "msg-2"
    # 本文が無いので、記憶にも中断の通告にも何も書かない
    persona.history_manager.add_to_building_only.assert_not_called()
    # 宙吊りの吹き出しを消す取り消しが、取り下げの後に流れる
    assert marks, "取り下げが呼ばれていない"
    after = _discards_after(events, marks[0])
    assert after, "取り下げた行の吹き出しを消す取り消しが流れていない"
    assert after[0]["building_id"] == "b1"


def test_a_dying_beat_that_said_something_is_still_confirmed(monkeypatch):
    """一語でも流れた回は取り下げない — 画面と音声に出た言葉を消さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["二周目の言葉。"]],
    )
    runtime._withdraw_speak_placeholder.return_value = True
    state = _spell_state(_cancellation_token=_CancelAtCheck(4))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    runtime._withdraw_speak_placeholder.assert_not_called()
    assert runtime._emit_speak_finalize.call_count == 2
    assert runtime._emit_speak_finalize.call_args_list[1].args[3] == "二周目の言葉。"


def test_a_refused_withdrawal_on_beat_death_still_confirms_the_row(monkeypatch):
    """取り下げを断られた回は従来の確定へ落ちる — 未確定の行を残さない。"""
    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], []],
    )
    runtime._withdraw_speak_placeholder.return_value = False
    state = _spell_state(_cancellation_token=_CancelAtCheck(3))

    with pytest.raises(LLMError):
        asyncio.run(node(state))

    assert runtime._emit_speak_finalize.call_count == 2
    second = runtime._emit_speak_finalize.call_args_list[1]
    assert second.args[2] == "msg-2"
    assert second.args[3] == ""


def test_a_salvaged_beat_is_not_confirmed_again_at_the_exit(monkeypatch):
    """中間 Beat の確定が失敗して建物へ退避した回 — 出口が同じ本文を重ねない。

    退避に成功したら器の印 (`finalized`) を立てる。立てないと、退避の直後
    (次の下書き行を開ける前) に Beat が死んだとき、出口の後始末が「まだ確定
    していない行」を見つけて同じ本文でもう一度確定し、記憶にも書く — 建物に
    2 件、記憶に 2 件の同じ発言が残る (三巡目レビュー N-3)。
    """
    from sea.runtime_emitters import SpeakFinalizeResult

    runtime, persona, node, events = _build_spell_node(
        monkeypatch, calls=[[f"やるね。\n{SPELL_LINE}"], ["終わりました。"]],
        node_def=_memorize_node_def(),
    )
    # 中間 Beat の確定が「対象行なし」で返る = 本文は建物へ直接退避される
    runtime._emit_speak_finalize.return_value = SpeakFinalizeResult(
        status="missing", building_msg=None,
    )

    # 退避の直後、次の下書き行を開ける前に Beat を殺す。Beat 境界の直後に通る
    # ツール一覧の取り直しがちょうどこの窓にある。
    def _die_right_after_the_salvage(*args, **kwargs):
        raise ExecutionCancelledException(
            message="stopped right after the salvage", interrupted_by="user",
        )

    monkeypatch.setattr(
        runtime_llm, "refresh_mcp_tools_at_head", _die_right_after_the_salvage,
    )
    runtime._withdraw_speak_placeholder.return_value = True

    with pytest.raises(LLMError):
        asyncio.run(node(_spell_state()))

    # 確定の試みは退避の 1 回だけ (出口は器の印を見て手を出さない)
    assert runtime._emit_speak_finalize.call_count == 1
    assert runtime._emit_speak_finalize.call_args.args[2] == "msg-1"
    # 退避済みの行を取り下げにも行かない
    runtime._withdraw_speak_placeholder.assert_not_called()
    # 建物への退避は 1 件だけ
    assert runtime._emit_say.call_count == 1
    assert "やるね。" in runtime._emit_say.call_args.args[2]
    # 記憶にもこの周の本文は 1 回だけ (出口の後始末が重ねて書かない)
    said_again = [
        c for c in runtime._store_memory.call_args_list
        if isinstance(c.args[1], str) and "やるね。" in c.args[1]
    ]
    assert len(said_again) == 1
    # 中断の通告 (settle の副産物) も出ない
    persona.history_manager.add_to_building_only.assert_not_called()


def test_a_node_without_memorize_gets_no_backfill(monkeypatch):
    """memorize 設定の無いノード — 記憶に書かない設計の発言を補填で書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
    )
    runtime._dump_llm_io.side_effect = RuntimeError("boom before memorize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._store_memory.assert_not_called()


# ---------------------------------------------------------------------------
# 建物へ本文を書く口は下書き行の確定だけではない — `_emit_say_and_capture` の
# 直接書き込み (非ストリーミング / fallback / tool streaming) の後に死んだ回も
# 「建物には本文があるのに記憶が無い」形で、補填の対象 (2026-08-27 Codex 指摘)。
# ---------------------------------------------------------------------------

class _FakeSyncClient:
    """generate だけを持つ最小の LLM クライアント (非ストリーミング経路用)。"""

    config_key = None

    def __init__(self, reply):
        self._reply = reply

    def generate(self, messages, tools=(), temperature=None,
                 response_schema=None, **kwargs):
        return self._reply

    def consume_usage(self):
        return None

    def consume_thought_signature(self):
        return None


def test_a_death_after_a_non_streaming_say_backfills_the_memory(monkeypatch):
    """非ストリーミング (同期) 経路 — say の後の例外死でも記憶が補填される。

    この経路は下書き行を作らない (pipeline_finalized が立たない) ので、
    say 直書きの印 (`beat_said`) が補填の発火条件に要る。"""
    client = _FakeSyncClient("こんにちは。")

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    monkeypatch.setattr(runtime_llm, "_is_llm_streaming_enabled", lambda: False)
    # say (branch: 非ストリーミング) の後、_finalize_beat の手前で通る
    # _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom after the sync say")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 下書き行は無いので、確定は呼ばれない
    runtime._emit_speak_finalize.assert_not_called()
    # 補填が say した本文で 1 回書く
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_a_death_after_the_fallback_say_backfills_the_memory(monkeypatch):
    """下書き行の発番に失敗した回の fallback say — その後の例外死でも補填される。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    runtime._emit_speak_start.return_value = None
    runtime._dump_llm_io.side_effect = RuntimeError("boom after the fallback say")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    # 下書き行を作れなかったので、確定は無い — 本文は fallback の _emit_say で
    # 建物に入り、記憶は補填が埋める
    runtime._emit_speak_finalize.assert_not_called()
    runtime._store_memory.assert_called_once()
    assert runtime._store_memory.call_args.args[1] == "こんにちは。"


def test_an_important_only_backfill_writes_the_same_shape_as_the_dual_write(monkeypatch):
    """important のみのノードの補填 — 通常の dual-write と同一の引数集合で書く。

    `_store_beat_memory` 経由だと pulse_context / paired_action_text / scope /
    line_role / spell_origin_id が付き、同じノードの発言が死に方で違う形になる
    (2026-08-27 Codex 指摘の固定)。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return runtime_llm.SpellLoopResult(
            segments=[], final_continuation=kwargs["text"], loop_count=0,
        )

    node_def = _node_def()
    node_def.memorize = None
    node_def.important = True
    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells, node_def=node_def,
    )
    # 確定 (branch 3) の後、_finalize_beat の手前で通る _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom before the dual-write")

    state = {"_messages": [], "_pulse_id": "pl-1"}
    with pytest.raises(LLMError):
        asyncio.run(node(state))

    runtime._store_memory.assert_called_once()
    call = runtime._store_memory.call_args
    assert call.args[1] == "こんにちは。"
    assert call.kwargs["tags"] == ["conversation"]
    assert call.kwargs["playbook_name"] == "pb"
    # dual-write に無い引数は補填でも渡さない
    for absent in ("paired_action_text", "spell_origin_id", "spell_seq",
                   "scope", "line_role", "pulse_context"):
        assert absent not in call.kwargs, absent
    # 書けた回は「もう記憶に書かれた」の印が立つ
    assert state["_beat_memorized"] is True
