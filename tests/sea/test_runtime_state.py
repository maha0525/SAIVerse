from types import SimpleNamespace

from sea.runtime import SEARuntime
from sea.runtime_state import (
    apply_output_mapping,
    eval_arithmetic_expression,
    extract_structured_json,
    process_structured_output,
    resolve_set_value,
)


def test_extract_structured_json_and_runtime_delegate() -> None:
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    text = '```json\n{"playbook": "basic_chat"}\n```'

    expected = extract_structured_json(text)

    assert expected == {"playbook": "basic_chat"}
    assert runtime._extract_structured_json(text) == expected


def test_process_structured_output_and_runtime_delegate() -> None:
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    node_def = SimpleNamespace(id="router", response_schema={"type": "object"}, output_mapping={"playbook": "selected_playbook"})
    state_a: dict[str, object] = {}
    state_b: dict[str, object] = {}
    text = '{"playbook":"meta_user/exec","args":{"input":"hi"}}'

    assert process_structured_output(node_def, text, state_a) is True
    assert runtime._process_structured_output(node_def, text, state_b) is True
    assert state_b == state_a


def test_apply_output_mapping_array_path() -> None:
    state: dict[str, object] = {"router": {"items": [{"name": "a"}, {"name": "b"}]}}

    apply_output_mapping(state, "router", {"items.1.name": "selected"})

    assert state["selected"] == "b"


def test_resolve_set_value_and_eval_delegate() -> None:
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    state = {"count": "2", "name": "alice"}

    assert eval_arithmetic_expression("{count} + 3", state) == 5
    assert runtime._eval_arithmetic_expression("{count} + 3", state) == 5
    assert resolve_set_value("Hello {name}", state) == "Hello alice"
    assert runtime._resolve_set_value("={count} + 3", state) == 5
