"""SubPlay line='main'/'sub' 動作確認テスト (Phase C-2a)。

ライン分岐ロジックの最小ユニットテスト:
- SubPlayNodeDef.line デフォルト = "main"
- line='sub' 指定時に run_playbook が parent _messages のコピーを base_messages として使う
- line='sub' で _force_lightweight_model フラグが立つ
- subplay 完了時に report_to_main が親 _messages に system タグ付き user として append される

実 LLM は呼ばず、runtime のロジック分岐のみを検証する。
"""
from unittest.mock import MagicMock

from sea.playbook_models import SubPlayNodeDef


# ---------------------------------------------------------------------------
# SubPlayNodeDef.line フィールド
# ---------------------------------------------------------------------------

def test_subplay_node_def_line_default_main():
    n = SubPlayNodeDef(id="x", type="subplay", playbook="dummy")
    assert n.line == "main"


def test_subplay_node_def_line_can_be_sub():
    n = SubPlayNodeDef(id="x", type="subplay", playbook="dummy", line="sub")
    assert n.line == "sub"


def test_subplay_node_def_line_rejects_unknown():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SubPlayNodeDef(id="x", type="subplay", playbook="dummy", line="other")


# ---------------------------------------------------------------------------
# run_playbook のライン分岐: line='sub' で _prepare_context を bypass し
# parent _messages のコピーを使う
# ---------------------------------------------------------------------------

def test_run_playbook_line_sub_uses_parent_messages():
    """line='sub' のとき、_prepare_context を呼ばずに parent _messages のコピーを使う。"""
    from sea.runtime_runner import run_playbook

    runtime = MagicMock()
    # _prepare_context が呼ばれたら誰かが呼んだとわかるよう sentinel
    runtime._prepare_context = MagicMock(return_value=[{"role": "system", "content": "from-prepare"}])
    runtime._compile_with_langgraph = MagicMock(return_value=["ok"])

    parent_messages = [
        {"role": "system", "content": "parent-system"},
        {"role": "user", "content": "parent-user"},
        {"role": "assistant", "content": "parent-assistant"},
    ]
    parent_state = {"_messages": parent_messages}

    persona = MagicMock()
    persona.execution_state = {}

    playbook = MagicMock()
    playbook.name = "test_pb"
    playbook.start_node = "n0"
    playbook.context_requirements = None

    run_playbook(
        runtime=runtime,
        playbook=playbook,
        persona=persona,
        building_id="b1",
        user_input="hello",
        auto_mode=False,
        parent_state=parent_state,
        line="sub",
    )

    # _prepare_context は呼ばれない
    assert runtime._prepare_context.call_count == 0
    # _compile_with_langgraph に渡された base_messages は parent_messages のコピー
    call_args = runtime._compile_with_langgraph.call_args
    passed_base_messages = call_args.args[5]  # 6 番目の positional arg
    assert passed_base_messages == parent_messages
    # コピーであって同一参照ではない
    assert passed_base_messages is not parent_messages


def test_run_playbook_line_sub_sets_force_lightweight_flag():
    """line='sub' のとき、parent_state に _force_lightweight_model=True が入る。"""
    from sea.runtime_runner import run_playbook

    runtime = MagicMock()
    runtime._prepare_context = MagicMock(return_value=[])
    runtime._compile_with_langgraph = MagicMock(return_value=[])

    parent_state = {"_messages": []}
    persona = MagicMock()
    persona.execution_state = {}
    playbook = MagicMock()
    playbook.name = "test_pb"
    playbook.start_node = "n0"
    playbook.context_requirements = None

    run_playbook(
        runtime=runtime,
        playbook=playbook,
        persona=persona,
        building_id="b1",
        user_input=None,
        auto_mode=False,
        parent_state=parent_state,
        line="sub",
    )

    assert parent_state.get("_force_lightweight_model") is True


def test_run_playbook_line_main_calls_prepare_context():
    """line='main' (default) は従来通り _prepare_context を呼ぶ。"""
    from sea.runtime_runner import run_playbook

    runtime = MagicMock()
    runtime._prepare_context = MagicMock(return_value=[{"role": "system", "content": "from-prepare"}])
    runtime._compile_with_langgraph = MagicMock(return_value=[])

    parent_state = {"_messages": [{"role": "user", "content": "ignored"}]}
    persona = MagicMock()
    persona.execution_state = {}
    playbook = MagicMock()
    playbook.name = "test_pb"
    playbook.start_node = "n0"
    playbook.context_requirements = None

    run_playbook(
        runtime=runtime,
        playbook=playbook,
        persona=persona,
        building_id="b1",
        user_input="hello",
        auto_mode=False,
        parent_state=parent_state,
        line="main",
    )

    assert runtime._prepare_context.call_count == 1
    # _force_lightweight_model フラグは立てない
    assert parent_state.get("_force_lightweight_model") is None


