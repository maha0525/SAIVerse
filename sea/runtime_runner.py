from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

from sea.playbook_models import PlaybookSchema

LOGGER = logging.getLogger(__name__)


def _apply_deferred_track_ops(parent_state: Dict[str, Any], persona: Any) -> None:
    """Flush queued Track operations at Pulse-root completion.

    Track-mutating spells (track_create activate=True / track_activate /
    track_pause / track_complete / track_abort) enqueue their effects onto
    PulseContext.deferred_track_ops during the Pulse instead of applying them
    immediately. This runs once the root Playbook returns, so Track switches
    happen at Pulse boundaries and don't bleed into the current Pulse's
    main-cache continuation. (Intent A v0.14, Intent B v0.11)

    Newly running Tracks are picked up by SubLineScheduler on its next tick
    (no immediate kick — keeps the scheduling model in one place).
    """
    pulse_ctx = parent_state.get("_pulse_context") if parent_state else None
    if pulse_ctx is None or not getattr(pulse_ctx, "deferred_track_ops", None):
        return

    manager_ref = getattr(persona, "manager_ref", None)
    track_manager = getattr(manager_ref, "track_manager", None) if manager_ref else None
    if track_manager is None:
        LOGGER.warning(
            "[deferred-track-ops] No TrackManager available on persona=%s — "
            "%d queued op(s) dropped",
            getattr(persona, "persona_id", "?"),
            len(pulse_ctx.deferred_track_ops),
        )
        pulse_ctx.deferred_track_ops.clear()
        return

    op_count = len(pulse_ctx.deferred_track_ops)
    LOGGER.info(
        "[deferred-track-ops] Applying %d op(s) at Pulse-root completion (persona=%s)",
        op_count, getattr(persona, "persona_id", "?"),
    )

    activated_track_id: Optional[str] = None
    for op in pulse_ctx.deferred_track_ops:
        try:
            if op.op_type == "activate":
                track_manager.activate(op.track_id)
                activated_track_id = op.track_id
            elif op.op_type == "pause":
                track_manager.pause(op.track_id)
            elif op.op_type == "complete":
                track_manager.complete(op.track_id)
            elif op.op_type == "abort":
                track_manager.abort(op.track_id)
            else:
                LOGGER.warning(
                    "[deferred-track-ops] Unknown op_type=%s (track_id=%s) — skipped",
                    op.op_type, op.track_id,
                )
                continue
            LOGGER.info(
                "[deferred-track-ops] Applied %s for track_id=%s",
                op.op_type, op.track_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "[deferred-track-ops] Failed to apply %s for track_id=%s: %s",
                op.op_type, op.track_id, exc,
            )

    pulse_ctx.deferred_track_ops.clear()

    if activated_track_id:
        # SubLineScheduler picks this up on its next poll tick. We don't kick
        # immediately — keeping all sub-line Pulse triggering inside one
        # scheduler avoids race conditions with the scheduler's own
        # interval / max-consecutive bookkeeping.
        LOGGER.info(
            "[deferred-track-ops] Track %s is now running; SubLineScheduler "
            "will pick it up on its next tick",
            activated_track_id,
        )


def run_playbook(
    runtime: Any,
    playbook: PlaybookSchema,
    persona: Any,
    building_id: str,
    user_input: Optional[str],
    auto_mode: bool,
    record_history: bool = True,
    parent_state: Optional[Dict[str, Any]] = None,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancellation_token: Optional[Any] = None,
    pulse_type: Optional[str] = None,
    initial_params: Optional[Dict[str, Any]] = None,
    isolate_pulse_context: bool = False,
    line: str = "main",
) -> List[str]:
    if cancellation_token:
        cancellation_token.raise_if_cancelled()

    parent = parent_state or {}

    if initial_params:
        LOGGER.debug("[sea] _run_playbook received args: %s", list(initial_params.keys()))
        # Store args for compile_with_langgraph to resolve via input_schema
        parent["_args"] = dict(initial_params)
    LOGGER.debug("[sea] _run_playbook called for %s, parent_state keys: %s", playbook.name, list(parent.keys()) if parent else "(none)")
    if "_pulse_id" in parent:
        pulse_id = str(parent["_pulse_id"])
    else:
        pulse_id = str(uuid.uuid4())

    parent_chain = parent.get("_playbook_chain", "")
    if parent_chain:
        current_chain = f"{parent_chain} > {playbook.name}"
    else:
        current_chain = playbook.name

    parent["_playbook_chain"] = current_chain

    if cancellation_token:
        parent["_cancellation_token"] = cancellation_token

    def wrapped_event_callback(event: Dict[str, Any]) -> None:
        if event_callback:
            if event.get("type") == "status":
                node = event.get("node", "")
                event["content"] = f"{current_chain} / {node}"
                event["playbook_chain"] = current_chain
            event_callback(event)

    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = playbook.name
        persona.execution_state["node"] = playbook.start_node
        persona.execution_state["status"] = "running"

    # ライン分岐: line="sub" の場合、_prepare_context (SAIMemory 再構築) を bypass し、
    # 親 state["_messages"] のコピーをそのまま base_messages とする。
    # これによりサブラインは「呼び出し時点の親メインラインの会話履歴」を引き継ぐ。
    # See: docs/intent/persona_action_tracks.md (v0.9 サブライン分岐の messages コピー仕様)
    context_warnings: List[Dict[str, Any]] = []
    if line == "sub" and parent.get("_messages") is not None:
        parent_messages = parent.get("_messages") or []
        base_messages = list(parent_messages)  # コピー (参照共有しない)
        LOGGER.info(
            "[sea][run-playbook] %s: line='sub', forking parent _messages (%d messages) instead of "
            "calling _prepare_context. Lightweight model will be used.",
            playbook.name, len(base_messages),
        )
        # サブラインで動かすときは軽量モデルを強制 (LLM ノードの model_type 指定を上書き)
        parent["_force_lightweight_model"] = True
    else:
        LOGGER.info(
            "[sea][run-playbook] %s: calling _prepare_context with history_depth=%s, pulse_id=%s",
            playbook.name,
            playbook.context_requirements.history_depth if playbook.context_requirements else "None",
            pulse_id,
        )
        base_messages = runtime._prepare_context(
            persona,
            building_id,
            user_input,
            playbook.context_requirements,
            pulse_id=pulse_id,
            warnings=context_warnings,
            event_callback=wrapped_event_callback,
            cancellation_token=cancellation_token,
        )
        LOGGER.info("[sea][run-playbook] %s: _prepare_context returned %d messages", playbook.name, len(base_messages))
    conversation_msgs = list(base_messages)

    for warn in context_warnings:
        if event_callback:
            wrapped_event_callback(warn)

    compiled_ok = runtime._compile_with_langgraph(
        playbook,
        persona,
        building_id,
        user_input,
        auto_mode,
        conversation_msgs,
        pulse_id,
        parent_state=parent,
        event_callback=wrapped_event_callback,
        cancellation_token=cancellation_token,
        pulse_type=pulse_type,
        isolate_pulse_context=isolate_pulse_context,
    )
    if compiled_ok is None:
        LOGGER.error(
            "LangGraph compilation failed for playbook '%s'. This indicates a configuration or dependency issue.",
            playbook.name,
        )
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        return []

    # NOTE: deferred Track ops are flushed inside ``compile_with_langgraph``'s
    # finally block (sea/runtime_graph.py) where the PulseContext actually
    # lives. Calling _apply_deferred_track_ops here would no-op because
    # `parent` doesn't carry _pulse_context across LangGraph state boundaries.

    return compiled_ok
