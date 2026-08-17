"""W10 (柱8) Playbook 契約の回帰テスト — `_` 予約 namespace 保護と入力契約の実行時検証。

Spell/Playbook 監査 (2026-07-15) の残 finding 2 点を固定する:

1. `_` 予約 namespace (P1): Playbook が宣言する名前 (input param / output_schema /
   node id / output_key / output_keys / output_mapping / SET assignments) から
   runtime のシステム変数 (_messages, _pulse_context, ...) へ書き込める穴。
   ロード時 (PlaybookSchema validator) で fail-closed に弾き、値が効く merge 点
   (inherited_vars / output_schema 書き戻し / SET) でも防御する。
2. 入力契約 (P2): required 欠落・型違い・enum 外値が暗黙値として実行される。
   提供された値は宣言型へ正規化し、変換不能と enum 外は正直に失敗させる。
   required 欠落は既存 52/94 パラメータが依存するため warn-only (W10 裁定)。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

import tools
from llm_clients.exceptions import LLMError
from sea.playbook_models import InputParam, PlaybookSchema
from sea.runtime import SEARuntime
from sea.runtime_engine import RuntimeEngine
from sea.runtime_graph import _validate_input_param, compile_with_langgraph
from sea.runtime_nodes import lg_tool_call_node
from sea.runtime_state import (
    apply_output_mapping,
    resolve_state_value,
    set_playbook_var,
    store_structured_result,
)


# ---------------------------------------------------------------------------
# 1. PlaybookSchema validator: `_` prefix の書き込みターゲットを fail-closed で拒否
# ---------------------------------------------------------------------------

def _pb_dict(**overrides) -> dict:
    base = {
        "name": "t_pb",
        "description": "test",
        "input_schema": [],
        "nodes": [{"id": "n0", "type": "pass", "next": None}],
        "start_node": "n0",
    }
    base.update(overrides)
    return base


def test_playbook_without_reserved_names_loads():
    pb = PlaybookSchema(**_pb_dict())
    assert pb.name == "t_pb"


@pytest.mark.parametrize(
    "overrides",
    [
        # input param 名
        {"input_schema": [{"name": "_messages", "description": "x"}]},
        # output_schema キー (親 state へ書き戻される)
        {"output_schema": ["_pulse_context"]},
        # node id (output_key 未指定時の既定ターゲット)
        {"nodes": [{"id": "_evil", "type": "pass", "next": None}], "start_node": "_evil"},
        # LLM output_key
        {"nodes": [{"id": "n0", "type": "llm", "output_key": "_spell_enabled"}]},
        # LLM output_mapping のターゲット
        {"nodes": [{"id": "n0", "type": "llm", "output_mapping": {"a.b": "_messages"}}]},
        # LLM output_keys のターゲット (dict 形式)
        {"nodes": [{"id": "n0", "type": "llm", "output_keys": [{"text": "_messages"}]}]},
        # TOOL output_keys (str 要素形式)
        {"nodes": [{"id": "n0", "type": "tool", "action": "t", "output_keys": ["_messages"]}]},
        # SET assignments キー
        {"nodes": [{"id": "n0", "type": "set", "assignments": {"_cancellation_token": None}}]},
    ],
)
def test_playbook_reserved_name_rejected_at_load(overrides):
    with pytest.raises(ValidationError, match="reserved '_' state namespace"):
        PlaybookSchema(**_pb_dict(**overrides))


# ---------------------------------------------------------------------------
# 2. 実行時の防御 (validator を通らない stale な schema オブジェクトへの二重化)
# ---------------------------------------------------------------------------

def _capture_initial_state(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_compile(*args, **kwargs):
        async def _fake_graph(initial_state, config):
            captured.update(initial_state)
            initial_state["ok_key"] = "from-child"
            return initial_state
        return _fake_graph

    monkeypatch.setattr("sea.runtime_graph.compile_playbook", _fake_compile)
    return captured


def _ns_playbook(**overrides) -> SimpleNamespace:
    base = dict(
        name="pb", start_node="n0", context_requirements=None,
        input_schema=[], output_schema=None, report_template=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_runtime_skips_reserved_input_param(monkeypatch):
    """`_` prefix の input param は state へ merge されない (system 変数が守られる)。"""
    captured = _capture_initial_state(monkeypatch)
    evil = InputParam(name="_pulse_id", description="x", required=False)
    playbook = _ns_playbook(input_schema=[evil])

    compile_with_langgraph(
        MagicMock(), playbook, SimpleNamespace(execution_state={}), "b1",
        None, False, base_messages=[], pulse_id="pulse-real",
        parent_state={"_args": {"_pulse_id": "hijacked"}},
    )

    assert captured["_pulse_id"] == "pulse-real"


def test_runtime_skips_reserved_output_schema_writeback(monkeypatch):
    """output_schema に `_` キーがあっても親 state の system 変数を上書きしない。"""
    _capture_initial_state(monkeypatch)
    playbook = _ns_playbook(output_schema=["_messages", "ok_key"])
    parent_messages = [{"role": "user", "content": "parent"}]
    parent_state = {"_messages": parent_messages}

    compile_with_langgraph(
        MagicMock(), playbook, SimpleNamespace(execution_state={}), "b1",
        None, False, base_messages=[], pulse_id="p1",
        parent_state=parent_state,
    )

    assert parent_state["_messages"] is parent_messages  # 上書きされていない
    assert parent_state["ok_key"] == "from-child"        # 通常キーは通る


def test_reserved_write_is_refused_at_the_single_door():
    """全ての書き込み口が通る一箇所 (set_playbook_var) が `_` を拒否する。"""
    state = {"_messages": ["original"]}

    assert set_playbook_var(state, "_messages", "boom", where="test") is False
    assert set_playbook_var(state, "ok", "yes", where="test") is True
    assert state["_messages"] == ["original"]
    assert state["ok"] == "yes"


def test_output_mapping_skips_reserved_target():
    """LLM ノードの output_mapping ターゲットからシステム変数を書けない。"""
    state = {"_messages": ["original"], "router": {"a": "x", "b": "y"}}
    apply_output_mapping(state, "router", {"router.a": "_messages", "router.b": "good"})

    assert state["_messages"] == ["original"]
    assert state["good"] == "y"


def test_store_structured_result_skips_reserved_key():
    """structured output の output_key が `_` なら派生キーごと書かれない。"""
    state = {"_pulse_context": "original"}
    store_structured_result(state, "_pulse_context", {"a": 1})

    assert state["_pulse_context"] == "original"
    assert "_pulse_context.a" not in state


def _fake_tool_playbook() -> SimpleNamespace:
    return SimpleNamespace(name="pb", display_name=None)


def _tool_persona() -> SimpleNamespace:
    return SimpleNamespace(persona_id="pid", persona_name="p", persona_log_path=None, manager_ref=None)


def test_tool_node_skips_reserved_output_key(monkeypatch):
    """TOOL ノードの output_key / output_keys からシステム変数を書けない。"""
    monkeypatch.setitem(tools.TOOL_REGISTRY, "w10_contract_tool", lambda: "result")

    engine = RuntimeEngine(MagicMock(), SimpleNamespace(city_id=1), Mock(), {})
    node_def = SimpleNamespace(
        id="t1", action="w10_contract_tool", args_input=None,
        output_key="_spell_enabled", output_keys=None, important=False,
    )
    node = engine.lg_tool_node(node_def, _tool_persona(), _fake_tool_playbook())

    state: dict = {"_spell_enabled": True, "_messages": []}
    asyncio.run(node(state))

    assert state["_spell_enabled"] is True   # 上書きされていない
    assert state["last"] == "result"         # 通常経路は動いている


def test_tool_node_reserved_output_keys_do_not_shift_positions(monkeypatch):
    """output_keys の一部が `_` でも、残りの位置は元のまま格納される。"""
    monkeypatch.setitem(tools.TOOL_REGISTRY, "w10_tuple_tool", lambda: ("a", "b", "c"))

    engine = RuntimeEngine(MagicMock(), SimpleNamespace(city_id=1), Mock(), {})
    node_def = SimpleNamespace(
        id="t2", action="w10_tuple_tool", args_input=None,
        output_key=None, output_keys=["first", "_messages", "third"], important=False,
    )
    node = engine.lg_tool_node(node_def, _tool_persona(), _fake_tool_playbook())

    state: dict = {"_messages": []}
    asyncio.run(node(state))

    assert state["first"] == "a"
    assert state["_messages"] != "b"   # システム変数は守られる
    assert state["third"] == "c"       # 位置はずれない


def test_tool_call_node_skips_reserved_output_key(monkeypatch):
    """TOOL_CALL ノードの output_key からシステム変数を書けない。"""
    monkeypatch.setitem(tools.TOOL_REGISTRY, "w10_contract_tool", lambda: "result")

    runtime = MagicMock()
    runtime._resolve_state_value = lambda state, path: resolve_state_value(state, path)
    runtime._append_tool_result_message = Mock()
    node = lg_tool_call_node(
        runtime,
        SimpleNamespace(id="tc1", call_source="fc", output_key="_cancellation_token"),
        _tool_persona(),
        _fake_tool_playbook(),
    )

    state: dict = {
        "fc": {"name": "w10_contract_tool", "args": {}},
        "_cancellation_token": None,
    }
    asyncio.run(node(state))

    assert state["_cancellation_token"] is None
    assert state["last"] == "result"


def test_set_node_skips_reserved_assignment():
    runtime = SEARuntime(SimpleNamespace(building_histories={}))
    node_def = SimpleNamespace(id="s1", assignments={"_messages": "boom", "good": "ok"})
    playbook = SimpleNamespace(name="pb")
    node = runtime._lg_set_node(node_def, playbook)

    state: dict = {"_messages": ["original"]}
    asyncio.run(node(state))

    assert state["_messages"] == ["original"]
    assert state["good"] == "ok"


# ---------------------------------------------------------------------------
# 3. 入力契約: 型の正規化と正直な失敗
# ---------------------------------------------------------------------------

def _param(**kwargs) -> InputParam:
    base = dict(name="p", description="x")
    base.update(kwargs)
    return InputParam(**base)


def test_number_param_coerces_quoted_values():
    p = _param(param_type="number")
    assert _validate_input_param("pb", p, "42") == 42
    assert _validate_input_param("pb", p, "4.5") == 4.5
    assert _validate_input_param("pb", p, 7) == 7


def test_number_param_rejects_garbage():
    p = _param(param_type="number")
    with pytest.raises(LLMError):
        _validate_input_param("pb", p, "abc")
    with pytest.raises(LLMError):
        _validate_input_param("pb", p, True)


def test_boolean_param_coerces_and_rejects():
    p = _param(param_type="boolean")
    assert _validate_input_param("pb", p, "true") is True
    assert _validate_input_param("pb", p, "0") is False
    assert _validate_input_param("pb", p, False) is False
    with pytest.raises(LLMError):
        _validate_input_param("pb", p, "banana")


def test_enum_param_membership():
    p = _param(param_type="enum", enum_values=["even", "free"])
    assert _validate_input_param("pb", p, "even") == "even"
    with pytest.raises(LLMError):
        _validate_input_param("pb", p, "chaos")


def test_empty_static_enum_is_rejected_at_load():
    """空の静的 enum は「何も許さない宣言」。制約なしとして素通ししない (F5)。"""
    with pytest.raises(ValidationError, match="enum"):
        InputParam(name="mode", description="x", param_type="enum", enum_values=[])


def test_enum_without_any_value_source_is_rejected_at_load():
    """静的リストも動的 source も無い enum は宣言として成立しない (F5)。"""
    with pytest.raises(ValidationError, match="enum"):
        InputParam(name="mode", description="x", param_type="enum")


@pytest.mark.parametrize("enum_values", [[], ["even", "free"]])
def test_enum_values_and_enum_source_cannot_both_be_declared(enum_values):
    """選択肢の供給源は一つだけ。

    UI へ選択肢を出す API は enum_source を優先し、実行時の検証は静的集合を
    見る。両立を許すと「UI で選べた値が実行時に弾かれる」宣言が書けてしまう
    ので、規則で優先順位を捌かず、書ける口をなくす。
    """
    with pytest.raises(ValidationError, match="mutually exclusive"):
        InputParam(
            name="mode", description="x", param_type="enum",
            enum_values=enum_values, enum_source="playbooks:router_callable",
        )


def test_dynamic_enum_wins_over_static_list_for_stale_declarations():
    """validator を経ていない宣言で両方あるときは動的側に倒す (API と同じ順位)。

    静的集合で弾くと、UI が enum_source から出した選択肢が実行時に拒否される。
    """
    stale = SimpleNamespace(
        name="mode", param_type="enum",
        enum_values=["even"], enum_source="playbooks:router_callable",
    )
    assert _validate_input_param("pb", stale, "anything") == "anything"


def test_empty_static_enum_rejects_every_value_at_runtime():
    """ロード時検証を経ていない stale な宣言でも、空 enum は fail-closed (F5)。

    truthiness 判定 (``if enum_values``) だと空リストが「制約なし」に化けて
    どんな値も通っていた。
    """
    stale = SimpleNamespace(name="mode", param_type="enum", enum_values=[], enum_source=None)
    with pytest.raises(LLMError):
        _validate_input_param("pb", stale, "anything")


def test_typed_default_is_normalized_at_load():
    """宣言された default も入力値と同じ型契約を通る (F4)。"""
    p = InputParam(name="budget", description="x", param_type="number", default="12")
    assert p.default == 12

    b = InputParam(name="flag", description="x", param_type="boolean", default="true")
    assert b.default is True


def test_unconvertible_default_fails_the_load():
    with pytest.raises(ValidationError, match="default value"):
        InputParam(name="budget", description="x", param_type="number", default="abc")


def test_enum_default_outside_values_fails_the_load():
    with pytest.raises(ValidationError, match="default value"):
        InputParam(
            name="mode", description="x", param_type="enum",
            enum_values=["even", "free"], default="chaos",
        )


def test_playbook_with_bad_default_fails_to_load():
    """default の型違反は Playbook ごとロードを失敗させる (fail-closed)。"""
    with pytest.raises(ValidationError, match="default value"):
        PlaybookSchema(**_pb_dict(
            input_schema=[{
                "name": "budget", "description": "x",
                "param_type": "number", "default": "abc",
            }],
        ))


def test_enum_param_dynamic_source_not_checked():
    """enum_source (動的 enum) は実行時に集合を取れないため素通し。"""
    p = _param(param_type="enum", enum_source="playbooks:router_callable")
    assert _validate_input_param("pb", p, "anything") == "anything"


def test_string_and_object_params_pass_through():
    assert _validate_input_param("pb", _param(param_type="string"), 123) == 123
    obj = {"type": "object"}
    assert _validate_input_param("pb", _param(param_type="object"), obj) is obj


def test_compile_applies_validation_to_provided_args(monkeypatch):
    """graph 起動時に args の値が宣言型へ正規化されて state に載る。"""
    captured = _capture_initial_state(monkeypatch)
    playbook = _ns_playbook(
        input_schema=[_param(name="budget", param_type="number")],
    )

    compile_with_langgraph(
        MagicMock(), playbook, SimpleNamespace(execution_state={}), "b1",
        None, False, base_messages=[], pulse_id="p1",
        parent_state={"_args": {"budget": "12"}},
    )

    assert captured["budget"] == 12


def test_compile_applies_validation_to_declared_default(monkeypatch):
    """default 経由の値も宣言型へ正規化されて state に載る (F4)。

    ロード時に正規化済みでも、validator を経ていない schema オブジェクト
    (直接組み立て / stale) が同じ穴を開けないことを固定する。
    """
    captured = _capture_initial_state(monkeypatch)
    stale = SimpleNamespace(
        name="budget", param_type="number", required=False,
        default="12", enum_values=None, enum_source=None,
    )
    playbook = _ns_playbook(input_schema=[stale])

    compile_with_langgraph(
        MagicMock(), playbook, SimpleNamespace(execution_state={}), "b1",
        None, False, base_messages=[], pulse_id="p1", parent_state={},
    )

    assert captured["budget"] == 12


def test_compile_required_missing_is_warn_only(monkeypatch, caplog):
    """required 欠落は空文字 fallback + WARNING (既存 52 パラメータ依存の W10 裁定)。"""
    captured = _capture_initial_state(monkeypatch)
    playbook = _ns_playbook(
        input_schema=[_param(name="input", required=True)],
    )

    with caplog.at_level("WARNING"):
        compile_with_langgraph(
            MagicMock(), playbook, SimpleNamespace(execution_state={}), "b1",
            None, False, base_messages=[], pulse_id="p1",
            parent_state={},
        )

    assert captured["input"] == ""
    assert any("required input 'input' missing" in r.message for r in caplog.records)
