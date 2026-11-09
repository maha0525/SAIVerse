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
