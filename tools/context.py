from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_PERSONA_ID: ContextVar[Optional[str]] = ContextVar("saiverse_persona_id", default=None)
_PERSONA_PATH: ContextVar[Optional[str]] = ContextVar("saiverse_persona_path", default=None)
_MANAGER: ContextVar[Optional[Any]] = ContextVar("saiverse_manager_ref", default=None)
_PLAYBOOK_NAME: ContextVar[Optional[str]] = ContextVar("saiverse_playbook_name", default=None)
_AUTO_MODE: ContextVar[bool] = ContextVar("saiverse_auto_mode", default=False)
_EVENT_CALLBACK: ContextVar[Optional[Any]] = ContextVar("saiverse_event_callback", default=None)
_MESSAGE_ID: ContextVar[Optional[str]] = ContextVar("saiverse_message_id", default=None)
# Active PulseContext for the running spell/tool. Track-mutating spells
# (track_create / track_activate / track_pause / track_complete / track_abort)
# read this and enqueue their effect onto ``deferred_track_ops`` so the
# operation lands at Pulse completion, not mid-Pulse (Intent A v0.14, Intent B
# v0.11). Tools that don't touch Tracks ignore it.
_PULSE_CONTEXT: ContextVar[Optional[Any]] = ContextVar("saiverse_pulse_context", default=None)
# Snapshot of the currently-running LLM node's messages list. Spell loops set
# this when invoking a spell so spells like ``run_playbook`` can fork a sub-line
# from the parent line's actual conversation context (intent A v0.14 §"子ライン
# は分岐であって独立ではない"). Other spells / tools that don't need parent
# messages can ignore it. Nesting works naturally via context manager — the
# inner persona_context call shadows the outer value and reset() restores it.
_LLM_MESSAGES: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar(
    "saiverse_llm_messages", default=None
)
# Optional override for the MCP instance a tool call should target. When set,
# addon tools that normally resolve their target from persona context (e.g. the
# stackchan vessel dispatcher, which picks the gateway of the building the acting
# persona is in) instead route to this named MCP instance. Used by the addon
# management UI's composite-action *test* execution, which has no persona /
# building context but lets the operator pick which vessel to fire the test at.
# ``None`` (default) preserves the persona-context resolution. Read by addon code
# via :func:`get_tool_target_instance_id`; the value is opaque to the core (it is
# the ``instance_id`` of ``_make_instance_key``, which the stackchan addon fills
# with a vessel_id).
_TOOL_TARGET_INSTANCE_ID: ContextVar[Optional[str]] = ContextVar(
    "saiverse_tool_target_instance_id", default=None
)


def get_active_persona_id() -> Optional[str]:
    return _PERSONA_ID.get()


def get_active_persona_path() -> Optional[Path]:
    value = _PERSONA_PATH.get()
    return Path(value) if value else None


def get_active_manager() -> Optional[Any]:
    return _MANAGER.get()


def get_active_playbook_name() -> Optional[str]:
    return _PLAYBOOK_NAME.get()


def get_auto_mode() -> bool:
    return _AUTO_MODE.get()


def get_event_callback() -> Optional[Any]:
    return _EVENT_CALLBACK.get()


def get_active_message_id() -> Optional[str]:
    return _MESSAGE_ID.get()


def get_active_pulse_context() -> Optional[Any]:
    """Return the active PulseContext for the currently running spell/tool.

    Returns ``None`` outside of a Pulse (e.g. CLI scripts that exercise tools
    directly). Track-mutating spells degrade to immediate execution in that
    case, since there's no Pulse boundary at which to flush.
    """
    return _PULSE_CONTEXT.get()


def get_active_llm_messages() -> Optional[List[Dict[str, Any]]]:
    """Return a snapshot of the calling LLM node's messages list, or None.

    Spell loops populate this just before invoking a spell so the spell can
    inspect the parent line's conversation context. ``run_playbook`` uses it
    to fork its sub-line from the actual parent messages instead of an empty
    list (intent A v0.14 §"子ラインは分岐であって独立ではない"). Spells that
    don't need parent context ignore the return value. Returns None outside
    of a spell-invoking LLM call (tool nodes, CLI runs, etc.).
    """
    return _LLM_MESSAGES.get()


def get_tool_target_instance_id() -> Optional[str]:
    """Return the MCP instance id a tool call should target, or None.

    Addon tools that resolve their target from persona context read this first
    and, when set, route to the named instance instead. Returns None outside of
    an explicit override (= the normal persona-driven path). See
    ``_TOOL_TARGET_INSTANCE_ID`` for the full rationale.
    """
    return _TOOL_TARGET_INSTANCE_ID.get()


