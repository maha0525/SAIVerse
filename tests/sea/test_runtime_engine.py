from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

from sea.runtime import SEARuntime


def _make_runtime() -> tuple[SEARuntime, SimpleNamespace, SimpleNamespace]:
    runtime = SEARuntime(SimpleNamespace(building_histories={"b1": []}))
    persona = SimpleNamespace(persona_id="pid", persona_name="p")
    playbook = SimpleNamespace(name="pb", display_name="PB")
    return runtime, persona, playbook


def test_lg_tool_node_delegates_to_engine() -> None:
    runtime, persona, playbook = _make_runtime()
    node_def = SimpleNamespace(id="tool1")
    expected = {"last": "ok"}
    runtime._runtime_engine.lg_tool_node = Mock(return_value=AsyncMock(return_value=expected))

    node = runtime._lg_tool_node(node_def, persona, playbook)
    result = asyncio.run(node({}))

    runtime._runtime_engine.lg_tool_node.assert_called_once_with(node_def, persona, playbook, None)
    assert result == expected


def test_lg_exec_node_delegates_to_engine() -> None:
    runtime, persona, playbook = _make_runtime()
    node_def = SimpleNamespace(id="exec1")
    outputs: list[str] = []
    runtime._runtime_engine.lg_exec_node = Mock(return_value=AsyncMock(return_value={"last": "done"}))

    node = runtime._lg_exec_node(node_def, playbook, persona, "b1", False, outputs)
    state = asyncio.run(node({}))

    assert state["last"] == "done"
    runtime._runtime_engine.lg_exec_node.assert_called_once_with(node_def, playbook, persona, "b1", False, outputs, None)


def test_lg_memorize_node_delegates_to_engine() -> None:
    runtime, persona, playbook = _make_runtime()
    node_def = SimpleNamespace(id="memo1")
    outputs: list[str] = []
    runtime._runtime_engine.lg_memorize_node = Mock(return_value=AsyncMock(return_value={"last": "memo"}))

    node = runtime._lg_memorize_node(node_def, persona, playbook, outputs)
    state = asyncio.run(node({}))

    assert state["last"] == "memo"
    runtime._runtime_engine.lg_memorize_node.assert_called_once_with(node_def, persona, playbook, outputs, None)


def test_lg_speak_node_delegates_to_engine() -> None:
    runtime, persona, playbook = _make_runtime()
    outputs: list[str] = []
    runtime._runtime_engine.lg_speak_node = Mock(return_value={"last": "spoken"})

    state = runtime._lg_speak_node({"last": "x"}, persona, "b1", playbook, outputs)

    assert state["last"] == "spoken"
    runtime._runtime_engine.lg_speak_node.assert_called_once_with({"last": "x"}, persona, "b1", playbook, outputs, None)
