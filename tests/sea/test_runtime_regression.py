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
