"""Regression: a TOOL node whose tool is not registered must record a failure.

``lg_tool_node`` (the ``tool`` node type, in ``sea/runtime_engine.py``) used to
only log when ``TOOL_REGISTRY`` had no entry for the node's action, and then
called ``maybe_await_tool_result(None, ...)``. That helper returns ``None``
instead of raising, so the node recorded a *successful* result of ``None``:
``state["last"] == "None"``, a ``tool '...' result:\\nNone`` message, a PulseContext
tool entry of ``None`` and a "completed" activity event.

This matters after a tool is deleted from the repository while user-owned
playbooks still point at it — for example a same-named override of the builtin
conversation playbooks, or a DB-only playbook, that still ends with the
``control_body`` node removed together with the Unity Gateway (2026-09-11,
docs/issues/unity_gateway_removal.md). The function-calling path
(``lg_tool_call_node`` in ``sea/runtime_nodes.py``) already treated a missing
tool as an error; the ``tool`` node path now does the same.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sea.runtime import SEARuntime
from tools import TOOL_REGISTRY

_MISSING = "nt_engine_tool_that_is_not_registered"


class _RecordingPulseContext:
    def __init__(self) -> None:
        self.entries: list = []

    def append(self, entry) -> None:
        self.entries.append(entry)


def _make():
    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}))
    persona = SimpleNamespace(persona_id="pid", persona_name="p", persona_log_path=None, manager_ref=None)
    playbook = SimpleNamespace(name="pb", display_name="PB", start_node="n")
    return runtime, persona, playbook


def test_missing_tool_is_recorded_as_tool_error_not_success() -> None:
    assert _MISSING not in TOOL_REGISTRY
    runtime, persona, playbook = _make()
    events: list = []
    pulse_ctx = _RecordingPulseContext()
    node_def = SimpleNamespace(
        id="process_body", action=_MISSING, args_input={"message": "speak_content"},
        output_key="body_result", output_keys=None, important=False,
    )
    node = runtime._lg_tool_node(node_def, persona, playbook, events.append)

    # The node must not raise: the conversation continues past a missing tool.
    state = asyncio.run(node({
        "_messages": [],
        "_pulse_context": pulse_ctx,
        "speak_content": "こんにちは",
    }))

    assert state["last"].startswith("Tool error:"), state["last"]
    assert "not found in registry" in state["last"]
    assert state["body_result"] == state["last"]

    last_message = state["_messages"][-1]["content"]
    assert f"tool '{_MISSING}' error:" in last_message
    assert "None" not in last_message

    assert [entry.content for entry in pulse_ctx.entries] == [state["last"]]

    completed = [e for e in events if e.get("type") == "activity" and e.get("status") == "completed"]
    assert completed == []


def test_missing_tool_fills_output_keys_with_the_error() -> None:
    runtime, persona, playbook = _make()
    node_def = SimpleNamespace(
        id="t", action=_MISSING, args_input=None, output_key=None,
        output_keys=["text", "snippet"], important=False,
    )
    node = runtime._lg_tool_node(node_def, persona, playbook)
    state = asyncio.run(node({"_messages": []}))

    assert state["text"].startswith("Tool error:")
    assert state["snippet"] is None
    assert state["last"] == state["text"]


def test_tool_call_node_missing_tool_answers_the_pending_tool_call() -> None:
    """The function-calling path must complete the protocol for a missing tool.

    It used to return early after writing only ``state["last"]``, so the
    assistant message carrying ``tool_calls`` had no matching ``role="tool"``
    reply when the next LLM node ran, the PulseContext had no entry, and the
    pending tool-call id was never cleared.
    """
    assert _MISSING not in TOOL_REGISTRY
    runtime, persona, playbook = _make()
    pulse_ctx = _RecordingPulseContext()
    assistant_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": _MISSING, "arguments": "{}"}}],
    }
    node_def = SimpleNamespace(id="t", call_source="fc", output_key="call_result")
    node = runtime._lg_tool_call_node(node_def, persona, playbook)
    state = asyncio.run(node({
        "_messages": [assistant_call],
        "_pulse_context": pulse_ctx,
        "_last_tool_call_id": "call-1",
        "tool_name": _MISSING,
        "tool_args": {},
    }))

    assert state["last"].startswith("Tool error ("), state["last"]
    assert "not found in registry" in state["last"]
    assert state["call_result"] == state["last"]

    reply = state["_messages"][-1]
    assert reply["role"] == "tool"
    assert reply["tool_call_id"] == "call-1"
    assert reply["content"] == state["last"]
    assert state["_last_tool_call_id"] is None

    assert [(entry.tool_call_id, entry.content) for entry in pulse_ctx.entries] == [("call-1", state["last"])]
