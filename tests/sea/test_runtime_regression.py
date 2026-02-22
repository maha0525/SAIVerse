import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from llm_clients.exceptions import LLMError
from sea.cancellation import CancellationToken
from sea.runtime import SEARuntime


def _runtime_and_persona() -> tuple[SEARuntime, SimpleNamespace]:
    manager = SimpleNamespace(building_histories={"b1": []})
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(
        persona_name="p",
        persona_id="pid",
        model="m",
        llm_client=object(),
        history_manager=SimpleNamespace(add_message=Mock()),
        execution_state={},
    )
    return runtime, persona


def test_run_meta_user_returns_list_and_emits_status_callback() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)
    events: list[dict] = []

    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])

    def _compile(*args, **kwargs):
        kwargs["event_callback"]({"type": "status", "node": "exec", "content": "ignored"})
        return ["assistant response"]

    runtime._compile_with_langgraph = Mock(side_effect=_compile)
    runtime._maybe_run_metabolism = Mock()

    result = runtime.run_meta_user(
        persona=persona,
        user_input="hello",
        building_id="b1",
        event_callback=events.append,
    )

    assert result == ["assistant response"]
    assert events == [{"type": "status", "node": "exec", "content": "meta_user/exec / exec", "playbook_chain": "meta_user/exec"}]
    persona.history_manager.add_message.assert_called_once()


def test_run_meta_auto_returns_none() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_auto/think", start_node="think", context_requirements=None)

    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])
    runtime._compile_with_langgraph = Mock(return_value=[])
    runtime._maybe_run_metabolism = Mock()

    result = runtime.run_meta_auto(persona=persona, building_id="b1", occupants=[])

    assert result is None
    assert getattr(persona, "_last_conscious_prompt_time_utc", None) is not None


def test_preview_context_delegates_to_preview_context_impl(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, persona = _runtime_and_persona()

    def _fake_preview(*args, **kwargs):
        return {"ok": True, "kwargs": kwargs}

    monkeypatch.setattr("sea.runtime.preview_context_impl", _fake_preview)

    result = runtime.preview_context(persona, "b1", "hello", playbook_name="meta_user")

    assert result == {"ok": True, "kwargs": {"playbook_name": "meta_user"}}


def test_select_llm_client_raises_llmerror_when_client_unset() -> None:
    runtime, persona = _runtime_and_persona()
    persona.llm_client = None

    with pytest.raises(LLMError) as exc_info:
        runtime._select_llm_client(SimpleNamespace(model_type="normal"), persona)

    assert "LLM client is not initialized" in str(exc_info.value)


def test_run_meta_user_falls_back_when_meta_playbook_unresolved() -> None:
    runtime, persona = _runtime_and_persona()
    selected = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)

    runtime._load_playbook_for = Mock(return_value=None)
    runtime._choose_playbook = Mock(return_value=selected)
    runtime._run_playbook = Mock(return_value=["ok"])
    runtime._maybe_run_metabolism = Mock()

    result = runtime.run_meta_user(persona, "hello", "b1", meta_playbook="not_found")

    assert result == ["ok"]
    runtime._choose_playbook.assert_called_once_with(kind="user", persona=persona, building_id="b1")


def test_run_meta_user_propagates_runtime_identifiers_and_callback_payload() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)
    events: list[dict] = []
    captured: dict = {}
    token = CancellationToken()

    runtime._choose_playbook = Mock(return_value=playbook)

    def _prepare_context(*args, **kwargs):
        captured["prepare_pulse_id"] = kwargs["pulse_id"]
        return []

    def _compile(*args, **kwargs):
        captured["compile_pulse_id"] = args[6]
        captured["parent_state"] = kwargs["parent_state"]
        kwargs["event_callback"]({"type": "status", "node": "exec", "content": "ignored"})
        return []

    runtime._prepare_context = Mock(side_effect=_prepare_context)
    runtime._compile_with_langgraph = Mock(side_effect=_compile)
    runtime._maybe_run_metabolism = Mock()

    runtime.run_meta_user(persona, "hello", "b1", event_callback=events.append, cancellation_token=token)

    assert captured["prepare_pulse_id"] == captured["compile_pulse_id"]
    assert captured["parent_state"]["_playbook_chain"] == "meta_user/exec"
    assert captured["parent_state"]["_cancellation_token"] is token
    assert events == [{"type": "status", "node": "exec", "content": "meta_user/exec / exec", "playbook_chain": "meta_user/exec"}]


def test_run_meta_auto_propagates_runtime_identifiers() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_auto/think", start_node="think", context_requirements=None)
    captured: dict = {}
    token = CancellationToken()

    runtime._choose_playbook = Mock(return_value=playbook)

    def _prepare_context(*args, **kwargs):
        captured["prepare_pulse_id"] = kwargs["pulse_id"]
        return []

    def _compile(*args, **kwargs):
        captured["compile_pulse_id"] = args[6]
        captured["parent_state"] = kwargs["parent_state"]
        return []

    runtime._prepare_context = Mock(side_effect=_prepare_context)
    runtime._compile_with_langgraph = Mock(side_effect=_compile)
    runtime._maybe_run_metabolism = Mock()

    runtime.run_meta_auto(persona, "b1", occupants=[], cancellation_token=token)

    assert captured["prepare_pulse_id"] == captured["compile_pulse_id"]
    assert captured["parent_state"]["_playbook_chain"] == "meta_auto/think"
    assert captured["parent_state"]["_cancellation_token"] is token


