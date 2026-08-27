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


def test_a_stream_call_that_dies_still_confirms_the_draft_row(monkeypatch):
    """LLM 呼び出しが即死した回 — 本文ゼロでも下書き行は空文字で確定する。"""
    client = _FakeStreamClient(call_exc=RuntimeError("api down"))

    async def _unused_spell_loop(**kwargs):  # pragma: no cover - 到達しない
        raise AssertionError("spell loop must not run")

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_unused_spell_loop,
    )
    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

    runtime._emit_speak_finalize.assert_called_once()
    args = runtime._emit_speak_finalize.call_args.args
    assert args[2] == "msg-1"
    assert args[3] == ""
    # 本文が無いので、記憶にも建物の通告にも何も書かない
    runtime._store_memory.assert_not_called()
    persona.history_manager.add_to_building_only.assert_not_called()


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
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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


def test_a_death_after_finalize_but_before_memorize_backfills_the_memory(monkeypatch):
    """確定後・memorize 前の例外死 — 本人の記憶が本文つきで補填される。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return kwargs["text"], kwargs["text"], 0

    runtime, persona, node, events = _build_node(
        monkeypatch, client=client, spell_loop=_no_spells,
        node_def=_memorize_node_def(),
    )
    # 確定 (branch 3) の後、_finalize_beat の手前で通る _dump_llm_io で落とす
    runtime._dump_llm_io.side_effect = RuntimeError("boom before memorize")

    with pytest.raises(LLMError):
        asyncio.run(node({"_messages": [], "_pulse_id": "pl-1"}))

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
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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


def test_a_node_without_memorize_gets_no_backfill(monkeypatch):
    """memorize 設定の無いノード — 記憶に書かない設計の発言を補填で書かない。"""
    client = _FakeStreamClient(chunks=["こんにちは。"])

    async def _no_spells(**kwargs):
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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
        return kwargs["text"], kwargs["text"], 0

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
