import asyncio
from types import SimpleNamespace

import pytest

from llm_clients.exceptions import LLMError
from sea import runtime, runtime_utils


def test_runtime_exposes_shared_format_helper() -> None:
    assert runtime._format is runtime_utils._format


def test_runtime_exposes_shared_streaming_helper() -> None:
    assert runtime._is_llm_streaming_enabled is runtime_utils._is_llm_streaming_enabled


def test_subplay_node_uses_runtime_utils_even_if_runtime_format_missing(monkeypatch) -> None:
    monkeypatch.delattr(runtime, "_format", raising=False)

    runtime_obj = runtime.SEARuntime(manager_ref=object())
    runtime_obj._load_playbook_for = lambda *args, **kwargs: object()
    runtime_obj._effective_building_id = lambda *args, **kwargs: "b1"
    runtime_obj._run_playbook = lambda *args, **kwargs: ["ok"]

    node_def = SimpleNamespace(id="sub", playbook="pb", input_template="hello {input}", execution="inline")
    playbook = SimpleNamespace(name="meta")
    node = runtime_obj._lg_subplay_node(node_def, SimpleNamespace(), "b1", playbook, auto_mode=False)

    state = {"inputs": {"input": "world"}, "last": "", "messages": []}
    result = asyncio.run(node(state))
    assert result["last"] == "ok"


def test_runtime_llm_node_uses_runtime_utils_even_if_module_format_missing(monkeypatch) -> None:
    import sea.runtime_llm as runtime_llm

    monkeypatch.delattr(runtime_llm, "_format", raising=False)

    class StubRuntime:
        def _prepare_context(self, *args, **kwargs):
            return []

        def _add_playbook_enum(self, schema, available):
            return schema

        def _select_llm_client(self, *args, **kwargs):
            raise RuntimeError("stop_after_format")

    runtime_obj = StubRuntime()
    node_def = SimpleNamespace(id="n1", action="hello {input}", context_profile=None, response_schema=None)
    playbook = SimpleNamespace(name="pb")
    persona = SimpleNamespace(persona_id="p1", persona_name="pn")

    node = runtime_llm.lg_llm_node(runtime_obj, node_def, persona, "b1", playbook)
    state = {"inputs": {"input": "world"}, "last": "", "messages": []}

    with pytest.raises(LLMError):
        asyncio.run(node(state))