def test_run_meta_user_transitions_execution_state_running_to_idle() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)
    statuses: list[str] = []

    runtime._choose_playbook = Mock(return_value=playbook)
    runtime._prepare_context = Mock(return_value=[])

    def _compile(*args, **kwargs):
        statuses.append(persona.execution_state["status"])
        persona.execution_state["playbook"] = None
        persona.execution_state["node"] = None
        persona.execution_state["status"] = "idle"
        return []

    runtime._compile_with_langgraph = Mock(side_effect=_compile)
    runtime._maybe_run_metabolism = Mock()

    runtime.run_meta_user(persona, "hello", "b1")

    assert statuses == ["running"]
    assert persona.execution_state["status"] == "idle"


def test_run_meta_user_logs_and_continues_on_history_record_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, persona = _runtime_and_persona()
    selected = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)
    logger_exception = Mock()

    persona.history_manager.add_message.side_effect = RuntimeError("history failed")
    runtime._choose_playbook = Mock(return_value=selected)
    runtime._run_playbook = Mock(return_value=["ok"])
    runtime._maybe_run_metabolism = Mock()
    monkeypatch.setattr("sea.runtime.LOGGER.exception", logger_exception)

    result = runtime.run_meta_user(persona, "hello", "b1")

    assert result == ["ok"]
    runtime._run_playbook.assert_called_once()
    logger_exception.assert_called_once_with("Failed to record user input to history")


def test_run_playbook_delegates_to_runtime_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="meta_user/exec", start_node="exec", context_requirements=None)
    called: dict[str, object] = {}

    def _fake_runner(*args, **kwargs):
        called["runtime"] = args[0]
        called["playbook"] = args[1]
        called["persona"] = args[2]
        called["building_id"] = args[3]
        called["user_input"] = args[4]
        return ["ok"]

    monkeypatch.setattr("sea.runtime.run_playbook", _fake_runner)

    result = runtime._run_playbook(playbook, persona, "b1", "hello", auto_mode=False)

    assert result == ["ok"]
    assert called == {
        "runtime": runtime,
        "playbook": playbook,
        "persona": persona,
        "building_id": "b1",
        "user_input": "hello",
    }


def test_emit_speak_payload_compatibility() -> None:
    manager = SimpleNamespace(
        building_histories={"b1": []},
        occupants={"b1": ["pid", "npc-2"]},
        user_presence_status="online",
        gateway_handle_ai_replies=Mock(),
        unity_gateway=None,
    )
    runtime = SEARuntime(manager)
    persona = SimpleNamespace(persona_id="pid", history_manager=SimpleNamespace(add_message=Mock()))

    runtime._emit_speak(persona, "b1", "hello", pulse_id="p-1")

    persona.history_manager.add_message.assert_called_once()
    payload = persona.history_manager.add_message.call_args.args[0]
    assert payload["metadata"] == {"tags": ["conversation", "pulse:p-1"], "with": ["npc-2", "user"]}


def test_emit_say_payload_compatibility() -> None:
    manager = SimpleNamespace(
        building_histories={"b1": []},
        occupants={"b1": ["pid", "npc-2"]},
        user_presence_status="away",
        gateway_handle_ai_replies=Mock(),
        unity_gateway=None,
    )
    runtime = SEARuntime(manager)
    history_manager = SimpleNamespace(add_to_building_only=Mock())
    persona = SimpleNamespace(persona_id="pid", history_manager=history_manager)

    runtime._emit_say(persona, "b1", "hello", pulse_id="p-1", metadata={"tags": ["media"], "image": "x.png"})

    history_manager.add_to_building_only.assert_called_once()
    payload = history_manager.add_to_building_only.call_args.args[1]
    assert payload["metadata"] == {"tags": ["pulse:p-1", "media"], "image": "x.png", "with": ["npc-2", "user"]}


def test_lg_tool_call_node_reflects_result_in_state(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="pb", display_name="PB")
    node_def = SimpleNamespace(id="tool", call_source="fc", output_key="tool_result")

    monkeypatch.setattr("tools.TOOL_REGISTRY", {"echo": lambda **kwargs: {"ok": kwargs.get("v")}})

    class _Ctx:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("tools.context.persona_context", lambda *args, **kwargs: _Ctx())

    state = {"fc": {"name": "echo", "args": {"v": "x"}}}
    result = asyncio.run(runtime._lg_tool_call_node(node_def, persona, playbook)(state))

    assert result["last"] == "{'ok': 'x'}"
    assert result["tool_result"] == {"ok": "x"}


def test_lg_stelis_nodes_manage_thread_state() -> None:
    runtime, persona = _runtime_and_persona()
    playbook = SimpleNamespace(name="pb")

    active_calls: list[str] = []
    memory = SimpleNamespace(
        can_start_stelis=lambda max_depth: True,
        get_current_thread=lambda: "parent-thread",
        _thread_id=lambda _: "fallback-thread",
        start_stelis_thread=lambda **kwargs: SimpleNamespace(thread_id="child-thread", depth=1),
        append_persona_message=lambda *args, **kwargs: None,
        set_active_thread=lambda tid: active_calls.append(tid),
        get_stelis_info=lambda _: SimpleNamespace(chronicle_prompt=None),
        end_stelis_thread=lambda **kwargs: True,
    )
    persona.sai_memory = memory
    runtime._generate_stelis_chronicle = Mock(return_value="summary")

    state = asyncio.run(runtime._lg_stelis_start_node(SimpleNamespace(id="start", label="x", stelis_config={}), persona, playbook)({}))
    assert state["stelis_thread_id"] == "child-thread"
    assert state["stelis_parent_thread_id"] == "parent-thread"

    state = asyncio.run(runtime._lg_stelis_end_node(SimpleNamespace(id="end", generate_chronicle=True), persona, playbook)(state))
    assert state["stelis_thread_id"] is None
    assert state["stelis_parent_thread_id"] is None
    assert state["stelis_chronicle"] == "summary"
    assert active_calls == ["child-thread", "parent-thread"]