# ---------------------------------------------------------------------------
# subplay node の line='sub' 完了処理: report_to_main を 2 経路で親に渡す
#
# (1) state["_messages"] への append (context_profile 不使用ノード向け)
# (2) 親 PulseContext への append (context_profile 使用ノード向け)
#
# Phase C-2a の最初の実装では (1) のみで、(2) が抜けていたため
# context_profile を使うメインライン LLM ノードからは見えなかった。
# まはー指摘 2026-04-28 の修正後の挙動を検証する。
# ---------------------------------------------------------------------------

import asyncio
from types import SimpleNamespace


def _make_subplay_test_env(report_value=None, sub_line="sub"):
    """subplay node を呼ぶための最小限の環境を構築。"""
    from sea.runtime_nodes import lg_subplay_node
    from sea.pulse_context import PulseContext

    # runtime mock
    runtime = MagicMock()
    runtime._effective_building_id = lambda persona, building_id: building_id
    runtime._start_subagent_thread = lambda persona, label: (None, None)

    # _run_playbook の副作用として state に report_to_main を入れる
    def fake_run_playbook(sub_pb, persona, eff_bid, sub_input, auto_mode, record_history,
                         state, event_callback, **kwargs):
        if report_value is not None:
            state["report_to_main"] = report_value
        return ["ok"]

    runtime._run_playbook = MagicMock(side_effect=fake_run_playbook)

    # persona, playbook
    persona = MagicMock()
    persona.persona_id = "test_persona"

    playbook = SimpleNamespace(name="parent_pb")

    # node_def
    node_def = SimpleNamespace(
        id="call_sub",
        playbook="dummy_sub",
        action=None,
        line=sub_line,
        execution="inline",
        isolate_pulse_context=False,
        args=None,
        propagate_output=False,
        subagent_chronicle=False,
    )

    # sub_pb (最低限あればよい)
    runtime._load_playbook_for = lambda name, p, b: SimpleNamespace(name=name, output_schema=["report_to_main"])

    # state
    pulse_ctx = PulseContext(pulse_id="test-pulse", thread_id="test-thread")
    state = {
        "_messages": [{"role": "user", "content": "initial"}],
        "_pulse_context": pulse_ctx,
        "_cancellation_token": None,
    }

    # node 関数を生成
    node_fn = lg_subplay_node(runtime, node_def, persona, "b1", playbook, False, [], None)
    return node_fn, state, pulse_ctx


def test_subplay_line_sub_appends_report_to_both_messages_and_pulse_ctx():
    """report_to_main があれば state['_messages'] と PulseContext と SAIMemory の3経路で記録される。"""
    node_fn, state, pulse_ctx = _make_subplay_test_env(report_value="検索結果のまとめ。")
    runtime = node_fn.__closure__[0].cell_contents if False else None  # noqa: F841
    asyncio.run(node_fn(state))

    # (1) state["_messages"] に append された
    assert any(
        msg["role"] == "user" and "検索結果のまとめ。" in msg["content"]
        for msg in state["_messages"]
    ), "report_to_main should be in state['_messages']"

    # (2) 親 PulseContext にも append された
    appended_in_pulse = [e for e in pulse_ctx.logs if "検索結果のまとめ。" in (e.content or "")]
    assert len(appended_in_pulse) == 1, (
        f"report_to_main should be appended to parent PulseContext exactly once "
        f"(got {len(appended_in_pulse)})"
    )
    assert appended_in_pulse[0].role == "user"

    # state["report_to_main"] はクリアされる
    assert "report_to_main" not in state


