"""Regression: native tools returning a 4-tuple must not leak their raw repr.

Both live TOOL-node paths — ``lg_tool_node`` (the ``tool`` node type, in
``sea/runtime_engine.py``) and ``lg_tool_call_node`` (the function-calling path,
in ``sea/runtime_nodes.py``) — used to do ``result_str = str(result)``. A native
tool returning ``(content, snippet, file_path, metadata)`` therefore stored the
stringified *whole tuple* into ``state["last"]`` (→ MEMORIZE / SAIMemory), the
tool-result message, and PulseContext, instead of just its text.

Fix: normalize via ``tools.core.parse_tool_result`` (the same canonical parser
the /spell and chat paths already use). See
docs/issues/archive/native_tool_return_4tuple_bug.md.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sea.runtime import SEARuntime
from tools import register_external_tool, unregister_external_tool
from tools.core import ToolSchema

_TUPLE_RESULT = ("VISIBLE TEXT", "history snippet", "/tmp/see.png", {"media": [{"path": "/tmp/see.png"}]})


def _register_tuple_tool(name: str):
    schema = ToolSchema(
        name=name, description="d",
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string", spell=False,
    )
    register_external_tool(name, schema, lambda **kw: _TUPLE_RESULT, allow_replace=True)


def _make():
    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}))
    persona = SimpleNamespace(persona_id="pid", persona_name="p", persona_log_path=None, manager_ref=None)
    playbook = SimpleNamespace(name="pb", display_name="PB", start_node="n")
    return runtime, persona, playbook


def _assert_no_raw_tuple_leak(text: str) -> None:
    assert "VISIBLE TEXT" in text, f"content text missing: {text!r}"
    # The raw tuple's other members must NOT appear (they would if str(tuple) leaked).
    assert "history snippet" not in text, f"raw tuple leaked: {text!r}"
    assert "/tmp/see.png" not in text, f"raw tuple leaked: {text!r}"


def test_lg_tool_node_tuple_result_is_normalized() -> None:
    runtime, persona, playbook = _make()
    _register_tuple_tool("nt_engine_tuple")
    try:
        node_def = SimpleNamespace(
            id="t", action="nt_engine_tuple", args_input=None,
            output_key=None, output_keys=None, important=False,
        )
        node = runtime._lg_tool_node(node_def, persona, playbook)
        state = asyncio.run(node({"_messages": []}))

        assert state["last"] == "VISIBLE TEXT"
        _assert_no_raw_tuple_leak(state["_messages"][-1]["content"])
    finally:
        unregister_external_tool("nt_engine_tuple")


def test_lg_tool_node_output_keys_still_expand_raw_tuple() -> None:
    """output_keys is the explicit multi-value contract and must keep the raw
    element values, while state["last"] stays the normalized text."""
    runtime, persona, playbook = _make()
    _register_tuple_tool("nt_engine_okeys")
    try:
        node_def = SimpleNamespace(
            id="t", action="nt_engine_okeys", args_input=None, output_key=None,
            output_keys=["text", "snippet", "file_path", "metadata"], important=False,
        )
        node = runtime._lg_tool_node(node_def, persona, playbook)
        state = asyncio.run(node({"_messages": []}))

        assert state["text"] == "VISIBLE TEXT"
        assert state["snippet"] == "history snippet"
        assert state["file_path"] == "/tmp/see.png"
        assert state["metadata"] == {"media": [{"path": "/tmp/see.png"}]}
        assert state["last"] == "VISIBLE TEXT"
    finally:
        unregister_external_tool("nt_engine_okeys")


def test_lg_tool_call_node_tuple_result_is_normalized() -> None:
    runtime, persona, playbook = _make()
    _register_tuple_tool("nt_fc_tuple")
    try:
        node_def = SimpleNamespace(id="t", call_source="fc", output_key=None)
        node = runtime._lg_tool_call_node(node_def, persona, playbook)
        state = asyncio.run(node({
            "_messages": [],
            "tool_name": "nt_fc_tuple",
            "tool_args": {},
        }))

        assert state["last"] == "VISIBLE TEXT"
    finally:
        unregister_external_tool("nt_fc_tuple")


if __name__ == "__main__":
    for fn in (
        test_lg_tool_node_tuple_result_is_normalized,
        test_lg_tool_node_output_keys_still_expand_raw_tuple,
        test_lg_tool_call_node_tuple_result_is_normalized,
    ):
        fn()
    print("ok")
