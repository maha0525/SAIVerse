from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from llm_clients.exceptions import LLMError
from sea.cancellation import CancellationToken, ExecutionCancelledException
from sea.langgraph_runner import compile_playbook
from sea.playbook_models import PlaybookSchema

LOGGER = logging.getLogger(__name__)

def compile_with_langgraph(
    runtime,
    playbook: PlaybookSchema,
    persona: Any,
    building_id: str,
    user_input: Optional[str],
    auto_mode: bool,
    base_messages: List[Dict[str, Any]],
    pulse_id: str,
    parent_state: Optional[Dict[str, Any]] = None,
    event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    cancellation_token: Optional[CancellationToken] = None,
    pulse_type: Optional[str] = None,
    isolate_pulse_context: bool = False,
    pulse_line_role: Optional[str] = None,
    pulse_line_track_id: Optional[str] = None,
) -> Optional[List[str]]:
    _lg_outputs: List[str] = []
    temperature = runtime._default_temperature(persona)
    parent = parent_state or {}

    # Update execution state: playbook started (LangGraph path)
    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = playbook.name
        persona.execution_state["node"] = playbook.start_node
        persona.execution_state["status"] = "running"

    compiled = compile_playbook(
        playbook,
        llm_node_factory=lambda node_def: runtime._lg_llm_node(node_def, persona, building_id, playbook, event_callback),
        tool_node_factory=lambda node_def: runtime._lg_tool_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
        tool_call_node_factory=lambda node_def: runtime._lg_tool_call_node(node_def, persona, playbook, event_callback, auto_mode=auto_mode),
        speak_node=lambda state: runtime._lg_speak_node(state, persona, building_id, playbook, _lg_outputs, event_callback),
        think_node=lambda state: runtime._lg_think_node(state, persona, playbook, _lg_outputs, event_callback),
        say_node_factory=lambda node_def: runtime._lg_say_node(node_def, persona, building_id, playbook, _lg_outputs, event_callback),
        memorize_node_factory=lambda node_def: runtime._lg_memorize_node(node_def, persona, playbook, _lg_outputs, event_callback),
        exec_node_factory=lambda node_def: runtime._lg_exec_node(node_def, playbook, persona, building_id, auto_mode, _lg_outputs, event_callback),
        subplay_node_factory=lambda node_def: runtime._lg_subplay_node(node_def, persona, building_id, playbook, auto_mode, _lg_outputs, event_callback),
        set_node_factory=lambda node_def: runtime._lg_set_node(node_def, playbook, event_callback),
        stelis_start_node_factory=lambda node_def: runtime._lg_stelis_start_node(node_def, persona, playbook, event_callback),
        stelis_end_node_factory=lambda node_def: runtime._lg_stelis_end_node(node_def, persona, playbook, event_callback),
    )
    if not compiled:
        # Update execution state: compilation failed, reset to idle
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        raise LLMError(
            f"Playbook '{playbook.name}' graph compilation failed",
            user_message=f"プレイブック '{playbook.name}' のグラフ構築に失敗しました。",
        )

    # Resolve input_schema parameters from _args (function call model).
    # _args is set by the caller (run_playbook, or exec/subplay node args).
    args_dict = parent.get("_args") or {}
    inherited_vars = {}
    for param in playbook.input_schema:
        param_name = param.name

        if param_name in args_dict:
            value = args_dict[param_name]
            LOGGER.debug("[sea][LangGraph] Resolved %s from args: %s", param_name, str(value)[:120] if value else "(empty)")
        else:
            value = param.default if param.default is not None else ""

        inherited_vars[param_name] = value

    # Inherit _pulse_usage_accumulator from parent_state if it exists (for sub-playbook calls)
    # This ensures usage is accumulated across all LLM calls in the entire pulse chain
    parent_accumulator = parent.get("_pulse_usage_accumulator")
    if parent_accumulator:
        # Use the same accumulator (reference) to accumulate across sub-playbooks
        usage_accumulator = parent_accumulator
    else:
        # Create new accumulator for this pulse
        usage_accumulator = {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cached_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_cost_usd": 0.0,
            "call_count": 0,
            "models_used": [],
        }

    # Inherit activity trace list (shared reference, same pattern as accumulator)
    parent_activity_trace = parent.get("_activity_trace")
    if parent_activity_trace is not None:
        activity_trace = parent_activity_trace
    else:
        activity_trace = []

    # Inherit cancellation token from parent state if not explicitly provided
    effective_cancellation_token = cancellation_token or parent.get("_cancellation_token")

    # Inherit PulseContext from parent_state (shared reference, same pattern as accumulator).
    # Subagent executions set isolate_pulse_context=True to get their own PulseContext,
    # preventing intermediate messages from leaking into the parent's protocol chain.
    parent_pulse_ctx = parent.get("_pulse_context") if not isolate_pulse_context else None
    if parent_pulse_ctx is not None:
        pulse_ctx = parent_pulse_ctx  # Share reference across sub-playbooks
    elif isolate_pulse_context:
        # Create a fresh, empty PulseContext (bypasses the cache so prior entries
        # such as router I/O are not visible to this sub-playbook).
        from sea.pulse_context import PulseContext
        _adapter = getattr(persona, "sai_memory", None)
        _thread_id = _adapter.get_current_thread() if _adapter else None
        pulse_ctx = PulseContext(pulse_id=pulse_id, thread_id=_thread_id or "")
    else:
        # Create new PulseContext for this pulse (or get existing one from cache)
        _adapter = getattr(persona, "sai_memory", None)
        _thread_id = _adapter.get_current_thread() if _adapter else None
        pulse_ctx = runtime._get_or_create_pulse_context(pulse_id, _thread_id or "")

    # Pulse-root only: push the entry-line frame onto the line stack so messages
    # produced during this Pulse get the right line_role / line_id / origin_track_id
    # in their SAIMemory metadata (Intent A v0.14, Intent B v0.11). The pop runs in
    # the finally block alongside _flush_pulse_logs.
    _pushed_root_line = False
    if parent_pulse_ctx is None and pulse_line_role:
        pulse_ctx.push_line(role=pulse_line_role, track_id=pulse_line_track_id)
        _pushed_root_line = True
        LOGGER.debug(
            "[runtime_graph] Pushed Pulse-root line: role=%s track_id=%s pulse_id=%s",
            pulse_line_role, pulse_line_track_id, pulse_id,
        )

    # Check spell toggle for this persona
    _spell_enabled = runtime._is_spell_enabled_for_persona(persona)

    initial_state = {
        # System variables (_ prefix, auto-inherited, nodes don't touch)
        "_messages": list(base_messages),
        "_context": {},
        "_outputs": _lg_outputs,
        "_persona_obj": persona,
        "_pulse_id": pulse_id,
        "_pulse_type": pulse_type,  # user/schedule/auto
        "_cancellation_token": effective_cancellation_token,  # For node-level cancellation checks
        "_pulse_usage_accumulator": usage_accumulator,  # Inherit from parent or create new
        "_activity_trace": activity_trace,  # Shared trace of exec/tool activities
        "_pulse_context": pulse_ctx,  # Pulse-level log context (replaces _intermediate_msgs)
        "_spell_enabled": _spell_enabled,  # Per-persona spell system toggle
        # ライン強制フラグ (parent から継承、Phase C-2a)。
        # サブライン子 Playbook は run_playbook で _force_lightweight_model を立てる。
        # 子の中の LLM ノードがこれを見て軽量モデルを選ぶ。
        "_force_lightweight_model": parent.get("_force_lightweight_model", False),
        # Playbook variables (no prefix)
        "last": user_input or "",
        "input": user_input or "",
        **inherited_vars,  # Add resolved parameters from args/input_schema
    }

    # Execute compiled playbook
    # Set recursion limit high enough for agentic loops (default is 25, too low for multi-step agents)
    langgraph_config = {"recursion_limit": 1000}

    final_state = None
    try:
        # Check cancellation before starting execution
        if cancellation_token:
            cancellation_token.raise_if_cancelled()

        # Check if we're inside an existing event loop
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop.is_running():
            # We're inside an existing loop (e.g., Gradio), use run_in_executor
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, compiled(initial_state, langgraph_config))
                final_state = future.result()
        else:
            # No running loop, use asyncio.run directly
            final_state = asyncio.run(compiled(initial_state, langgraph_config))
    except ExecutionCancelledException:
        # Re-raise cancellation exceptions
        raise
    except LLMError:
        # Re-raise LLM errors for proper error handling in caller
        # Update execution state: execution failed, reset to idle
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        raise
    except Exception as exc:
        LOGGER.exception("SEA LangGraph execution failed")
        # Update execution state: execution failed, reset to idle
        if hasattr(persona, "execution_state"):
            persona.execution_state["playbook"] = None
            persona.execution_state["node"] = None
            persona.execution_state["status"] = "idle"
        # Wrap as LLMError so existing error propagation chain
        # delivers it to the frontend instead of silently swallowing.
        raise LLMError(
            f"Playbook execution failed: {type(exc).__name__}: {exc}",
            original_error=exc,
            user_message=f"プレイブックの実行中にエラーが発生しました: {exc}",
        ) from exc
    finally:
        # Pop the Pulse-root line frame we pushed before LangGraph execution.
        # Done before flush so the PulseContext.deferred_track_ops apply step
        # below sees a clean stack — though current ops don't read line state
        # at apply time, keeping this order matches a reader's expectation
        # ("the Pulse is done, the line is closed, then we settle the books").
        if _pushed_root_line:
            try:
                pulse_ctx.pop_line()
            except Exception:
                LOGGER.exception(
                    "[runtime_graph] Failed to pop Pulse-root line for pulse_id=%s",
                    pulse_id,
                )

        # Flush PulseContext to DB if this is the top-level playbook (not a sub-playbook).
        # Using finally ensures logs are preserved even when LLM errors or other
        # exceptions abort execution — otherwise all accumulated entries are lost.
        if parent.get("_pulse_context") is None:
            # Prefer final_state (has the most up-to-date context), fall back to
            # initial_state (still has the same PulseContext reference from setup).
            _source_state = final_state if isinstance(final_state, dict) else initial_state
            _final_pulse_ctx = _source_state.get("_pulse_context")
            if _final_pulse_ctx:
                runtime._flush_pulse_logs(persona, _final_pulse_ctx)
                # Apply deferred Track operations queued by spells during this Pulse.
                # Same pulse-root condition as _flush_pulse_logs above — Track switches
                # land at Pulse boundaries (Intent A v0.14, Intent B v0.11). Done here
                # (rather than in runtime_runner.run_playbook) because the
                # PulseContext only lives in LangGraph state, not in `parent`.
                try:
                    from sea.runtime_runner import _apply_deferred_track_ops
                    _apply_deferred_track_ops(
                        {"_pulse_context": _final_pulse_ctx},
                        persona,
                    )
                except Exception:
                    LOGGER.exception(
                        "[runtime_graph] Failed to apply deferred Track ops at Pulse-root completion"
                    )

                # Flush meta judgment buffer to meta_judgment_log table
                # (Phase 2 / handoff Part 2). pulse_type == 'meta_judgment' の Pulse
                # でのみ buffer が populate されている。MetaLayer 経由で書き込み、
                # 次回メタ判断時の judge プロンプトに動的注入される。
                # Track 切替系 spell が発動した場合は committed_to_main_cache=True。
                _meta_buffer = getattr(_final_pulse_ctx, "meta_judgment_buffer", None)
                if _meta_buffer is not None:
                    try:
                        meta_layer = getattr(runtime.manager, "meta_layer", None)
                        if meta_layer is not None:
                            # trigger_type / trigger_context / track_at_judgment_id を
                            # state から抽出。MetaLayer の入口で initial_params 経由で
                            # state に乗せている。trigger_type は trigger_context JSON
                            # の "trigger" キーから取り出す。
                            import json as _json
                            _trigger_context_str = _source_state.get("trigger_context") or ""
                            _trigger_type = "unknown"
                            try:
                                _ctx_obj = _json.loads(_trigger_context_str) if _trigger_context_str else {}
                                if isinstance(_ctx_obj, dict):
                                    _trigger_type = str(_ctx_obj.get("trigger") or "unknown")
                            except (ValueError, TypeError):
                                pass
                            _alert_track_id = _source_state.get("alert_track_id") or None
                            _committed = any(
                                op.op_type == "activate"
                                for op in _final_pulse_ctx.deferred_track_ops
                            )
                            meta_layer._record_judgment_log(
                                persona_id=getattr(persona, "persona_id", None),
                                trigger_type=_trigger_type,
                                trigger_context=_trigger_context_str or None,
                                track_at_judgment_id=_alert_track_id,
                                thought_parts=_meta_buffer.get("thought_parts") or [],
                                spells=_meta_buffer.get("spells") or [],
                                committed_to_main_cache=_committed,
                                prompt_snapshot=None,
                            )
                    except Exception:
                        LOGGER.exception(
                            "[runtime_graph] Failed to flush meta_judgment buffer to log"
                        )

    # Write back state variables to parent_state based on output_schema
    if parent_state is not None and isinstance(final_state, dict) and playbook.output_schema:
        for key in playbook.output_schema:
            if key in final_state:
                value = final_state[key]
                # Use _store_structured_result to also create flattened dot-notation keys
                # (e.g., research_result.summary, research_result.status)
                if isinstance(value, dict):
                    runtime._store_structured_result(parent_state, key, value)
                else:
                    parent_state[key] = value
                LOGGER.debug("[sea][LangGraph] Propagated %s to parent_state: %s", key, str(value))

    # Update execution state: playbook completed (LangGraph path)
    if hasattr(persona, "execution_state"):
        persona.execution_state["playbook"] = None
        persona.execution_state["node"] = None
        persona.execution_state["status"] = "idle"

    # speak/think nodes already emitted; return collected texts for UI consistency
    return list(_lg_outputs)
