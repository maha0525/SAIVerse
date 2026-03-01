from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncio

from api.routes import config as config_route
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

    runtime._runtime_engine.lg_tool_node.assert_called_once_with(node_def, persona, playbook, None, auto_mode=False)
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


def test_lg_exec_node_runs_selected_playbook_for_meta_user_manual() -> None:
    runtime, persona, playbook = _make_runtime()
    playbook.name = "meta_user_manual"
    node_def = SimpleNamespace(id="exec", playbook_source="selected_playbook", args_source="selected_args")
    sub_playbook = SimpleNamespace(name="deep_research")
    runtime._load_playbook_for = Mock(return_value=sub_playbook)
    runtime._run_playbook = Mock(return_value=["ok"])
    runtime._effective_building_id = Mock(return_value="b1")
    runtime._append_tool_result_message = Mock()

    node = runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, "b1", False)
    state = asyncio.run(node({"selected_playbook": "deep_research", "selected_args": {"input": "hi"}}))

    assert state["last"] == "ok"
    runtime._load_playbook_for.assert_called_once_with("deep_research", persona, "b1")
    runtime._run_playbook.assert_called_once()
    assert runtime._run_playbook.call_args.args[0] is sub_playbook


def test_lg_exec_node_invalid_selected_playbook_does_not_fallback_to_basic_chat() -> None:
    runtime, persona, playbook = _make_runtime()
    node_def = SimpleNamespace(id="exec", playbook_source="selected_playbook", args_source="selected_args")
    runtime._load_playbook_for = Mock(return_value=None)
    runtime._run_playbook = Mock()
    runtime._effective_building_id = Mock(return_value="b1")

    node = runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, "b1", False)
    state = asyncio.run(node({"selected_playbook": "invalid_playbook", "selected_args": {"input": "hi"}}))

    assert state["_exec_error"] is True
    assert "invalid_playbook" in state["_exec_error_detail"]
    runtime._run_playbook.assert_not_called()


def test_lg_exec_node_meta_user_manual_invalid_selected_playbook_emits_warning_for_user() -> None:
    runtime, persona, playbook = _make_runtime()
    playbook.name = "meta_user_manual"
    node_def = SimpleNamespace(id="exec", playbook_source="selected_playbook", args_source="selected_args")
    runtime._load_playbook_for = Mock(return_value=None)
    runtime._run_playbook = Mock()
    runtime._effective_building_id = Mock(return_value="b1")
    events: list[dict[str, str]] = []

    node = runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, "b1", False, event_callback=events.append)
    state = asyncio.run(node({"selected_playbook": "invalid_tool_id", "selected_args": {"input": "hi"}}))

    assert state["_exec_error"] is True
    assert state["last"] == "指定されたツールID 'invalid_tool_id' は存在しません。"
    assert any(e.get("type") == "warning" for e in events)
    runtime._run_playbook.assert_not_called()


def test_lg_exec_node_last_route_keeps_existing_not_found_behavior() -> None:
    runtime, persona, playbook = _make_runtime()
    playbook.name = "meta_user_manual"
    node_def = SimpleNamespace(id="exec", playbook_source="selected_playbook", args_source="selected_args")
    runtime._load_playbook_for = Mock(return_value=None)
    runtime._run_playbook = Mock()
    runtime._effective_building_id = Mock(return_value="b1")
    events: list[dict[str, str]] = []

    node = runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, "b1", False, event_callback=events.append)
    state = asyncio.run(node({"last": "invalid_by_last", "selected_args": {"input": "hi"}}))

    assert state["_exec_error"] is True
    assert state["last"] == "Sub-playbook not found: invalid_by_last"
    assert not any(e.get("type") == "warning" for e in events)
    runtime._run_playbook.assert_not_called()


def test_lg_exec_node_uses_router_result_when_selected_playbook_unspecified() -> None:
    runtime, persona, playbook = _make_runtime()
    node_def = SimpleNamespace(id="exec", playbook_source="selected_playbook", args_source="selected_args")
    sub_playbook = SimpleNamespace(name="deep_research")
    runtime._load_playbook_for = Mock(return_value=sub_playbook)
    runtime._run_playbook = Mock(return_value=["ok"])
    runtime._effective_building_id = Mock(return_value="b1")
    runtime._append_tool_result_message = Mock()

    node = runtime._runtime_engine.lg_exec_node(node_def, playbook, persona, "b1", False)
    state = asyncio.run(node({"last": "deep_research", "selected_args": {"input": "hi"}}))

    assert state["last"] == "ok"
    runtime._load_playbook_for.assert_called_once_with("deep_research", persona, "b1")


def test_set_playbook_returns_400_for_invalid_selected_playbook(monkeypatch) -> None:
    class _PlaybookModel:
        router_callable = object()
        dev_only = object()

    class _UserSettingsModel:
        USERID = object()

    class _Query:
        def __init__(self, model: object) -> None:
            self.model = model

        def filter(self, *_args: object, **_kwargs: object) -> "_Query":
            return self

        def all(self) -> list[object]:
            if self.model is _PlaybookModel:
                return [SimpleNamespace(name="deep_research")]
            return []

        def first(self) -> object:
            return None

    class _DB:
        def query(self, model: object) -> _Query:
            return _Query(model)

        def add(self, _obj: object) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr("database.session.SessionLocal", lambda: _DB())
    monkeypatch.setattr("database.models.Playbook", _PlaybookModel)
    monkeypatch.setattr("database.models.UserSettings", _UserSettingsModel)

    manager = SimpleNamespace(state=SimpleNamespace(current_playbook=None, playbook_params={}, developer_mode=False))
    req = config_route.PlaybookOverrideRequest(
        playbook="meta_user_manual",
        playbook_params={"selected_playbook": "もう一度試してみて"},
    )

    try:
        config_route.set_playbook(req, manager)
        raise AssertionError("HTTPException was not raised")
    except config_route.HTTPException as exc:
        assert exc.status_code == 400
