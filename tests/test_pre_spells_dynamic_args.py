"""Test for pre_spells dynamic-args path.

Verifies that ``/spell name='X'`` (no-args form) entries are routed through
``_decide_spell_args_via_playbook`` (= spell_args_decider Playbook) instead of
being skipped, and that the rest of pre_spells processing still works for
fully-specified entries.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sea.runtime_llm import _execute_pre_spells, _SPELL_PATTERN_NO_ARGS


def test_no_args_pattern_matches_simple_form() -> None:
    m = _SPELL_PATTERN_NO_ARGS.match("/spell name='send_email_to_user'")
    assert m is not None
    assert m.group(1) == "send_email_to_user"


def test_no_args_pattern_does_not_match_args_form() -> None:
    """args= が付いている形式は no_args パターンでは捕まえない。"""
    m = _SPELL_PATTERN_NO_ARGS.match("/spell name='X' args={}")
    assert m is None


def test_no_args_pattern_does_not_match_garbage() -> None:
    assert _SPELL_PATTERN_NO_ARGS.match("not a spell") is None
    assert _SPELL_PATTERN_NO_ARGS.match("/spell ") is None


def _make_state(messages: list | None = None) -> dict:
    return {
        "_messages": messages if messages is not None else [],
        "_pulse_id": "test-pulse-id",
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_no_args_entry_invokes_decider() -> None:
    """`/spell name='X'` (no args) → _decide_spell_args_via_playbook が呼ばれる。"""
    decider_mock = AsyncMock(return_value={"to": "mahomu", "subject": "test"})

    runtime = SimpleNamespace(
        _store_memory=lambda *args, **kwargs: None,
    )
    persona = SimpleNamespace(persona_id="pid")
    state = _make_state()
    playbook = SimpleNamespace(name="track_user_conversation")

    spell_run_mock = AsyncMock(return_value=("ok", None))

    with patch("sea.runtime_llm._decide_spell_args_via_playbook", decider_mock), \
         patch("sea.runtime_llm._run_spell_tool_async", spell_run_mock), \
         patch("sea.runtime_llm.SPELL_TOOL_NAMES", {"send_email_to_user"}):
        asyncio.run(_execute_pre_spells(
            ["/spell name='send_email_to_user'"],
            runtime, persona, "b1", state, playbook, None,
        ))

    # decider was called with the spell name
    decider_mock.assert_awaited_once()
    assert decider_mock.await_args.args[0] == "send_email_to_user"

    # the actual spell was then run with the decider's args
    spell_run_mock.assert_awaited_once()
    call_args = spell_run_mock.await_args.args
    assert call_args[0] == "send_email_to_user"
    assert call_args[1] == {"to": "mahomu", "subject": "test"}


def test_decider_returning_none_skips_spell() -> None:
    """decider が None を返した場合、その Spell は skip され例外にならない。"""
    decider_mock = AsyncMock(return_value=None)
    spell_run_mock = AsyncMock(return_value=("ok", None))

    runtime = SimpleNamespace(_store_memory=lambda *args, **kwargs: None)
    persona = SimpleNamespace(persona_id="pid")
    state = _make_state()
    playbook = SimpleNamespace(name="track_user_conversation")

    with patch("sea.runtime_llm._decide_spell_args_via_playbook", decider_mock), \
         patch("sea.runtime_llm._run_spell_tool_async", spell_run_mock), \
         patch("sea.runtime_llm.SPELL_TOOL_NAMES", {"send_email_to_user"}):
        asyncio.run(_execute_pre_spells(
            ["/spell name='send_email_to_user'"],
            runtime, persona, "b1", state, playbook, None,
        ))

    # decider was called but spell was NOT run
    decider_mock.assert_awaited_once()
    spell_run_mock.assert_not_called()


def test_fully_specified_entry_does_not_invoke_decider() -> None:
    """args 指定済みエントリは decider を呼ばずに直接実行される。"""
    decider_mock = AsyncMock()
    spell_run_mock = AsyncMock(return_value=("ok", None))

    runtime = SimpleNamespace(_store_memory=lambda *args, **kwargs: None)
    persona = SimpleNamespace(persona_id="pid")
    state = _make_state()
    playbook = SimpleNamespace(name="track_user_conversation")

    with patch("sea.runtime_llm._decide_spell_args_via_playbook", decider_mock), \
         patch("sea.runtime_llm._run_spell_tool_async", spell_run_mock), \
         patch("sea.runtime_llm.SPELL_TOOL_NAMES", {"my_spell"}):
        asyncio.run(_execute_pre_spells(
            ["/spell name='my_spell' args={'k': 'v'}"],
            runtime, persona, "b1", state, playbook, None,
        ))

    decider_mock.assert_not_called()
    spell_run_mock.assert_awaited_once()
    assert spell_run_mock.await_args.args[1] == {"k": "v"}


def test_unknown_spell_in_no_args_form_is_skipped() -> None:
    """no_args 形式でも未登録 Spell は skip 警告のみ。"""
    decider_mock = AsyncMock()
    spell_run_mock = AsyncMock()

    runtime = SimpleNamespace(_store_memory=lambda *args, **kwargs: None)
    persona = SimpleNamespace(persona_id="pid")
    state = _make_state()
    playbook = SimpleNamespace(name="track_user_conversation")

    with patch("sea.runtime_llm._decide_spell_args_via_playbook", decider_mock), \
         patch("sea.runtime_llm._run_spell_tool_async", spell_run_mock), \
         patch("sea.runtime_llm.SPELL_TOOL_NAMES", {"known_spell"}):
        asyncio.run(_execute_pre_spells(
            ["/spell name='unknown_spell'"],
            runtime, persona, "b1", state, playbook, None,
        ))

    decider_mock.assert_not_called()
    spell_run_mock.assert_not_called()


def test_mixed_entries_handled_independently() -> None:
    """確定値 + 引数なし + 不明 が混在しても、有効なものは全部実行される。"""
    decider_mock = AsyncMock(return_value={"k": "decided"})
    spell_run_mock = AsyncMock(return_value=("ok", None))

    runtime = SimpleNamespace(_store_memory=lambda *args, **kwargs: None)
    persona = SimpleNamespace(persona_id="pid")
    state = _make_state()
    playbook = SimpleNamespace(name="track_user_conversation")

    with patch("sea.runtime_llm._decide_spell_args_via_playbook", decider_mock), \
         patch("sea.runtime_llm._run_spell_tool_async", spell_run_mock), \
         patch("sea.runtime_llm.SPELL_TOOL_NAMES", {"spell_a", "spell_b"}):
        asyncio.run(_execute_pre_spells(
            [
                "/spell name='spell_a' args={'fixed': 1}",
                "/spell name='spell_b'",
                "/spell name='unknown_spell'",
            ],
            runtime, persona, "b1", state, playbook, None,
        ))

    # decider は spell_b 用に 1 回呼ばれる
    decider_mock.assert_awaited_once()
    assert decider_mock.await_args.args[0] == "spell_b"

    # spell_a と spell_b の 2 件が実行される
    assert spell_run_mock.await_count == 2
