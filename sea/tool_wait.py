"""ツールの呼び出しを待つ関数 — 停止ボタンと終了処理のときだけ、待つのをやめて抜ける。

docs/intent/stop_button.md (保障 3・4) の実装。ツールを呼んで待つ場所
(スペル ``sea/runtime_llm.py`` の ``_run_spell_tool_async`` /
``_execute_handy_tool_inline``、TOOL ノード ``sea/runtime_engine.py`` /
``sea/runtime_nodes.py``) は、すべてここの関数を通してツールを待つ。止める合図の
確認と、合図の種類に応じて抜けるかどうかの判定は、この関数の中でだけ行う。

- 同期のツール関数は専用の daemon スレッドで走らせる。asyncio の既定の
  executor は使わない — ``asyncio.run`` は終わるときに既定の executor を
  shutdown して、固まったスレッドを待ち続けるため。daemon にするのは、裏で
  動き続けるスレッドがプロセスの終了を妨げないようにするため。
- ``tools/context.py`` の ``persona_context`` は contextvars なので、呼び出し側の
  文脈を ``contextvars.copy_context()`` でスレッドへ写す。
- 待っている間、取り消しの印 (``CancellationToken``) を約 0.2 秒ごとに見る。
  抜けるのは ``interrupted_by`` が ``user_stop`` (停止ボタン) か
  ``server_shutdown`` (終了処理) のときだけ。新しい発言・schedule・auto による
  割り込み ("user" / "schedule" / "auto") ではツールの完了を待つ
  (beat_execution_context.md §3.4、2026-07-16 の決定)。
  ``CancellationToken.cancel()`` は ``interrupted_by`` を上書きするので、割り込み
  済みの印に後から停止ボタンが押されると "user_stop" に変わり、そこで抜ける。
- 抜けた後もツールのスレッドは止められない (Python のスレッドは外から止め
  られない)。後で戻ってきた結果は捨てる。
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import threading
from typing import Any, Callable, Dict, Optional

from sea.cancellation import (
    STOP_INTERRUPTED_BY,
    CancellationToken,
    ExecutionCancelledException,
)

LOGGER = logging.getLogger(__name__)

#: 待つのをやめて抜ける ``interrupted_by`` の値。停止ボタンと終了処理だけ。
ABANDON_INTERRUPTED_BY = STOP_INTERRUPTED_BY

#: 取り消しの印を見る間隔 (秒)。
POLL_INTERVAL_SECONDS = 0.2

#: ツールを走らせるスレッドの名前の頭。``sea.beat_gate`` がこれを見て、
#: ツールのスレッドから親の握る Beat ロックを取りに行く呼び出しを、
#: 待たずに投げる (永久に固まらせない)。
TOOL_THREAD_NAME_PREFIX = "tool-wait:"


#: ツールのスレッドの文脈に置く「待つのをやめた (見捨てた)」印。待つ側が
#: 停止で抜けるときに set する。``sea.beat_gate`` は、見捨てられたツールが
#: ペルソナの Beat ロックを取りに来たら、待たずに投げる — 止めた返事のため
#: に動いていたツールが、後からペルソナの処理の列に割り込んで記録を書かない
#: ため (docs/intent/stop_button.md「抜けたときに何が残るか」)。
_ABANDONED: "contextvars.ContextVar[Optional[threading.Event]]" = contextvars.ContextVar(
    "tool_wait_abandoned", default=None,
)


#: ツールのスレッドの文脈に置く、待つ側が見ている停止の合図
#: (``CancellationToken``)。子の Playbook を走らせるツール (``run_playbook``)
#: はこれを子へ渡し、停止を子まで届ける (docs/intent/stop_button.md)。
_CALL_TOKEN: "contextvars.ContextVar[Optional[CancellationToken]]" = contextvars.ContextVar(
    "tool_wait_cancellation_token", default=None,
)


def current_tool_cancellation_token() -> Optional[CancellationToken]:
    """いまのツールを待っている側の停止の合図。ツールの外・合図の無い呼び出しでは None。"""
    return _CALL_TOKEN.get()


def is_tool_thread() -> bool:
    """いまのスレッドが、この関数群がツールを走らせている専用スレッドか。"""
    return threading.current_thread().name.startswith(TOOL_THREAD_NAME_PREFIX)


def current_tool_call_abandoned() -> bool:
    """いまのツールのスレッドが、待つ側に見捨てられた (停止で抜けられた) か。"""
    event = _ABANDONED.get()
    return event is not None and event.is_set()


def _abandon_reason(token: Optional[CancellationToken]) -> Optional[str]:
    """待つのをやめるべきなら、その ``interrupted_by`` を返す。続けるなら None。"""
    if token is None or not token.is_cancelled():
        return None
    interrupted_by = token.interrupted_by
    if interrupted_by in ABANDON_INTERRUPTED_BY:
        return interrupted_by
    return None


def _entry_line_depth() -> Optional[int]:
    """呼び出し時点の、共有の PulseContext の line の深さ (無ければ None)。"""
    try:
        from tools.context import get_active_pulse_context
        pulse_ctx = get_active_pulse_context()
    except Exception:  # noqa: BLE001 — 文脈が無い呼び出しでは深さを見ない
        return None
    line_depth = getattr(pulse_ctx, "line_depth", None)
    if not callable(line_depth):
        return None
    return line_depth()


def _child_line_running(entry_depth: Optional[int]) -> bool:
    """このツールが共有の PulseContext に子の line を積んでいる最中か。

    ``run_playbook`` は親と同じ PulseContext に子の line を積んで子を走らせる。
    その最中に親が待つのをやめて抜けると、親の後始末 (``pop_line``) が子の
    line を外し、子のその後の記録が親の line に帰属してしまう。子には停止の
    合図を渡してあるので、子が自分で抜けて line を外すまで待つ。
    """
    if entry_depth is None:
        return False
    try:
        from tools.context import get_active_pulse_context
        pulse_ctx = get_active_pulse_context()
        return pulse_ctx is not None and pulse_ctx.line_depth() > entry_depth
    except Exception:  # noqa: BLE001
        return False


class _AbandonDecider:
    """停止の合図を見て、いま待つのをやめるかを決める (呼び出しごとに一つ)。"""

    def __init__(self, token: Optional[CancellationToken], name: str):
        self._token = token
        self._name = name
        self._entry_depth = _entry_line_depth()
        self._logged_wait = False

    def reason(self) -> Optional[str]:
        reason = _abandon_reason(self._token)
        if reason is None:
            return None
        if _child_line_running(self._entry_depth):
            if not self._logged_wait:
                LOGGER.info(
                    "[tool_wait] Stop requested (interrupted_by=%s) while tool '%s' runs a "
                    "child line; waiting for the child to stop on the same signal",
                    reason, self._name,
                )
                self._logged_wait = True
            return None
        return reason


def _display_name(tool_func: Any, tool_name: Optional[str]) -> str:
    if tool_name:
        return tool_name
    return getattr(tool_func, "__name__", None) or repr(tool_func)


def _raise_abandoned(name: str, interrupted_by: str, *, started: bool) -> None:
    if started:
        LOGGER.warning(
            "[tool_wait] Stopped waiting for tool '%s' (interrupted_by=%s); "
            "its thread may keep running and any later result will be discarded",
            name, interrupted_by,
        )
    else:
        LOGGER.info(
            "[tool_wait] Not starting tool '%s': already stopped (interrupted_by=%s)",
            name, interrupted_by,
        )
    raise ExecutionCancelledException(
        message=f"Stopped while waiting for tool '{name}'",
        interrupted_by=interrupted_by,
    )


def _start_daemon_thread(
    func: Callable[..., Any],
    kwargs: Dict[str, Any],
    name: str,
    on_done: Callable[[bool, Any], None],
    abandoned: threading.Event,
    token: Optional[CancellationToken],
) -> threading.Thread:
    """``func(**kwargs)`` を呼び出し側の contextvars を写した daemon スレッドで走らせる。

    終わったら ``on_done(ok, value)`` を呼ぶ (ok=False のとき value は例外)。
    ``abandoned`` は待つ側が見捨てたときに set する印で、スレッドの文脈に置く。
    """
    ctx = contextvars.copy_context()

    def _call() -> Any:
        _ABANDONED.set(abandoned)
        _CALL_TOKEN.set(token)
        return func(**kwargs)

    def _runner() -> None:
        try:
            value = ctx.run(_call)
        except BaseException as exc:  # noqa: BLE001 — 例外は呼び出し側へ運ぶ
            on_done(False, exc)
        else:
            on_done(True, value)

    thread = threading.Thread(
        target=_runner, name=f"{TOOL_THREAD_NAME_PREFIX}{name}", daemon=True,
    )
    thread.start()
    return thread


async def _await_with_polling(
    awaitable: Any,
    token: Optional[CancellationToken],
    name: str,
) -> Any:
    """awaitable をタスクとして待ち、停止の合図ならタスクを取り消して抜ける。"""
    task = asyncio.ensure_future(awaitable)
    if token is None:
        return await task
    while True:
        done, _pending = await asyncio.wait({task}, timeout=POLL_INTERVAL_SECONDS)
        if done:
            return task.result()
        reason = _abandon_reason(token)
        if reason is not None:
            task.cancel()
            # 取り消したタスクの結果・例外は誰も読まない。読まずに捨てると
            # asyncio が「例外が読まれなかった」と警告するので、ここで読み捨てる。
            task.add_done_callback(_discard_task_outcome)
            _raise_abandoned(name, reason, started=True)


def _discard_task_outcome(task: "asyncio.Future[Any]") -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except BaseException:  # noqa: BLE001
        pass


async def await_tool_call(
    tool_func: Any,
    tool_kwargs: Dict[str, Any],
    *,
    cancellation_token: Optional[CancellationToken],
    tool_name: Optional[str] = None,
) -> Any:
    """ツールを呼び、その結果を待つ (async 版)。

    - 同期関数は専用の daemon スレッドで走らせ、戻り値が awaitable なら
      このイベントループで待つ。
    - コルーチン関数はタスクとして待つ。抜けるときはタスクを取り消す。
    - 正常な結果と、ツールが投げた例外はそのまま返す・投げる。
    - ``cancellation_token`` が None なら抜けない (普通に待つ)。
    - ``tool_func`` が None / 呼べないものなら None を返す
      (``tools.mcp_client.maybe_await_tool_result`` と同じ)。

    Raises:
        ExecutionCancelledException: 印の ``interrupted_by`` が
            ``user_stop`` / ``server_shutdown`` になったとき。
    """
    if tool_func is None or not callable(tool_func):
        return None
    name = _display_name(tool_func, tool_name)
    tool_kwargs = dict(tool_kwargs or {})

    reason = _abandon_reason(cancellation_token)
    if reason is not None:
        _raise_abandoned(name, reason, started=False)

    if inspect.iscoroutinefunction(tool_func):
        return await _await_with_polling(tool_func(**tool_kwargs), cancellation_token, name)

    loop = asyncio.get_running_loop()
    future: "asyncio.Future[Any]" = loop.create_future()

    def _set_outcome(ok: bool, value: Any) -> None:
        if future.done():
            return  # 待つのをやめた後に戻ってきた結果は捨てる
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)

    def _on_done(ok: bool, value: Any) -> None:
        try:
            loop.call_soon_threadsafe(_set_outcome, ok, value)
        except RuntimeError:
            # 待つのをやめた後でループがもう閉じている。結果は捨てる。
            LOGGER.debug(
                "[tool_wait] Discarding late result of tool '%s' (event loop closed)",
                name,
            )

    abandoned = threading.Event()
    decider = _AbandonDecider(cancellation_token, name)
    _start_daemon_thread(
        tool_func, tool_kwargs, name, _on_done, abandoned, cancellation_token,
    )

    if cancellation_token is None:
        result = await future
    else:
        while True:
            done, _pending = await asyncio.wait({future}, timeout=POLL_INTERVAL_SECONDS)
            if done:
                result = future.result()
                break
            reason = decider.reason()
            if reason is not None:
                abandoned.set()
                future.cancel()  # 後で戻る結果は _set_outcome が捨てる
                _raise_abandoned(name, reason, started=True)

    if inspect.isawaitable(result):
        return await _await_with_polling(result, cancellation_token, name)
    return result


def wait_tool_call_sync(
    tool_func: Any,
    tool_kwargs: Dict[str, Any],
    *,
    cancellation_token: Optional[CancellationToken],
    tool_name: Optional[str] = None,
) -> Any:
    """ツールを呼び、その結果を待つ (同期版)。

    ツール関数を専用の daemon スレッドで走らせ、呼び出し元のスレッドで待つ。
    抜ける条件と結果・例外の扱いは :func:`await_tool_call` と同じ。戻り値が
    awaitable でもここでは待たない (同期の呼び出し元は従来どおり受け取る)。
    """
    if tool_func is None or not callable(tool_func):
        return None
    name = _display_name(tool_func, tool_name)
    tool_kwargs = dict(tool_kwargs or {})

    reason = _abandon_reason(cancellation_token)
    if reason is not None:
        _raise_abandoned(name, reason, started=False)

    finished = threading.Event()
    outcome: Dict[str, Any] = {}

    def _on_done(ok: bool, value: Any) -> None:
        outcome["ok"] = ok
        outcome["value"] = value
        finished.set()

    abandoned = threading.Event()
    decider = _AbandonDecider(cancellation_token, name)
    _start_daemon_thread(
        tool_func, tool_kwargs, name, _on_done, abandoned, cancellation_token,
    )

    while not finished.wait(POLL_INTERVAL_SECONDS if cancellation_token is not None else None):
        reason = decider.reason()
        if reason is not None:
            abandoned.set()
            _raise_abandoned(name, reason, started=True)

    if outcome["ok"]:
        return outcome["value"]
    raise outcome["value"]


__all__ = [
    "ABANDON_INTERRUPTED_BY",
    "current_tool_cancellation_token",
    "POLL_INTERVAL_SECONDS",
    "await_tool_call",
    "wait_tool_call_sync",
]
