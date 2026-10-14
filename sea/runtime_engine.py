from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from saiverse.logging_config import log_sea_trace
from sea.message_stamp import (
    attach_stored_id_to_unstored_reply,
    stamp_generation_metadata,
    unstored_reply_for_last,
)
from sea.playbook_models import PlaybookSchema
from sea.runtime_emitters import SPEAK_MEMORY_MESSAGE_ID_KEY
from sea.runtime_state import effective_auto_mode, set_playbook_var
from sea.runtime_utils import _format, _resolve_template_arg, event_building_id
from tools import TOOL_REGISTRY, canonicalize_tool_name
from tools.context import persona_context
from tools.core import parse_tool_result
from tools.mcp_client import maybe_await_tool_result

LOGGER = logging.getLogger(__name__)

StateNode = Callable[[dict], Awaitable[dict]]
EventCallback = Optional[Callable[[Dict[str, Any]], None]]


class RuntimeEngine:
    def __init__(self, runtime: Any, manager_ref: Any, llm_selector: Callable[..., Any], emitters: Dict[str, Callable[..., Any]]) -> None:
        self.runtime = runtime
        self.manager = manager_ref
        self.llm_selector = llm_selector
        self.emitters = emitters

    def lg_tool_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, event_callback: EventCallback = None, auto_mode: bool = False) -> StateNode:
        from pathlib import Path

        tool_name = node_def.action
        args_input = getattr(node_def, "args_input", None)
        output_key = getattr(node_def, "output_key", None)
        output_keys = getattr(node_def, "output_keys", None)

        async def node(state: dict) -> dict:
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()
            node_id = getattr(node_def, "id", "tool")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # アドオン由来のツールは <addon>__<name> キーで登録されるため、
            # Playbook 内の素名参照を一意なら名前空間キーへ解決する。
            tool_key = canonicalize_tool_name(tool_name)
            tool_func = TOOL_REGISTRY.get(tool_key)
            persona_obj = state.get("_persona_obj") or persona
            persona_id = getattr(persona_obj, "persona_id", "unknown")

            try:
                persona_dir = getattr(persona_obj, "persona_log_path", None)
                persona_dir = persona_dir.parent if persona_dir else Path.cwd()
                manager_ref = getattr(persona_obj, "manager_ref", None)

                # Build kwargs from args_input (None or {} = no args)
                # Supports nested keys via dot notation (e.g., "tool_call.args.playbook_name")
                # String values are resolved as state variable paths.
                # To pass a literal string, use {"$literal": "value"} syntax.
                kwargs: Dict[str, Any] = {}
                if args_input:
                    for arg_name, source in args_input.items():
                        if isinstance(source, dict) and "$literal" in source:
                            value = source["$literal"]
                            LOGGER.debug("[sea][tool] Using $literal arg '%s' = %s", arg_name, value)
                        elif isinstance(source, str):
                            value = self.runtime._resolve_state_value(state, source)
                            LOGGER.debug("[sea][tool] Mapping arg '%s' <- state['%s'] = %s", arg_name, source, value)
                        else:
                            value = source
                            LOGGER.debug("[sea][tool] Using literal arg '%s' = %s", arg_name, value)
                        kwargs[arg_name] = value

                # ===== Tool execution logging (centralized) =====
                LOGGER.info("[sea][tool] CALL %s (persona=%s) args=%s", tool_name, persona_id, kwargs)

                if tool_func is None:
                    # 未登録のツール名は失敗として下の except へ送る。maybe_await_tool_result は
                    # None を渡されると例外を出さずに None を返すので、ここで止めないと
                    # 「None という結果の成功」が state["last"] / PulseContext / _messages に
                    # 記録される (runtime_nodes.py の tool_call ノードと同じ扱いに揃える)。
                    # 削除済みのツール (例: control_body) を指す上書き版の playbook がこれを踏む。
                    raise LookupError(f"Tool '{tool_name}' not found in registry")
                LOGGER.info("[sea][tool] Tool function found: %s", tool_func)

                # Execute tool with persona context
                # 直前の LLM speak ノードが保存した message_id を後続ツールにも
                # 引き継ぐ。persona_context は毎回 _MESSAGE_ID を強制的に書き換える
                # ため、state 経由で明示的に渡さないとアドオン連携が切れる。
                _current_msg_id = state.get("_last_message_id")
                # PulseContext も同様に state 経由で受け渡す。track_* 系スペル等が
                # 「Pulse 完了時に deferred 適用」するために必須 (Intent A v0.14)。
                _pulse_ctx = state.get("_pulse_context")
                # auto_mode は factory が capture した引数でなく state の実効値
                # (親 Pulse からの継承込み) を読む。
                _eff_auto_mode = effective_auto_mode(state, auto_mode)
                if persona_id and persona_dir:
                    with persona_context(persona_id, persona_dir, manager_ref, playbook_name=playbook.name, auto_mode=_eff_auto_mode, event_callback=event_callback, message_id=_current_msg_id, pulse_context=_pulse_ctx):
                        result = await maybe_await_tool_result(tool_func, **kwargs)
                else:
                    result = await maybe_await_tool_result(tool_func, **kwargs)

                # A native tool returning a tuple ``(content, snippet, file_path,
                # metadata)`` (or the 2-/3-tuple forms) must NOT leak its raw
                # repr into the trace, PulseContext, SAIMemory, or the LLM
                # message — only its text belongs there. Normalize tuples via the
                # canonical parser (the raw ``result`` is still used below for
                # output_keys / output_key, the explicit multi-value contract).
                # Non-tuple returns (str / dict / ToolResult) keep str() so a
                # bare dict is not lossily collapsed to "".
                # See docs/issues/native_tool_return_4tuple_bug.md.
                result_str = parse_tool_result(result)[0] if isinstance(result, tuple) else str(result)
                result_preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                LOGGER.info("[sea][tool] RESULT %s -> %s", tool_name, result_preview)
                log_sea_trace(playbook.name, node_id, "TOOL", f"action={tool_name} → {result_str}")

                # Activity trace: record tool execution (skip infrastructure playbooks)
                if not playbook.name.startswith(("meta_", "sub_")):
                    pb_display = playbook.display_name or playbook.name
                    _at = state.get("_activity_trace")
                    if isinstance(_at, list):
                        _at.append({"action": "tool", "name": tool_name, "playbook": pb_display})
                    if event_callback:
                        event_callback({
                            "type": "activity", "action": "tool", "name": tool_name,
                            "playbook": pb_display, "status": "completed",
                            # 名乗りと部屋は同じ persona_obj から引く — これは
                            # ツールを実際に走らせた本人 (上の persona_context と
                            # 同じ) で、「誰の吹き出しか」と「その人がいまいる
                            # 部屋」が一致する。state["_persona_obj"] を書くのは
                            # compile_with_langgraph の 1 箇所だけで、そこは
                            # ノードを作るときと同じ persona を入れる
                            # (sea/runtime_graph.py の initial_state)。置かない
                            # 経路 (sea/work_session.py) では or で persona に
                            # 倒れるので、二つが別物になることはない。
                            "persona_id": getattr(persona_obj, "persona_id", None),
                            "persona_name": getattr(persona_obj, "persona_name", None),
                            "pulse_id": state.get("_pulse_id"),
                            # 発火時点の部屋。名乗らないと、別の部屋で進んでいる
                            # Beat の活動記録が閲覧中の部屋の吹き出しに混ざる。
                            "building_id": event_building_id(
                                self.runtime, persona_obj,
                            ),
                        })

                # Handle tuple results with output_keys (for multi-value returns)
                if output_keys and isinstance(result, tuple):
                    # Expand tuple to multiple state variables
                    for i, key in enumerate(output_keys):
                        if i < len(result):
                            if set_playbook_var(state, key, result[i], where=f"node '{node_id}' output_keys[{i}]"):
                                LOGGER.debug("[sea][LangGraph] Stored tuple[%d] in state[%s]: %s", i, key, str(result[i]))
                    # Set last to first element (primary result)
                    state["last"] = str(result[0]) if result else ""
                elif isinstance(result, tuple):
                    # Legacy: extract first element
                    state["last"] = str(result[0]) if result else ""
                else:
                    state["last"] = str(result)

                # Store result in state if output_key is specified (legacy single-value)
                if output_key and not output_keys:
                    set_playbook_var(state, output_key, result, where=f"node '{node_id}' output_key")

                # Append to PulseContext
                _pulse_ctx = state.get("_pulse_context")
                if _pulse_ctx:
                    from sea.pulse_context import PulseLogEntry
                    _pulse_ctx.append(PulseLogEntry(
                        role="tool", content=result_str,
                        node_id=node_id, playbook_name=playbook.name,
                        tool_name=tool_name,
                        important=getattr(node_def, "important", False) or False))

                # Append to in-memory messages so subsequent LLM nodes in the
                # same Pulse can see the tool result. PulseContext alone is not
                # enough because LLM nodes that read state["_messages"] directly
                # (= the path that becomes the only path once context_profile is
                # removed in Phase 3 段階 4-D) would otherwise miss the result.
                # Pattern matches lg_subplay_node's report_to_parent fix
                # (sea/runtime_nodes.py:229-263, まはー指摘 2026-04-28). Wrapped
                # as user role + <system> tag for cross-provider compatibility
                # (Gemini rejects mid-stream system role; see sea/runtime_llm.py
                # の同一書式コメント).
                self._append_tool_message(
                    state, persona_obj, playbook,
                    f"tool '{tool_name}' result:\n{result_str}",
                )
            except Exception as exc:
                error_msg = f"Tool error: {exc}"
                state["last"] = error_msg
                # Populate output_keys so downstream nodes can resolve template variables
                if output_keys:
                    for i, key in enumerate(output_keys):
                        set_playbook_var(state, key, None, where=f"node '{node_id}' output_keys[{i}]")
                    set_playbook_var(state, output_keys[0], error_msg, where=f"node '{node_id}' output_keys[0]")
                if output_key and not output_keys:
                    set_playbook_var(state, output_key, error_msg, where=f"node '{node_id}' output_key")
                LOGGER.exception("SEA LangGraph tool %s failed", tool_name)
                # Append error to PulseContext
                _pulse_ctx = state.get("_pulse_context")
                if _pulse_ctx:
                    from sea.pulse_context import PulseLogEntry
                    _pulse_ctx.append(PulseLogEntry(
                        role="tool", content=f"Tool error: {exc}",
                        node_id=node_id, playbook_name=playbook.name,
                        tool_name=tool_name))
                # Also surface the error to in-memory messages so subsequent
                # LLM nodes can react (e.g. retry / abort / report). Same
                # rationale as the success path.
                self._append_tool_message(
                    state, persona_obj, playbook,
                    f"tool '{tool_name}' error:\n{error_msg}",
                )
            return state

        return node

    def _append_tool_message(
        self, state: dict, persona: Any, playbook: PlaybookSchema, body: str,
    ) -> None:
        """TOOL ノードの結果 (またはエラー) を送る列 ``state["_messages"]`` へ足す。

        既定は今までどおり、記録に残さない ``<system>…</system>`` の user
        メッセージ。リアルタイム情報と自動想起を残すモデルの、残すパルスの列で、
        ターンの最初の LLM 呼び出しをまだ送っていない間
        (:func:`sea.runtime_llm.records_additions_before_first_send`) は、事前
        スペルの結果 (sea/runtime_llm._execute_pre_spells) と同じ規則で足す —
        system ロールの記録に残し、その id を持たせ、記録が次のターンに復元
        されるのと同じ文面 (``presented_system_record_content``) で送る。記録に
        残さないと、最初の呼び出しで送ったこの結果の位置が次のターンでは返事に
        置き換わり、前方一致が割れる (docs/intent/persist_realtime_and_recall.md
        不変条件 1、Codex 五巡目 #2)。最初の送信の直前に送る列の後ろ側は記録から
        読み直した並びに揃えられる (sea/runtime_llm._align_first_send_with_record
        — ツールの中で書かれた記録もそこで並ぶ) が、記録に残らない結果は読み直しに
        出ないので、揃える前提としてもここで記録に残す必要がある。記録に失敗したら
        同じ文面で id 無しのまま足す (揃えた後の検査が WARN で知らせる)。
        """
        if state.get("_messages") is None:
            state["_messages"] = []
        from sea.runtime_llm import records_additions_before_first_send

        if not records_additions_before_first_send(state):
            state["_messages"].append({"role": "user", "content": f"<system>{body}</system>"})
            return
        from sai_memory.memory.storage import presented_system_record_content

        msg: Dict[str, Any] = {
            "role": "user", "content": presented_system_record_content(body),
        }
        try:
            stored_id = self.runtime._store_memory(
                persona, body, role="system",
                tags=["conversation", "tool"],
                pulse_id=state.get("_pulse_id"),
                playbook_name=playbook.name,
                pulse_context=state.get("_pulse_context"),
                return_message_id=True,
            )
            if stored_id:
                msg["id"] = str(stored_id)
        except Exception:
            LOGGER.exception(
                "[sea][tool] failed to keep the tool message before the first LLM call",
            )
        state["_messages"].append(msg)

    def lg_exec_node(
        self,
        node_def: Any,
        playbook: PlaybookSchema,
        persona: Any,
        building_id: str,
        auto_mode: bool,
        outputs: Optional[List[str]] = None,
        event_callback: EventCallback = None,
    ) -> StateNode:
        # Get source variable names from node definition (with defaults for backward compatibility)
        playbook_source = getattr(node_def, "playbook_source", "selected_playbook") or "selected_playbook"
        args_source = getattr(node_def, "args_source", "selected_args") or "selected_args"

        async def node(state: dict) -> dict:
            # Check for cancellation at start of node
            cancellation_token = state.get("_cancellation_token")
            if cancellation_token:
                cancellation_token.raise_if_cancelled()

            # Send status event for node execution
            node_id = getattr(node_def, "id", "exec")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # auto_mode は factory が capture した引数でなく state の実効値
            # (親 Pulse からの継承込み) を読む。以降の許可判定と子 Playbook への
            # 引き渡しは全てこの値を使う。
            eff_auto_mode = effective_auto_mode(state, auto_mode)
            selected_playbook = state.get(playbook_source)
            sub_name = selected_playbook or state.get("last") or ""
            clean_name = str(sub_name).strip()

            sub_pb = self.runtime._load_playbook_for(clean_name, persona, building_id) if clean_name else None
            if sub_pb is None:
                error_msg = f"Sub-playbook not found: {clean_name or '<empty>'}"
                state["last"] = error_msg
                state["_exec_error"] = True
                state["_exec_error_detail"] = error_msg
                if outputs is not None:
                    outputs.append(error_msg)
                return state

            # Build child args: static args (template) + dynamic args_source (overrides)
            child_args = {}
            # 1. Resolve static args from node_def (template strings like "{objective}")
            static_args = getattr(node_def, "args", None)
            if static_args and isinstance(static_args, dict):
                state_vars = {k: v for k, v in state.items() if not k.startswith("_")}
                for key, tmpl in static_args.items():
                    if isinstance(tmpl, str):
                        child_args[key] = _resolve_template_arg(tmpl, state_vars)
                    else:
                        child_args[key] = tmpl
            # 2. Merge dynamic args from args_source (takes precedence)
            dynamic_args = state.get(args_source) or {}
            if isinstance(dynamic_args, dict):
                child_args.update(dynamic_args)

            # Determine sub_input for context preparation
            sub_input = child_args.get("input") or child_args.get("query")
            if not sub_input:
                sub_input = state.get("input")

            # Set _args on parent state so compile_with_langgraph can resolve them
            state["_args"] = child_args if child_args else None

            eff_bid = self.runtime._effective_building_id(persona, building_id)

            # ── Playbook permission check ──
            # 判定は runtime.decide_playbook_permission に集約 (同じ規則を
            # /run_playbook スペル側と共有する)。ここが持つのは結果の見せ方だけ。
            #
            # ``user_configured=False`` 固定: EXEC が起こす Playbook 名は state の
            # ``selected_playbook`` から来る (router LLM の選択か、呼び出し側が
            # 積んだ値)。ユーザーが設定画面で名指しした起動は pre_spells 経路
            # (``/run_playbook``) を通るので、そちらだけが事前承認を名乗る。
            city_id = getattr(self.manager, "city_id", None)
            allowed, denial_msg = self.runtime.decide_playbook_permission(
                city_id, clean_name, persona,
                user_configured=False,
                auto_mode=eff_auto_mode,
                event_callback=event_callback,
            )
            log_sea_trace(
                playbook.name, node_id, "PERM",
                f"{clean_name} → {'allow' if allowed else 'deny'}",
            )
            if not allowed:
                self.runtime._notify_persona_permission_result(
                    state, persona, clean_name, denial_msg, event_callback,
                )
                return state

            # Determine execution mode
            execution = getattr(node_def, "execution", "inline") or "inline"
            subagent_thread_id = None
            subagent_parent_id = None

            if execution == "subagent":
                label = f"Subagent: {sub_name}"
                subagent_thread_id, subagent_parent_id = self.runtime._start_subagent_thread(
                    persona, label=label, pulse_context=state.get("_pulse_context"),
                )
                if not subagent_thread_id:
                    LOGGER.warning("[sea][exec] Failed to start subagent thread for '%s', falling back to inline", sub_name)
                    execution = "inline"  # Fallback
                else:
                    log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} [subagent thread={subagent_thread_id}] (input=\"{str(sub_input)}\")")

            if execution == "inline":
                log_sea_trace(playbook.name, node_id, "EXEC", f"→ {sub_name} (input=\"{str(sub_input)}\")")

            try:
                cancellation_token = state.get("_cancellation_token")
                sub_outputs = await asyncio.to_thread(
                    self.runtime._run_playbook, sub_pb, persona, eff_bid, sub_input, eff_auto_mode, True, state, event_callback,
                    cancellation_token=cancellation_token,
                    isolate_pulse_context=(execution == "subagent"),
                )
            except Exception as exc:
                LOGGER.exception("SEA LangGraph exec sub-playbook failed")
                # 書き込み時の機械刻印 (sea/message_stamp.py): 子は親の state を
                # そのまま受け取って走るので、子の _prepare_context が子の
                # `_presented_message_ids` で親の材料を上書きしている
                # (sea/runtime_runner.py)。途中まで走った子の三つ組・前駆を親の
                # 次の刻印へ持ち越さない — compile_playbook がこのノードの後で消す
                # (sea/message_stamp.drop_stamp_material)。
                # End subagent thread on error (no chronicle)
                if execution == "subagent" and subagent_thread_id:
                    self.runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=False, pulse_context=state.get("_pulse_context"))
                error_msg = f"Sub-playbook error: {type(exc).__name__}: {exc}"
                state["last"] = error_msg
                state["_exec_error"] = True
                state["_exec_error_detail"] = error_msg
                log_sea_trace(playbook.name, node_id, "EXEC", f"→ {error_msg}")
                if event_callback:
                    event_callback({
                        "type": "error",
                        "content": f"[{sub_name}] {type(exc).__name__}: {exc}",
                        "playbook": playbook.name,
                        "node": node_id,
                    })
                # Record error to SAIMemory so the persona (and subsequent LLM calls) can see it
                if not self.runtime._store_memory(
                    persona, error_msg,
                    role="system",
                    tags=["error", "exec", str(sub_name).strip()],
                    pulse_id=state.get("_pulse_id"),
                ):
                    LOGGER.warning("Failed to store exec error to SAIMemory for node %s", node_id)
                    if event_callback:
                        event_callback({
                            "type": "warning",
                            "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。",
                            "warning_code": "memorize_failed",
                            "display": "toast",
                        })
                # Append error to PulseContext
                _pulse_ctx = state.get("_pulse_context")
                if _pulse_ctx:
                    from sea.pulse_context import PulseLogEntry
                    _pulse_ctx.append(PulseLogEntry(
                        role="system", content=error_msg,
                        node_id=node_id, playbook_name=playbook.name))
                if outputs is not None:
                    outputs.append(error_msg)
                return state

            # 書き込み時の機械刻印: この後 state["last"] に入るのは子の本文 (親の
            # SPEAK / MEMORIZE が刻むと、他人のコールの数字・子の
            # `_presented_message_ids` が載る)。しかも子は親の state を受け取って
            # 走るので、親の材料は子の `_presented_message_ids` で上書き済み —
            # 残すと親の次の返事の前駆が、親の見ていない子の行を指しうる。
            # compile_playbook がこのノードの後で (エラー文で早期 return した回も)
            # 材料を消す (sea/message_stamp.drop_stamp_material)。親が次に自前の
            # LLM ノードで送るときに、送る `messages` から作り直される
            # (sea/message_stamp.sync_presented_message_ids_with_sent)。

            # End subagent thread on success
            if execution == "subagent" and subagent_thread_id:
                gen_chronicle = getattr(node_def, "subagent_chronicle", True)
                chronicle = self.runtime._end_subagent_thread(persona, subagent_thread_id, subagent_parent_id, generate_chronicle=gen_chronicle, pulse_context=state.get("_pulse_context"))
                state["_subagent_chronicle"] = chronicle or ""
                log_sea_trace(playbook.name, node_id, "EXEC", f"← {sub_name} [subagent ended, chronicle={'yes' if chronicle else 'no'}]")

            # Success path: clear error flag
            state["_exec_error"] = False
            state.pop("_exec_error_detail", None)

            # Track executed playbook in executed_playbooks list
            executed_list = state.get("executed_playbooks")
            if isinstance(executed_list, list):
                executed_list.append(str(sub_name).strip())
                LOGGER.debug("[sea][exec] Added '%s' to executed_playbooks: %s", sub_name, executed_list)

            # Append tool result message to close the router function call pair
            joined = ""
            if sub_outputs:
                joined = "\n".join(str(item).strip() for item in sub_outputs if str(item).strip())
            self.runtime._append_tool_result_message(state, str(sub_name).strip(), joined or "(completed)")
            if sub_outputs:
                state["last"] = sub_outputs[-1]
            return state

        return node

    def lg_memorize_node(self, node_def: Any, persona: Any, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> StateNode:
        async def node(state: dict) -> dict:
            # Send status event for node execution
            node_id = getattr(node_def, "id", "memorize")
            if event_callback:
                event_callback({"type": "status", "content": f"{playbook.name} / {node_id}", "playbook": playbook.name, "node": node_id})
            # Include all state variables for template expansion (e.g., structured output like document_data.*)
            variables = dict(state)
            # Flatten nested dicts/lists for dot notation access (e.g., finalize_output.content)
            for key, value in list(state.items()):
                if isinstance(value, dict):
                    flat = self.runtime._flatten_dict(value)
                    for path, val in flat.items():
                        variables[f"{key}.{path}"] = val
            variables.update({
                "input": state.get("input", ""),
                "last": state.get("last", ""),
                "persona_id": getattr(persona, "persona_id", None),
                "persona_name": getattr(persona, "persona_name", None),
            })
            action_template = getattr(node_def, "action", None) or "{last}"
            LOGGER.debug("[memorize] action_template=%s", action_template)
            LOGGER.debug("[memorize] available variables containing 'finalize': %s",
                        {k: v for k, v in variables.items() if 'finalize' in str(k).lower()})
            memo_text = _format(action_template, variables)
            LOGGER.debug("[memorize] memo_text=%s", memo_text)
            role = getattr(node_def, "role", "assistant") or "assistant"
            tags = getattr(node_def, "tags", None)
            # Phase 3 段階 4-C: MemorizeNodeDef に line_role / scope フィールドが追加された。
            # 未指定時は _store_memory が PulseContext の current_line_metadata() から
            # 自動解決する (= 現在のライン階層に従う)。明示指定したい場合 (例: 親ラインに
            # 明示記録、サブラインを volatile で揮発、メタ判断ターンを discardable で保存)
            # のみ Playbook 側でフィールドを書く。
            line_role = getattr(node_def, "line_role", None)
            scope = getattr(node_def, "scope", None)
            pulse_id = state.get("_pulse_id")
            pulse_context = state.get("_pulse_context")
            metadata_key = getattr(node_def, "metadata_key", None)
            metadata = state.get(metadata_key) if metadata_key else None
            # action が `{last}` だけで role が assistant なら、この MEMORIZE は
            # state["last"] をそのまま本人の発言として記憶に書く。前の LLM ノードが
            # 記憶に保存しなかった返事 (sea/message_stamp.UNSTORED_REPLY_STATE_KEY) が
            # その state["last"] なら、保存した行の id を返事の dict に載せるので、
            # `_store_memory` から行の id を受け取る。それ以外は従来どおり bool で
            # 受ける (空の memo_text で `return_message_id=True` だと "" が返り、
            # 失敗の警告に化けるため、受け取るのは必要な回だけ)。
            stores_last_as_is = (
                role == "assistant"
                and action_template == "{last}"
                and bool(memo_text)
                and unstored_reply_for_last(state) is not None
            )
            stored = self.runtime._store_memory(
                persona, memo_text,
                role=role, tags=tags,
                pulse_id=pulse_id, metadata=metadata,
                line_role=line_role, scope=scope,
                pulse_context=pulse_context,
                playbook_name=playbook.name,
                # 2026-05-20: thought_signature 永続化 (MEMORIZE node 経由 / assistant 役のみ意味あり)。
                # role != "assistant" の場合は state 値に関わらず無害 (signature 解釈は Gemini text Part のみ)。
                thought_signature=state.get("_last_thought_signature") if role == "assistant" else None,
                # 書き込み時の機械刻印 (v3 §7.1)。role='assistant' のときだけ効く
                # (判定は _store_memory 側)。刻むのは action が `{last}` だけのとき
                # に限る — state の刻印の材料は直近の LLM ノードのもので、本文が
                # state["last"] (= その生成の出力。間で書き換えたノードの後は
                # compile_playbook が材料を消している) のときだけ帰属が成り立つ。
                # `{other}` などから作った本文は、前の生成や子の出力かもしれず、
                # どの生成のものか確かめられないので刻印なしに倒す (Codex 五巡目)。
                beat_state=state if action_template == "{last}" else None,
                return_message_id=stores_last_as_is,
            )
            if stored and stores_last_as_is:
                # state["last"] を memo_text で書き換える前に載せる
                # (unstored_reply_for_last は state["last"] と返事の本文を値で比べる)。
                attach_stored_id_to_unstored_reply(state, stored)
            if not stored:
                LOGGER.warning("Failed to store memory in MEMORIZE node %s", node_id)
                if event_callback:
                    event_callback({
                        "type": "warning",
                        "content": "記憶の保存に失敗しました。会話内容が記録されていない可能性があります。",
                        "warning_code": "memorize_failed",
                        "display": "toast",
                    })
            log_sea_trace(
                playbook.name, node_id, "MEMORIZE",
                f"role={role} line_role={line_role} scope={scope} tags={tags} text=\"{memo_text}\"",
            )
            state["last"] = memo_text
            if outputs is not None:
                outputs.append(memo_text)

            # Activity trace: record memorize execution
            if not playbook.name.startswith(("meta_", "sub_")):
                pb_display = playbook.display_name or playbook.name
                node_label = getattr(node_def, "label", None) or node_id
                _at = state.get("_activity_trace")
                if isinstance(_at, list):
                    _at.append({"action": "memorize", "name": node_label, "playbook": pb_display})
                if event_callback:
                    event_callback({
                        "type": "activity", "action": "memorize", "name": node_label,
                        "playbook": pb_display, "status": "completed",
                        "persona_id": getattr(persona, "persona_id", None),
                        "persona_name": getattr(persona, "persona_name", None),
                        "pulse_id": state.get("_pulse_id"),
                        # 発火時点の部屋 (TOOL ノードと同じ理由)。
                        "building_id": event_building_id(self.runtime, persona),
                    })

            # Debug: log speak_content at end of memorize node
            speak_content = state.get("speak_content", "")
            LOGGER.info("[DEBUG] memorize node end: state['speak_content'] = '%s'", speak_content)

            # MEMORIZE does NOT append to PulseContext.
            # Its job is writing to SAIMemory (persistent storage).
            # Runtime tracing is handled by TOOL and LLM nodes.

            return state

        return node

    def lg_speak_node(self, state: dict, persona: Any, building_id: str, playbook: PlaybookSchema, outputs: Optional[List[str]] = None, event_callback: EventCallback = None) -> dict:
        # Send status event for node execution
        if event_callback:
            event_callback({"type": "status", "content": f"{playbook.name} / speak", "playbook": playbook.name, "node": "speak"})
        text = state.get("last") or ""
        reasoning_text = state.pop("_reasoning_text", "")
        reasoning_details_val = state.pop("_reasoning_details", None)
        auto_recall_text = state.pop("_auto_recall_text", None)
        activity_trace = state.get("_activity_trace")
        pulse_id = state.get("_pulse_id")
        eff_bid = self.runtime._effective_building_id(persona, building_id)
        # Build extra metadata with reasoning for SAIMemory storage
        speak_metadata: Dict[str, Any] = {}
        if reasoning_text:
            speak_metadata["reasoning"] = reasoning_text
        if reasoning_details_val is not None:
            speak_metadata["reasoning_details"] = reasoning_details_val
        if auto_recall_text:
            # 記憶アーキv2 §4.5: reasoning と同じ流儀で「ふと浮かんだ記憶」を永続化。
            speak_metadata["auto_recall"] = auto_recall_text
        # 書き込み時の機械刻印 (v3 §7.1)。SPEAK ノードの emit_speak は
        # add_to_persona_only 経由で memory.db にも 1 行残す = 生成メッセージの
        # 永続点なので、_store_memory 経路と同じ刻印を載せる。
        speak_metadata = stamp_generation_metadata(speak_metadata, state) or {}
        building_msg = self.emitters["speak"](persona, eff_bid, text, pulse_id=pulse_id, extra_metadata=speak_metadata if speak_metadata else None, event_callback=event_callback)
        # 前の LLM ノードが記憶に保存しなかった返事がこの state["last"] なら、
        # emit_speak が記憶に書いた行の id (SPEAK_MEMORY_MESSAGE_ID_KEY) をその返事の
        # dict に載せる (sea/message_stamp.py)。建物への保存が失敗した回も、記憶に
        # 書けていれば emit_speak は message_id の無い dict でこの id を返す。
        if isinstance(building_msg, dict):
            attach_stored_id_to_unstored_reply(
                state, building_msg.get(SPEAK_MEMORY_MESSAGE_ID_KEY),
            )
        if outputs is not None:
            outputs.append(text)
        if event_callback:
            # building_id = この発言が残った部屋。表示中の部屋と違う吹き出しを
            # 画面が作らないための材料 (docs/issues/pulse_beats_merge_into_single_record.md 契約 5)。
            say_event: Dict[str, Any] = {"type": "say", "content": text, "persona_id": getattr(persona, "persona_id", None), "building_id": eff_bid}
            if pulse_id:
                say_event["pulse_id"] = pulse_id
            if building_msg and building_msg.get("message_id"):
                say_event["message_id"] = str(building_msg["message_id"])
            if reasoning_text:
                say_event["reasoning"] = reasoning_text
            if reasoning_details_val is not None:
                say_event["reasoning_details"] = reasoning_details_val
            if activity_trace:
                say_event["activity_trace"] = list(activity_trace)
            event_callback(say_event)

        # Append to PulseContext (speak is important by default)
        _pulse_ctx = state.get("_pulse_context")
        if _pulse_ctx:
            from sea.pulse_context import PulseLogEntry
            _pulse_ctx.append(PulseLogEntry(
                role="assistant", content=text,
                node_id="speak", playbook_name=playbook.name,
                important=True))

        return state