def test_subplay_line_sub_stores_report_to_saimemory_with_conversation_tag():
    """report_to_main は SAIMemory にも conversation タグで記録される (次の Pulse 以降の参照用)。"""
    from sea.runtime_nodes import lg_subplay_node
    from sea.pulse_context import PulseContext

    runtime = MagicMock()
    runtime._effective_building_id = lambda persona, building_id: building_id
    runtime._start_subagent_thread = lambda persona, label: (None, None)

    def fake_run_playbook(sub_pb, persona, eff_bid, sub_input, auto_mode, record_history,
                         state, event_callback, **kwargs):
        state["report_to_main"] = "重要なまとめ"
        return ["ok"]

    runtime._run_playbook = MagicMock(side_effect=fake_run_playbook)
    runtime._store_memory = MagicMock(return_value=True)
    runtime._load_playbook_for = lambda name, p, b: SimpleNamespace(
        name=name, output_schema=["report_to_main"]
    )

    persona = MagicMock()
    persona.persona_id = "test_persona"
    playbook = SimpleNamespace(name="parent_pb")
    node_def = SimpleNamespace(
        id="call_sub", playbook="dummy_sub", action=None, line="sub",
        execution="inline", isolate_pulse_context=False, args=None,
        propagate_output=False, subagent_chronicle=False,
    )
    pulse_ctx = PulseContext(pulse_id="test-pulse-xyz", thread_id="test-thread")
    state = {
        "_messages": [],
        "_pulse_context": pulse_ctx,
        "_pulse_id": "test-pulse-xyz",
        "_cancellation_token": None,
    }

    node_fn = lg_subplay_node(runtime, node_def, persona, "b1", playbook, False, [], None)
    asyncio.run(node_fn(state))

    # _store_memory が conversation タグ + 現在の pulse_id で 1 回呼ばれる
    runtime._store_memory.assert_called_once()
    call_kwargs = runtime._store_memory.call_args.kwargs
    assert call_kwargs["role"] == "user"
    assert "conversation" in call_kwargs["tags"]
    assert call_kwargs["pulse_id"] == "test-pulse-xyz"
    # content は formatted な system タグ付きメッセージ
    call_args = runtime._store_memory.call_args.args
    formatted_text = call_args[1]
    assert "重要なまとめ" in formatted_text
    assert "<system>" in formatted_text


def test_subplay_line_main_does_not_call_store_memory():
    """line='main' では SAIMemory への保存処理は走らない。"""
    from sea.runtime_nodes import lg_subplay_node
    from sea.pulse_context import PulseContext

    runtime = MagicMock()
    runtime._effective_building_id = lambda persona, building_id: building_id
    runtime._start_subagent_thread = lambda persona, label: (None, None)

    def fake_run_playbook(sub_pb, persona, eff_bid, sub_input, auto_mode, record_history,
                         state, event_callback, **kwargs):
        state["report_to_main"] = "should-not-store"
        return ["ok"]

    runtime._run_playbook = MagicMock(side_effect=fake_run_playbook)
    runtime._store_memory = MagicMock(return_value=True)
    runtime._load_playbook_for = lambda name, p, b: SimpleNamespace(
        name=name, output_schema=["report_to_main"]
    )

    persona = MagicMock()
    persona.persona_id = "test_persona"
    playbook = SimpleNamespace(name="parent_pb")
    node_def = SimpleNamespace(
        id="call_sub", playbook="dummy_sub", action=None, line="main",
        execution="inline", isolate_pulse_context=False, args=None,
        propagate_output=False, subagent_chronicle=False,
    )
    pulse_ctx = PulseContext(pulse_id="p1", thread_id="t1")
    state = {
        "_messages": [],
        "_pulse_context": pulse_ctx,
        "_pulse_id": "p1",
        "_cancellation_token": None,
    }

    node_fn = lg_subplay_node(runtime, node_def, persona, "b1", playbook, False, [], None)
    asyncio.run(node_fn(state))

    # main ライン継続時は _store_memory 呼ばない
    runtime._store_memory.assert_not_called()


def test_subplay_line_sub_no_report_logs_warning_and_no_append():
    """report_to_main が空なら何も append しない。"""
    node_fn, state, pulse_ctx = _make_subplay_test_env(report_value=None)
    asyncio.run(node_fn(state))

    # _messages に新規 append されてない (initial の 1 件のみ)
    assert len(state["_messages"]) == 1
    # PulseContext にも何も入ってない
    assert len(pulse_ctx.logs) == 0


def test_subplay_line_main_does_not_touch_report_to_main():
    """line='main' なら report_to_main の処理は行われない (state にも残る)。"""
    node_fn, state, pulse_ctx = _make_subplay_test_env(
        report_value="should-not-be-appended", sub_line="main"
    )
    asyncio.run(node_fn(state))

    # state["_messages"] に append されてない
    assert len(state["_messages"]) == 1
    # PulseContext にも append されてない
    assert len(pulse_ctx.logs) == 0
    # report_to_main は state に残る (main ライン継続なので親のロジックが消費する)
    assert state.get("report_to_main") == "should-not-be-appended"