def set_tool_target_instance_id(instance_id: Optional[str]) -> None:
    """Set the MCP instance id override for the current context.

    Callers set this inside a task-/context-scoped body (e.g. the coroutine that
    runs a composite action's steps) so the value is captured by the surrounding
    ``copy_context()`` and dies with that context. No explicit reset is needed
    when the setting is confined to a short-lived task; setting it in a
    long-lived context should be paired with a manual reset.
    """
    _TOOL_TARGET_INSTANCE_ID.set(instance_id)


def set_active_message_id(message_id: Optional[str]) -> None:
    """BuildingHistory保存後にmessage_idを確定させるために使用する。

    persona_context の外側から呼べるよう、contextmanager経由ではなく
    直接セットする関数として提供する。ContextVarの性質上、
    同一スレッド/タスク内でのみ有効。
    """
    _MESSAGE_ID.set(message_id)


@contextmanager
def open_persona_memory() -> Iterator[Any]:
    """Yield the acting persona's SAIMemoryAdapter for tool execution.

    Prefers the live adapter already attached to the loaded persona
    (``manager.personas[persona_id].sai_memory``). memory.db は SQLite WAL の
    単一 writer なので、ツール呼び出しごとに新しい接続 (= 新品 adapter) を
    開くと、初期化時の書き込み (DDL/シード/移行) が本体 adapter のトランザク
    ションと衝突して ``database is locked`` の玉突きを起こす (P1: memory 系
    スペルの DB ロック玉突き)。live adapter は close しない (所有権は
    ペルソナ側)。

    Falls back to a fresh temporary adapter only when no live adapter is
    available (persona 未ロード / adapter 未 ready — CLI スクリプトやテスト
    経路)。The temporary adapter is closed when the context exits.

    Note: the yielded adapter's readiness (``is_ready()`` / ``can_embed()``)
    is NOT checked here — callers keep their own checks so per-tool error
    behaviour (raise vs. error string) is preserved.

    共有した live adapter の生 ``conn`` を直接触る場合、呼び出し側は
    ``with adapter._db_lock:`` を保持すること (adapter のメソッド呼び出し
    だけなら内部でロックを取る)。ロックを LLM コール等の長時間処理を跨いで
    保持しないこと。

    Raises:
        RuntimeError: no active persona is set, or the fallback adapter
            could not be constructed.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    personas = getattr(manager, "personas", None) or {}
    persona = personas.get(persona_id)
    live = getattr(persona, "sai_memory", None) if persona is not None else None
    if live is not None and live.is_ready():
        yield live
        return

    # Fallback: build a dedicated adapter and close it on exit. Lazy import
    # keeps this module importable without the memory stack and lets tests
    # patch ``saiverse_memory.SAIMemoryAdapter``.
    from saiverse_memory import SAIMemoryAdapter

    persona_dir = get_active_persona_path()
    try:
        adapter = SAIMemoryAdapter(
            persona_id, persona_dir=persona_dir, resource_id=persona_id
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")
    try:
        yield adapter
    finally:
        adapter.close()


@contextmanager
def persona_context(
    persona_id: str,
    persona_path: Path | str,
    manager: Optional[Any] = None,
    playbook_name: Optional[str] = None,
    auto_mode: bool = False,
    event_callback: Optional[Any] = None,
    message_id: Optional[str] = None,
    pulse_context: Optional[Any] = None,
    llm_messages: Optional[List[Dict[str, Any]]] = None,
) -> Iterator[None]:
    """Temporarily set the active persona identity for tool execution.

    ``llm_messages`` is a snapshot of the caller's LLM messages list; spell
    loops pass it so spells like ``run_playbook`` can inspect the parent
    line's context. Pass None for tool-node / CLI paths that don't need it.
    """
    token_id = _PERSONA_ID.set(persona_id)
    token_path = _PERSONA_PATH.set(str(persona_path))
    token_manager = _MANAGER.set(manager)
    token_playbook = _PLAYBOOK_NAME.set(playbook_name)
    token_auto = _AUTO_MODE.set(auto_mode)
    token_event_cb = _EVENT_CALLBACK.set(event_callback)
    token_msg_id = _MESSAGE_ID.set(message_id)
    token_pulse_ctx = _PULSE_CONTEXT.set(pulse_context)
    token_llm_msgs = _LLM_MESSAGES.set(llm_messages)
    try:
        yield
    finally:
        _PERSONA_ID.reset(token_id)
        _PERSONA_PATH.reset(token_path)
        _MANAGER.reset(token_manager)
        _PLAYBOOK_NAME.reset(token_playbook)
        _AUTO_MODE.reset(token_auto)
        _EVENT_CALLBACK.reset(token_event_cb)
        _MESSAGE_ID.reset(token_msg_id)
        _PULSE_CONTEXT.reset(token_pulse_ctx)
        _LLM_MESSAGES.reset(token_llm_msgs)
