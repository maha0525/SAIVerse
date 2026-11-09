"""ツールを待つ関数 (sea/tool_wait.py) と、それを使う待ち場所の回帰テスト。

docs/intent/stop_button.md の保障 3・4:

- 停止ボタン ("user_stop") と終了処理 ("server_shutdown") の合図なら、ツールが
  戻らなくても待つのをやめて ExecutionCancelledException で抜ける。
- 新しい発言などの割り込み ("user" / "schedule" / "auto") では、ツールの完了を
  待つ (2026-07-16 の決定)。
- 正常な結果とツールの例外はそのまま通る。印が無ければ普通に待つ。
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sea.cancellation import CancellationToken, ExecutionCancelledException
from sea.tool_wait import await_tool_call, wait_tool_call_sync
from tools.context import get_active_persona_id, persona_context


class _HungTool:
    """``release`` が立つまで戻らない同期ツール。走ったスレッドを記録する。"""

    def __init__(self, result: str = "done") -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.result = result
        self.thread: threading.Thread | None = None

    def __call__(self, **kwargs):
        self.thread = threading.current_thread()
        self.started.set()
        # テストが失敗してもスレッドを永久に残さない上限 (daemon なので
        # プロセスの終了は妨げないが、念のため)。
        self.release.wait(30)
        return self.result


def _cancel_after(token: CancellationToken, interrupted_by: str, delay: float) -> None:
    threading.Timer(delay, token.cancel, kwargs={"interrupted_by": interrupted_by}).start()


# ---------------------------------------------------------------------------
# async 版
# ---------------------------------------------------------------------------


def test_hung_sync_tool_is_abandoned_on_user_stop():
    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "user_stop", 0.1)

    started_at = time.monotonic()
    with pytest.raises(ExecutionCancelledException) as ei:
        asyncio.run(await_tool_call(tool, {}, cancellation_token=token, tool_name="hung"))
    elapsed = time.monotonic() - started_at

    assert ei.value.interrupted_by == "user_stop"
    assert elapsed < 1.0
    # 抜けた後もツールのスレッドは裏で動き続ける。daemon なのでプロセスの
    # 終了は妨げない。
    assert tool.thread is not None and tool.thread.daemon
    assert tool.thread.is_alive()
    tool.release.set()


def test_hung_sync_tool_is_abandoned_on_server_shutdown():
    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "server_shutdown", 0.1)
    with pytest.raises(ExecutionCancelledException) as ei:
        asyncio.run(await_tool_call(tool, {}, cancellation_token=token))
    assert ei.value.interrupted_by == "server_shutdown"
    tool.release.set()


@pytest.mark.parametrize("interrupted_by", ["user", "schedule", "auto", None])
def test_interrupt_by_new_message_keeps_waiting_for_the_tool(interrupted_by):
    """新しい発言などの割り込みでは抜けず、ツールの結果を受け取る。"""
    tool = _HungTool(result="image-ready")
    token = CancellationToken()

    async def scenario():
        task = asyncio.ensure_future(
            await_tool_call(tool, {}, cancellation_token=token, tool_name="slow")
        )
        await asyncio.sleep(0.05)
        token.cancel(interrupted_by=interrupted_by)
        await asyncio.sleep(0.6)  # 何回分も印を見る時間
        assert not task.done(), "割り込みでツールを待つのをやめてしまった"
        tool.release.set()
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(scenario()) == "image-ready"


def test_stop_after_interrupt_overwrites_and_abandons():
    """割り込み済みの印に後から停止ボタンが押されたら ("user" → "user_stop") 抜ける。"""
    tool = _HungTool()
    token = CancellationToken()

    async def scenario():
        task = asyncio.ensure_future(await_tool_call(tool, {}, cancellation_token=token))
        token.cancel(interrupted_by="user")
        await asyncio.sleep(0.4)
        assert not task.done()
        token.cancel(interrupted_by="user_stop")
        return await asyncio.wait_for(task, timeout=2)

    with pytest.raises(ExecutionCancelledException) as ei:
        asyncio.run(scenario())
    assert ei.value.interrupted_by == "user_stop"
    tool.release.set()


def test_already_stopped_token_does_not_start_the_tool():
    calls = []
    token = CancellationToken()
    token.cancel(interrupted_by="user_stop")
    with pytest.raises(ExecutionCancelledException):
        asyncio.run(await_tool_call(lambda: calls.append(1), {}, cancellation_token=token))
    assert calls == []


def test_normal_result_and_kwargs_pass_through():
    token = CancellationToken()
    result = asyncio.run(
        await_tool_call(lambda a, b: (a, b, {"k": 1}), {"a": 1, "b": "x"}, cancellation_token=token)
    )
    assert result == (1, "x", {"k": 1})


def test_tool_exception_propagates_unchanged():
    class _Boom(RuntimeError):
        pass

    def broken():
        raise _Boom("broken tool")

    with pytest.raises(_Boom, match="broken tool"):
        asyncio.run(await_tool_call(broken, {}, cancellation_token=CancellationToken()))


def test_no_token_is_a_plain_call():
    assert asyncio.run(await_tool_call(lambda: "ok", {}, cancellation_token=None)) == "ok"
    assert asyncio.run(await_tool_call(None, {}, cancellation_token=None)) is None


def test_sync_tool_runs_in_daemon_thread_with_caller_contextvars():
    """同期ツールは専用の daemon スレッドで走り、persona_context が見える。"""
    seen = {}

    def tool():
        seen["persona"] = get_active_persona_id()
        seen["thread"] = threading.current_thread()
        return "ok"

    async def scenario():
        with persona_context("persona_x", "."):
            return await await_tool_call(tool, {}, cancellation_token=CancellationToken())

    assert asyncio.run(scenario()) == "ok"
    assert seen["persona"] == "persona_x"
    assert seen["thread"] is not threading.main_thread()
    assert seen["thread"].daemon


def test_sync_tool_returning_awaitable_is_awaited():
    async def inner():
        return "from-awaitable"

    def tool():
        return inner()

    assert asyncio.run(await_tool_call(tool, {}, cancellation_token=CancellationToken())) == "from-awaitable"


def test_hung_coroutine_tool_is_abandoned_and_its_task_cancelled():
    state = {"cancelled": False}

    async def hung():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    token = CancellationToken()

    async def scenario():
        asyncio.get_running_loop().call_later(0.1, token.cancel, "user_stop")
        try:
            await await_tool_call(hung, {}, cancellation_token=token)
        finally:
            await asyncio.sleep(0.05)  # 取り消したタスクに一度走る機会を渡す

    started_at = time.monotonic()
    with pytest.raises(ExecutionCancelledException):
        asyncio.run(scenario())
    assert time.monotonic() - started_at < 1.0
    assert state["cancelled"] is True


def test_coroutine_tool_keeps_running_on_interrupt():
    async def slow():
        await asyncio.sleep(0.5)
        return "finished"

    token = CancellationToken()

    async def scenario():
        asyncio.get_running_loop().call_later(0.05, token.cancel, "user")
        return await await_tool_call(slow, {}, cancellation_token=token)

    assert asyncio.run(scenario()) == "finished"


def test_asyncio_run_returns_without_waiting_for_the_abandoned_thread():
    """asyncio.run の後片付け (既定の executor の shutdown) が固まったスレッドを
    待たない — 既定の executor を使わない理由そのもの。"""
    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "user_stop", 0.1)
    started_at = time.monotonic()
    with pytest.raises(ExecutionCancelledException):
        asyncio.run(await_tool_call(tool, {}, cancellation_token=token))
    assert time.monotonic() - started_at < 1.0
    assert tool.thread is not None and tool.thread.is_alive()
    tool.release.set()


# ---------------------------------------------------------------------------
# 同期版 (_execute_handy_tool_inline が使う)
# ---------------------------------------------------------------------------


def test_sync_variant_abandons_on_user_stop():
    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "user_stop", 0.1)
    started_at = time.monotonic()
    with pytest.raises(ExecutionCancelledException) as ei:
        wait_tool_call_sync(tool, {}, cancellation_token=token, tool_name="hung")
    assert ei.value.interrupted_by == "user_stop"
    assert time.monotonic() - started_at < 1.0
    assert tool.thread is not None and tool.thread.daemon
    tool.release.set()


def test_sync_variant_keeps_waiting_on_interrupt():
    tool = _HungTool(result="r")
    token = CancellationToken()
    token.cancel(interrupted_by="user")
    threading.Timer(0.5, tool.release.set).start()
    assert wait_tool_call_sync(tool, {}, cancellation_token=token) == "r"


def test_sync_variant_result_exception_and_context():
    with pytest.raises(ValueError, match="bad"):
        wait_tool_call_sync(lambda: (_ for _ in ()).throw(ValueError("bad")), {}, cancellation_token=None)
    with persona_context("persona_y", "."):
        assert wait_tool_call_sync(get_active_persona_id, {}, cancellation_token=CancellationToken()) == "persona_y"


# ---------------------------------------------------------------------------
# 待ち場所: スペル (_run_spell_tool_async)
# ---------------------------------------------------------------------------


def _spell_state(token: CancellationToken) -> dict:
    return {
        "_persona_obj": SimpleNamespace(
            persona_id="test_persona", persona_log_path=None, manager_ref=None,
        ),
        "_pulse_context": None,
        "_cancellation_token": token,
    }


def test_spell_with_hung_tool_raises_cancellation_instead_of_spell_error():
    """固まったスペルが停止ボタンで ExecutionCancelledException として外へ出る
    ("Spell error" の文字列に畳まれない — 外側の停止の処理が通告②を置く)。"""
    from sea.runtime_llm import _run_spell_tool_async

    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "user_stop", 0.1)
    with patch.dict("sea.runtime_llm.TOOL_REGISTRY", {"_hung_spell": tool}, clear=False):
        with pytest.raises(ExecutionCancelledException) as ei:
            asyncio.run(_run_spell_tool_async(
                tool_name="_hung_spell", tool_args={}, persona=None,
                state=_spell_state(token), playbook_name="test_playbook",
                event_callback=None,
            ))
    assert ei.value.interrupted_by == "user_stop"
    tool.release.set()


def test_spell_waits_for_tool_on_new_message_interrupt():
    from sea.runtime_llm import _run_spell_tool_async

    tool = _HungTool(result="spell-result")
    token = CancellationToken()
    token.cancel(interrupted_by="user")
    threading.Timer(0.5, tool.release.set).start()
    with patch.dict("sea.runtime_llm.TOOL_REGISTRY", {"_slow_spell": tool}, clear=False):
        text, _meta, ok = asyncio.run(_run_spell_tool_async(
            tool_name="_slow_spell", tool_args={}, persona=None,
            state=_spell_state(token), playbook_name="test_playbook",
            event_callback=None,
        ))
    assert (text, ok) == ("spell-result", True)


# ---------------------------------------------------------------------------
# 待ち場所: TOOL ノード (runtime_nodes.lg_tool_call_node)
# ---------------------------------------------------------------------------


def test_tool_call_node_propagates_stop_instead_of_tool_error():
    from sea.runtime_nodes import lg_tool_call_node

    tool = _HungTool()
    token = CancellationToken()
    _cancel_after(token, "user_stop", 0.1)

    runtime = SimpleNamespace(
        _resolve_state_value=lambda state, path: {"fc.name": "_hung_node_tool", "fc.args": {}}.get(path),
        _append_tool_result_message=lambda *a, **k: None,
    )
    node_def = SimpleNamespace(id="call", call_source="fc", output_key=None)
    playbook = SimpleNamespace(name="pb", display_name=None)
    persona = SimpleNamespace(persona_id="p1", persona_log_path=None, manager_ref=None)
    node = lg_tool_call_node(runtime, node_def, persona, playbook)
    state = {"_cancellation_token": token}
    with patch.dict("tools.TOOL_REGISTRY", {"_hung_node_tool": tool}, clear=False):
        with pytest.raises(ExecutionCancelledException):
            asyncio.run(node(state))
    assert not str(state.get("last", "")).startswith("Tool error")
    tool.release.set()


# ---------------------------------------------------------------------------
# Beat ロックの取り直し: ツールのスレッドから親の握るロックを取りに行ったら、
# 永久に固まらずにその場で投げる (Codex 指摘、docs/intent/stop_button.md)。
# ---------------------------------------------------------------------------


def test_hold_beat_from_tool_thread_raises_instead_of_deadlocking():
    import types as _types

    from sea.beat_gate import BeatGate, BeatLockFromToolThreadError
    from sea.tool_wait import wait_tool_call_sync

    gate = BeatGate(_types.SimpleNamespace())

    def tool_that_reenters_beat():
        with gate.hold("p1", purpose="nested"):
            return "unreachable"

    started = time.monotonic()
    with gate.hold("p1", purpose="parent"):
        with pytest.raises(BeatLockFromToolThreadError):
            wait_tool_call_sync(tool_that_reenters_beat, {}, cancellation_token=None)
    assert time.monotonic() - started < 5


def test_hold_beat_from_tool_thread_succeeds_when_lock_is_free():
    import types as _types

    from sea.beat_gate import BeatGate
    from sea.tool_wait import wait_tool_call_sync

    gate = BeatGate(_types.SimpleNamespace())

    def tool_with_beat():
        with gate.hold("p2", purpose="own"):
            return "ok"

    assert wait_tool_call_sync(tool_with_beat, {}, cancellation_token=None) == "ok"


def test_hold_beat_from_tool_thread_waits_for_an_unrelated_holder():
    """親が握っていないペルソナのロックは、関係のない処理が一時的に握っていても待って取る (Codex 2 巡目)。"""
    import types as _types

    from sea.beat_gate import BeatGate
    from sea.tool_wait import wait_tool_call_sync

    gate = BeatGate(_types.SimpleNamespace())
    holder_has_lock = threading.Event()
    release_holder = threading.Event()

    def unrelated_holder():
        with gate.hold("p3", purpose="unrelated_job"):
            holder_has_lock.set()
            release_holder.wait(5)

    holder = threading.Thread(target=unrelated_holder, daemon=True)
    holder.start()
    assert holder_has_lock.wait(5)
    threading.Timer(0.3, release_holder.set).start()

    def tool_with_beat():
        with gate.hold("p3", purpose="tool"):
            return "acquired after the unrelated holder released"

    assert wait_tool_call_sync(tool_with_beat, {}, cancellation_token=None) == (
        "acquired after the unrelated holder released"
    )
    holder.join(5)


def test_abandoned_tool_cannot_enter_the_personas_beat_afterwards():
    """停止で見捨てたツールは、親が抜けて新しい Beat が始まった後も、ロックが空いた後も、
    ペルソナの Beat に入らずに投げる (Codex 3 巡目、docs/intent/stop_button.md)。"""
    import types as _types

    from sea.beat_gate import BeatGate, BeatLockFromToolThreadError
    from sea.cancellation import CancellationToken, ExecutionCancelledException
    from sea.tool_wait import wait_tool_call_sync

    gate = BeatGate(_types.SimpleNamespace())
    token = CancellationToken()
    go_enter = threading.Event()
    outcome: dict = {}

    def slow_tool():
        go_enter.wait(5)
        try:
            with gate.hold("p4", purpose="late_tool"):
                outcome["entered"] = True
        except BeatLockFromToolThreadError as exc:
            outcome["error"] = exc

    # 親: Beat を握ってツールを待ち、停止で抜ける
    threading.Timer(0.3, lambda: token.cancel(interrupted_by="user_stop")).start()
    with gate.hold("p4", purpose="parent"):
        with pytest.raises(ExecutionCancelledException):
            wait_tool_call_sync(slow_tool, {}, cancellation_token=token)

    # 新しい Beat が同じペルソナのロックを握っている最中に、見捨てたツールが入ろうとする
    new_beat_holding = threading.Event()
    release_new_beat = threading.Event()

    def new_beat():
        with gate.hold("p4", purpose="new_reply"):
            new_beat_holding.set()
            release_new_beat.wait(5)

    t = threading.Thread(target=new_beat, daemon=True)
    t.start()
    assert new_beat_holding.wait(5)
    go_enter.set()
    deadline = time.monotonic() + 5
    while not outcome and time.monotonic() < deadline:
        time.sleep(0.02)
    release_new_beat.set()
    t.join(5)
    assert "entered" not in outcome
    assert isinstance(outcome.get("error"), BeatLockFromToolThreadError)


# ---------------------------------------------------------------------------
# 子の line を走らせるツール (run_playbook) の停止 (PR #467 の Codex P1)。
# 親が待つのをやめて先に後始末 (pop_line) すると、子の line が外され、子の
# その後の記録が親の line に帰属する。子に停止を渡し、子が自分の line を
# 外すまで親は待つ。
# ---------------------------------------------------------------------------


def test_stop_waits_until_the_child_line_is_popped():
    from sea.cancellation import CancellationToken, ExecutionCancelledException
    from sea.pulse_context import PulseContext
    from sea.tool_wait import current_tool_cancellation_token, wait_tool_call_sync
    from tools.context import persona_context

    pulse_ctx = PulseContext(pulse_id="pulse-child")
    pulse_ctx.push_line(role="main_line")
    entry_depth = pulse_ctx.line_depth()
    token = CancellationToken()
    seen: dict = {}

    def child_line_tool():
        child_token = current_tool_cancellation_token()
        seen["token_is_parents"] = child_token is token
        pulse_ctx.push_line(role="sub_line")
        try:
            while not child_token.is_cancelled():
                time.sleep(0.02)
            time.sleep(0.4)  # 子が自分の区切りまで進んで抜ける間
            seen["child_frame_was_still_on_top"] = pulse_ctx.line_depth() == entry_depth + 1
        finally:
            pulse_ctx.pop_line()
        raise ExecutionCancelledException(interrupted_by=child_token.interrupted_by)

    threading.Timer(0.2, lambda: token.cancel(interrupted_by="user_stop")).start()
    with persona_context("p", ".", pulse_context=pulse_ctx):
        with pytest.raises(ExecutionCancelledException):
            wait_tool_call_sync(child_line_tool, {}, cancellation_token=token)
        # 親が抜けた時点で、子はもう自分の line を外し終えている
        assert pulse_ctx.line_depth() == entry_depth
    assert seen["token_is_parents"] is True
    assert seen["child_frame_was_still_on_top"] is True


def test_run_playbook_hands_the_stop_signal_to_the_child_and_reraises_it():
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from sea.cancellation import CancellationToken, ExecutionCancelledException
    from sea.tool_wait import wait_tool_call_sync

    tool_path = Path(__file__).resolve().parent.parent / "builtin_data" / "tools" / "run_playbook.py"
    spec = importlib.util.spec_from_file_location("run_playbook_for_stop_test", str(tool_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from sea.runtime import SEARuntime
    runtime = MagicMock()
    runtime.decide_playbook_permission = (
        lambda *a, **kw: SEARuntime.decide_playbook_permission(runtime, *a, **kw)
    )
    runtime._load_playbook_for = MagicMock(
        return_value=SimpleNamespace(name="memory_research", router_callable=True)
    )
    captured: dict = {}

    def fake_run(playbook, persona_obj, building_id, **kwargs):
        captured["token"] = kwargs["parent_state"].get("_cancellation_token")
        raise ExecutionCancelledException(interrupted_by="user_stop")

    runtime._run_playbook = MagicMock(side_effect=fake_run)
    persona_obj = SimpleNamespace(current_building_id="b")
    manager = SimpleNamespace(sea_runtime=runtime, personas={"p": persona_obj})
    pulse_ctx = SimpleNamespace(_line_stack=[object()], pulse_id="pulse-x")
    token = CancellationToken()

    with patch.object(module, "get_active_persona_id", return_value="p"), \
         patch.object(module, "get_active_manager", return_value=manager), \
         patch.object(module, "get_active_pulse_context", return_value=pulse_ctx):
        with pytest.raises(ExecutionCancelledException):
            wait_tool_call_sync(module.run_playbook, {"name": "memory_research"}, cancellation_token=token)
    assert captured["token"] is token


def test_a_new_message_after_stop_does_not_cancel_the_stop():
    """停止ボタンの直後に新しい発言の割り込みが届いても、停止は消えない (PR #467 の Codex P2)。"""
    from sea.cancellation import CancellationToken, ExecutionCancelledException
    from sea.tool_wait import wait_tool_call_sync

    token = CancellationToken()
    token.cancel(interrupted_by="user_stop")
    token.cancel(interrupted_by="user")  # 0.2 秒の見回りの間に届いた新しい発言
    assert token.interrupted_by == "user_stop"

    never = threading.Event()
    started = time.monotonic()
    with pytest.raises(ExecutionCancelledException):
        wait_tool_call_sync(lambda: never.wait(), {}, cancellation_token=token)
    assert time.monotonic() - started < 2


def test_stop_after_an_interrupt_still_turns_into_a_stop():
    """割り込み ("user") のあとに停止ボタンを押せば、停止に変わる (従来どおり)。"""
    from sea.cancellation import CancellationToken

    token = CancellationToken()
    token.cancel(interrupted_by="user")
    token.cancel(interrupted_by="user_stop")
    assert token.interrupted_by == "user_stop"
