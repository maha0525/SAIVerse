"""langgraph との境界を実物で固定する (依存関係の更新 2026-09-02、langgraph 1.0 → 1.2)。

SEA の他のテストは `compile_playbook` を偽物に差し替えているので、実物の
langgraph を通る場所はここだけ。守りたい契約は `sea/runtime_graph.py` が
実行結果に対して行う `isinstance(final_state, dict)` 判定 — output_schema の
親 state への書き戻しと PulseContext の flush はこの判定を入口にしている。

langgraph 1.1 で `invoke()` / `ainvoke()` に `version="v2"` が入り、そちらは
dict ではなく `GraphOutput` (dict 風アクセスは `LangGraphDeprecatedSinceV11`
警告) を返す。SAIVerse は `version` を渡さないので既定の v1 のまま素の dict が
返らなければならない。
"""
from __future__ import annotations

import asyncio
import warnings
from unittest.mock import MagicMock

import pytest

from sea.langgraph_runner import compile_playbook
from sea.playbook_models import PlaybookSchema


def _compile(playbook: PlaybookSchema):
    def set_factory(node_def):
        # 実物の SET ノードは runtime 側の関数。ここでは「どのノードを通ったか」を
        # state に刻むだけの最小の代替で、langgraph の配線だけを試す。
        def _node(state: dict) -> dict:
            return {**state, "visited": [*state.get("visited", []), node_def.id]}
        return _node

    compiled = compile_playbook(
        playbook,
        llm_node_factory=MagicMock(),
        tool_node_factory=MagicMock(),
        speak_node=MagicMock(),
        think_node=MagicMock(),
        set_node_factory=set_factory,
    )
    assert compiled is not None, "langgraph が入っていれば compile は None を返さない"
    return compiled


def _run(compiled, state: dict):
    # runtime_graph.py と同じ呼び方 (config だけを渡し、version は渡さない)。
    return asyncio.run(compiled(state, {"recursion_limit": 1000}))


def test_ainvoke_result_is_a_plain_dict_without_langgraph_deprecation():
    playbook = PlaybookSchema(
        name="boundary_linear",
        description="t",
        input_schema=[],
        start_node="a",
        nodes=[
            {"id": "a", "type": "set", "assignments": {"x": 1}, "next": "b"},
            {"id": "b", "type": "pass", "next": None},
        ],
    )
    compiled = _compile(playbook)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)  # langgraph の非推奨警告は DeprecationWarning の子
        result = _run(compiled, {"k": "v"})

    assert type(result) is dict
    assert result["k"] == "v"
    assert result["visited"] == ["a"]


def test_conditional_next_routes_through_real_langgraph():
    playbook = PlaybookSchema(
        name="boundary_conditional",
        description="t",
        input_schema=[],
        start_node="gate",
        nodes=[
            {
                "id": "gate", "type": "pass",
                "conditional_next": {"field": "route", "cases": {"left": "l", "default": "r"}},
            },
            {"id": "l", "type": "set", "assignments": {"x": 1}, "next": None},
            {"id": "r", "type": "set", "assignments": {"x": 2}, "next": None},
        ],
    )
    compiled = _compile(playbook)

    assert _run(compiled, {"route": "left"})["visited"] == ["l"]
    assert _run(compiled, {"route": "other"})["visited"] == ["r"]


@pytest.mark.parametrize("value, expected", [(5, ["hi"]), (1, ["lo"])])
def test_numeric_conditional_next_routes_through_real_langgraph(value, expected):
    playbook = PlaybookSchema(
        name="boundary_numeric",
        description="t",
        input_schema=[],
        start_node="gate",
        nodes=[
            {
                "id": "gate", "type": "pass",
                "conditional_next": {"field": "n", "operator": "gte", "cases": {"3": "hi", "default": "lo"}},
            },
            {"id": "hi", "type": "set", "assignments": {"x": 1}, "next": None},
            {"id": "lo", "type": "set", "assignments": {"x": 2}, "next": None},
        ],
    )
    compiled = _compile(playbook)

    assert _run(compiled, {"n": value})["visited"] == expected
