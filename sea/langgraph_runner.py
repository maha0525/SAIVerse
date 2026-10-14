"""Optional LangGraph executor for SEA playbooks.

If langgraph is not installed, this module falls back to a no-op compile that
returns None. SEARuntime will detect None and use its lightweight runner.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Optional

from sea.message_stamp import drop_stamp_material

try:  # pragma: no cover - optional dependency
    from langgraph.graph import StateGraph, END, START
except Exception:  # langgraph missing or import error
    StateGraph = None  # type: ignore
    END = START = None  # type: ignore


def _may_rewrite_last_with_non_generated_text(node_def: Any) -> bool:
    """``state["last"]`` を生成以外の本文 (ツールの結果・子 Playbook の出力・
    エラー文・代入した値) に書き換えうる種類のノードか。

    ``NodeType.TOOL`` / ``TOOL_CALL`` / ``EXEC`` / ``SUBPLAY`` と、
    ``assignments`` に ``"last"`` キーを持つ ``SET`` と、``action`` が ``{last}``
    だけでない ``MEMORY`` (MEMORIZE)。``LLM`` / ``SPEAK`` / ``THINK`` / ``SAY`` /
    ``PASS`` / ``STELIS_START`` / ``STELIS_END`` / ``"last"`` を代入しない
    ``SET`` / ``action`` が ``{last}`` だけ (または省略・空。``lg_memorize_node``
    は実行時に ``{last}`` を使う) の ``MEMORY`` は対象外。

    MEMORIZE は ``state["last"] = memo_text`` で ``state["last"]`` を ``action``
    のテンプレートから作り直す。``action`` が ``{last}`` 以外 (例: ``{other}``、
    ``"前置き: {last}"``) なら、作り直した ``state["last"]`` は前の LLM ノードの
    返事の本文ではない — 値が同じ文字列 (例: ``"1"``) でも。包まないと
    ``_unstored_reply`` が残り、後ろの SPEAK / THINK / MEMORIZE が保存した行の
    id が前の返事の dict に付く。``{last}`` だけの MEMORIZE は包まない —
    ``state["last"]`` はそのままで、後ろのノードが返事の dict に id を付けるには
    ``_unstored_reply`` が残っている必要がある。MEMORIZE 自身の行の刻印は
    ノードの中 (``_store_memory`` の ``beat_state``) で済むので、包んでも変わらない。
    """
    from sea.playbook_models import NodeType

    node_type = getattr(node_def, "type", None)
    if node_type in (NodeType.TOOL, NodeType.TOOL_CALL, NodeType.EXEC, NodeType.SUBPLAY):
        return True
    if node_type == NodeType.SET:
        assignments = getattr(node_def, "assignments", None)
        return isinstance(assignments, dict) and "last" in assignments
    if node_type == NodeType.MEMORY:
        # lg_memorize_node と同じ既定 (`action or "{last}"`) で判定する。
        action = getattr(node_def, "action", None) or "{last}"
        return action != "{last}"
    return False


def _drop_stamp_material_after(node_fn: Callable[[dict], Any]) -> Callable[[dict], Any]:
    """ノード関数を包み、終わった後に ``drop_stamp_material`` を呼ぶ (sea/message_stamp.py)。

    正常終了・早期 return・例外のどれでも呼ぶ (例外は ``finally`` で消してから
    そのまま投げ直す)。LangGraph の ``StateGraph(dict)`` は state の dict を
    そのままノードへ渡し、ノードが返した dict が次のノードへ渡る。SAIVerse の
    ノードは受け取った dict を書き換えて同じ dict を返すが、別の dict を返す
    ノードでも次のノードが見るのは返した方なので、両方から消す。

    async のノード関数 (例: ``lg_subplay_node`` の ``async def node``) は async の
    関数で、同期のノード関数は同期の関数で包む — LangGraph はノード関数が
    コルーチン関数かどうかで呼び方を決めるので、包んでも変えない。
    """
    if inspect.iscoroutinefunction(node_fn):
        @functools.wraps(node_fn)
        async def async_wrapped(state: dict) -> Any:
            result = None
            try:
                result = await node_fn(state)
                return result
            finally:
                drop_stamp_material(state)
                if isinstance(result, dict) and result is not state:
                    drop_stamp_material(result)

        return async_wrapped

    @functools.wraps(node_fn)
    def sync_wrapped(state: dict) -> Any:
        result = None
        try:
            result = node_fn(state)
            return result
        finally:
            drop_stamp_material(state)
            if isinstance(result, dict) and result is not state:
                drop_stamp_material(result)

    return sync_wrapped


def compile_playbook(
    playbook,
    llm_node_factory: Callable[[Any], Callable[[dict], Any]],
    tool_node_factory: Callable[[Any], Callable[[dict], Any]],
    speak_node: Callable[[dict], Any],
    think_node: Callable[[dict], Any],
    say_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    memorize_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    exec_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    subplay_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    set_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    stelis_start_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    stelis_end_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
    tool_call_node_factory: Optional[Callable[[Any], Callable[[dict], Any]]] = None,
) -> Optional[Callable[[dict], Any]]:
    """Compile a PlaybookSchema into a LangGraph runnable coroutine.

    Returns None if langgraph is unavailable.
    """

    if StateGraph is None:
        return None

    from sea.playbook_models import NodeType
    import logging
    logger = logging.getLogger(__name__)

    try:
        graph = StateGraph(dict)
        logger.debug("[langgraph] Compiling playbook '%s' with %d nodes", playbook.name, len(playbook.nodes))

        # 書き込み時の機械刻印 (sea/message_stamp.py): state["last"] を生成以外の
        # 本文に書き換えうるノードは、終わった後に刻印の材料を落とすよう包んで
        # 登録する。経路ごとに落とす書き方だと早期 return で落とし忘れるので、
        # ノードを登録するこの一か所で包む。
        def _add_node(node_def: Any, node_fn: Callable[[dict], Any]) -> None:
            if _may_rewrite_last_with_non_generated_text(node_def):
                node_fn = _drop_stamp_material_after(node_fn)
            graph.add_node(node_def.id, node_fn)

        for node_def in playbook.nodes:
            logger.debug("[langgraph] Adding node '%s' (type=%s) to playbook '%s'",
                        node_def.id, node_def.type, playbook.name)

            if node_def.type == NodeType.EXEC and exec_node_factory is not None:
                _add_node(node_def, exec_node_factory(node_def))
            elif node_def.type == NodeType.EXEC and exec_node_factory is None:
                logger.error("[langgraph] Cannot add EXEC node '%s': exec_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.LLM:
                _add_node(node_def, llm_node_factory(node_def))
            elif node_def.type == NodeType.TOOL:
                _add_node(node_def, tool_node_factory(node_def))
            elif node_def.type == NodeType.TOOL_CALL and tool_call_node_factory is not None:
                _add_node(node_def, tool_call_node_factory(node_def))
            elif node_def.type == NodeType.TOOL_CALL and tool_call_node_factory is None:
                logger.error("[langgraph] Cannot add TOOL_CALL node '%s': tool_call_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.SPEAK:
                _add_node(node_def, speak_node)
            elif node_def.type == NodeType.SAY and say_node_factory is not None:
                _add_node(node_def, say_node_factory(node_def))
            elif node_def.type == NodeType.SAY and say_node_factory is None:
                logger.error("[langgraph] Cannot add SAY node '%s': say_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.THINK:
                _add_node(node_def, think_node)
            elif node_def.type == NodeType.MEMORY and memorize_node_factory is not None:
                _add_node(node_def, memorize_node_factory(node_def))
            elif node_def.type == NodeType.MEMORY and memorize_node_factory is None:
                logger.error("[langgraph] Cannot add MEMORY node '%s': memorize_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.SUBPLAY and subplay_node_factory is not None:
                _add_node(node_def, subplay_node_factory(node_def))
            elif node_def.type == NodeType.SUBPLAY and subplay_node_factory is None:
                logger.error("[langgraph] Cannot add SUBPLAY node '%s': subplay_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.SET and set_node_factory is not None:
                _add_node(node_def, set_node_factory(node_def))
            elif node_def.type == NodeType.SET and set_node_factory is None:
                logger.error("[langgraph] Cannot add SET node '%s': set_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.PASS:
                _add_node(node_def, lambda state: state)
            elif node_def.type == NodeType.STELIS_START and stelis_start_node_factory is not None:
                _add_node(node_def, stelis_start_node_factory(node_def))
            elif node_def.type == NodeType.STELIS_START and stelis_start_node_factory is None:
                logger.error("[langgraph] Cannot add STELIS_START node '%s': stelis_start_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.STELIS_END and stelis_end_node_factory is not None:
                _add_node(node_def, stelis_end_node_factory(node_def))
            elif node_def.type == NodeType.STELIS_END and stelis_end_node_factory is None:
                logger.error("[langgraph] Cannot add STELIS_END node '%s': stelis_end_node_factory is None", node_def.id)
                return None
            else:
                logger.error("[langgraph] Unhandled node type '%s' for node '%s' in playbook '%s'",
                             node_def.type, node_def.id, playbook.name)
                return None

        logger.debug("[langgraph] Adding edges for playbook '%s'", playbook.name)
        try:
            graph.add_edge(START, playbook.start_node)
            logger.debug("[langgraph] Added START -> '%s'", playbook.start_node)
        except Exception as e:
            logger.error("[langgraph] Failed to add START edge: %s", e, exc_info=True)
            return None

        for node_def in playbook.nodes:
            logger.debug("[langgraph] Processing edges for node '%s'", node_def.id)

            # Handle error_next for exec nodes: route based on _exec_error state flag
            error_next = getattr(node_def, "error_next", None)
            if error_next and getattr(node_def, "type", None) == NodeType.EXEC:
                normal_target = node_def.next
                err_target = error_next

                def make_error_router(_node_id=node_def.id, _pb_name=playbook.name):
                    def router_fn(state: dict) -> str:
                        if state.get("_exec_error"):
                            from saiverse.logging_config import log_sea_trace
                            log_sea_trace(_pb_name, _node_id, "EXEC", "→ error_next (sub-playbook failed)")
                            return "_exec_error"
                        return "_exec_ok"
                    return router_fn

                path_map = {
                    "_exec_error": err_target if err_target else END,
                    "_exec_ok": normal_target if normal_target else END,
                }
                try:
                    graph.add_conditional_edges(node_def.id, make_error_router(), path_map)
                    logger.debug("[langgraph] Added error_next edges for '%s': error->%s, ok->%s",
                                node_def.id, err_target, normal_target)
                except Exception as e:
                    logger.error("[langgraph] Failed to add error_next edges for '%s': %s", node_def.id, e, exc_info=True)
                    return None
                continue

            # Check for conditional_next first (takes precedence over next)
            conditional_next = getattr(node_def, "conditional_next", None)
            if conditional_next:
                logger.debug("[langgraph] Node '%s' has conditional_next", node_def.id)
                # Create routing function that returns path key (not node ID directly)
                def make_router(cond_next, _node_id=node_def.id, _pb_name=playbook.name):
                    def router_fn(state: dict) -> str:
                        import logging
                        logger = logging.getLogger(__name__)

                        # Resolve field value from state (supports nested keys)
                        field_path = cond_next.field.split(".")
                        value = state
                        for key in field_path:
                            if isinstance(value, dict):
                                value = value.get(key)
                            elif isinstance(value, list):
                                # Support array indexing (e.g., "0", "1") and .length
                                if key == "length":
                                    value = len(value)
                                    break
                                elif key.isdigit():
                                    idx = int(key)
                                    value = value[idx] if idx < len(value) else None
                                else:
                                    value = None
                                    break
                            else:
                                value = None
                                break

                        # Get operator (default: eq for exact match)
                        operator = getattr(cond_next, "operator", "eq") or "eq"

                        logger.debug("[langgraph] conditional_next: field=%s value=%s operator=%s cases=%s",
                                    cond_next.field, value, operator, list(cond_next.cases.keys()))

                        # Helper to expand template variables in case keys
                        def _expand_case_key(key: str) -> str:
                            if "{" not in key:
                                return key
                            from sea.runtime import _format
                            return _format(key, state)

                        # Handle numeric comparison operators
                        if operator in ("gte", "gt", "lte", "lt", "ne"):
                            try:
                                num_value = float(value) if value is not None else 0
                            except (ValueError, TypeError):
                                num_value = 0

                            # Find matching case by numeric comparison
                            # Cases are checked in order, first match wins
                            for case_key in cond_next.cases.keys():
                                if case_key == "default":
                                    continue
                                try:
                                    expanded_key = _expand_case_key(case_key)
                                    case_num = float(expanded_key)
                                    matched = False
                                    if operator == "gte" and num_value >= case_num:
                                        matched = True
                                    elif operator == "gt" and num_value > case_num:
                                        matched = True
                                    elif operator == "lte" and num_value <= case_num:
                                        matched = True
                                    elif operator == "lt" and num_value < case_num:
                                        matched = True
                                    elif operator == "ne" and num_value != case_num:
                                        matched = True

                                    if matched:
                                        logger.debug("[langgraph] conditional_next: numeric match %s %s %s -> %s",
                                                    num_value, operator, case_num, case_key)
                                        from saiverse.logging_config import log_sea_trace
                                        log_sea_trace(_pb_name, _node_id, "PASS", f"{cond_next.field}={num_value} {operator} {case_num} → {cond_next.cases.get(case_key, 'END')}")
                                        return case_key
                                except (ValueError, TypeError):
                                    continue

                            # No numeric match, try default
                            if "default" in cond_next.cases:
                                logger.debug("[langgraph] conditional_next: no numeric match, using default")
                                from saiverse.logging_config import log_sea_trace
                                log_sea_trace(_pb_name, _node_id, "PASS", f"{cond_next.field}={num_value} {operator} (no match) → {cond_next.cases.get('default', 'END')}")
                                return "default"
                            logger.debug("[langgraph] conditional_next: no match, ending")
                            return "__end__"

                        # Default: exact string match (eq operator)
                        value_str = str(value) if value is not None else ""

                        # Try direct match first
                        if value_str in cond_next.cases:
                            result = value_str
                        else:
                            # Try expanding template variables in case keys
                            result = None
                            for case_key in cond_next.cases:
                                if case_key == "default":
                                    continue
                                expanded = _expand_case_key(case_key)
                                if expanded == value_str:
                                    result = case_key  # Return original key for path_map
                                    break
                            if result is None:
                                result = "default" if "default" in cond_next.cases else "__end__"

                        target = cond_next.cases.get(result, "END")
                        logger.debug("[langgraph] conditional_next: selected path=%s -> target=%s", result, target)
                        from saiverse.logging_config import log_sea_trace
                        log_sea_trace(_pb_name, _node_id, "PASS", f"{cond_next.field}={value_str} {operator} → {target}")
                        return result
                    return router_fn

                # Build path_map: case_value -> target_node_or_END
                path_map = {}
                for case_value, target_node in conditional_next.cases.items():
                    if target_node is None:
                        path_map[case_value] = END
                    else:
                        path_map[case_value] = target_node
                # Add fallback for unmatched cases
                if "__end__" not in path_map:
                    path_map["__end__"] = END

                try:
                    graph.add_conditional_edges(node_def.id, make_router(conditional_next), path_map)
                    logger.debug("[langgraph] Added conditional edges for '%s'", node_def.id)
                except Exception as e:
                    logger.error("[langgraph] Failed to add conditional edges for '%s': %s", node_def.id, e, exc_info=True)
                    return None
            elif node_def.next:
                try:
                    graph.add_edge(node_def.id, node_def.next)
                    logger.debug("[langgraph] Added edge '%s' -> '%s'", node_def.id, node_def.next)
                except Exception as e:
                    logger.error("[langgraph] Failed to add edge '%s' -> '%s': %s", node_def.id, node_def.next, e, exc_info=True)
                    return None
            else:
                try:
                    graph.add_edge(node_def.id, END)
                    logger.debug("[langgraph] Added edge '%s' -> END", node_def.id)
                except Exception as e:
                    logger.error("[langgraph] Failed to add edge '%s' -> END: %s", node_def.id, e, exc_info=True)
                    return None

        logger.debug("[langgraph] All edges added successfully for playbook '%s'", playbook.name)
        logger.debug("[langgraph] Calling graph.compile() for playbook '%s'", playbook.name)

        try:
            compiled = graph.compile()
            logger.debug("[langgraph] graph.compile() completed without exception")
        except Exception as e:
            logger.error("[langgraph] graph.compile() threw exception: %s", e, exc_info=True)
            return None

        logger.debug("[langgraph] graph.compile() returned: %s (type: %s)",
                    compiled, type(compiled).__name__ if compiled else "None")

        if compiled is None:
            logger.error("[langgraph] graph.compile() returned None for playbook '%s'", playbook.name)
            return None

        # Check if compiled object has ainvoke
        if not hasattr(compiled, 'ainvoke'):
            logger.error("[langgraph] Compiled object does not have 'ainvoke' attribute. Available: %s",
                        dir(compiled))
            return None

        logger.debug("[langgraph] Compilation successful for playbook '%s', returning ainvoke", playbook.name)
        return compiled.ainvoke
    except Exception as exc:
        logger.exception("[langgraph] Failed to compile playbook '%s': %s", playbook.name, exc)
        return None
