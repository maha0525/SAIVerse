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
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from llm_clients.exceptions import LLMError
from sea.playbook_models import InputParam, PlaybookSchema
from sea.runtime import SEARuntime
from sea.runtime_graph import _validate_input_param, compile_with_langgraph


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
