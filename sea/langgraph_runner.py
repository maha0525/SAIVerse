"""Optional LangGraph executor for SEA playbooks.

If langgraph is not installed, this module falls back to a no-op compile that
returns None. SEARuntime will detect None and use its lightweight runner.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

try:  # pragma: no cover - optional dependency
    from langgraph.graph import StateGraph, END, START
except Exception:  # langgraph missing or import error
    StateGraph = None  # type: ignore
    END = START = None  # type: ignore


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

        for node_def in playbook.nodes:
            logger.debug("[langgraph] Adding node '%s' (type=%s) to playbook '%s'",
                        node_def.id, node_def.type, playbook.name)

            if node_def.type == NodeType.EXEC and exec_node_factory is not None:
                graph.add_node(node_def.id, exec_node_factory(node_def))
            elif node_def.type == NodeType.EXEC and exec_node_factory is None:
                logger.error("[langgraph] Cannot add EXEC node '%s': exec_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.LLM:
                graph.add_node(node_def.id, llm_node_factory(node_def))
            elif node_def.type == NodeType.TOOL:
                graph.add_node(node_def.id, tool_node_factory(node_def))
            elif node_def.type == NodeType.SPEAK:
                graph.add_node(node_def.id, speak_node)
            elif node_def.type == NodeType.SAY and say_node_factory is not None:
                graph.add_node(node_def.id, say_node_factory(node_def))
            elif node_def.type == NodeType.SAY and say_node_factory is None:
                logger.error("[langgraph] Cannot add SAY node '%s': say_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.THINK:
                graph.add_node(node_def.id, think_node)
            elif node_def.type == NodeType.MEMORY and memorize_node_factory is not None:
                graph.add_node(node_def.id, memorize_node_factory(node_def))
            elif node_def.type == NodeType.MEMORY and memorize_node_factory is None:
                logger.error("[langgraph] Cannot add MEMORY node '%s': memorize_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.SUBPLAY and subplay_node_factory is not None:
                graph.add_node(node_def.id, subplay_node_factory(node_def))
            elif node_def.type == NodeType.SUBPLAY and subplay_node_factory is None:
                logger.error("[langgraph] Cannot add SUBPLAY node '%s': subplay_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.SET and set_node_factory is not None:
                graph.add_node(node_def.id, set_node_factory(node_def))
            elif node_def.type == NodeType.SET and set_node_factory is None:
                logger.error("[langgraph] Cannot add SET node '%s': set_node_factory is None", node_def.id)
                return None
            elif node_def.type == NodeType.PASS:
                graph.add_node(node_def.id, lambda state: state)
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
            # Check for conditional_next first (takes precedence over next)
            conditional_next = getattr(node_def, "conditional_next", None)
            if conditional_next:
                logger.debug("[langgraph] Node '%s' has conditional_next", node_def.id)
                # Create routing function that returns path key (not node ID directly)
                def make_router(cond_next):
                    def router_fn(state: dict) -> str:
                        import logging
                        logger = logging.getLogger(__name__)

                        # Resolve field value from state (supports nested keys)
                        field_path = cond_next.field.split(".")
                        value = state
                        for key in field_path:
                            if isinstance(value, dict):
                                value = value.get(key)
                            else:
                                value = None
                                break

                        # Get operator (default: eq for exact match)
                        operator = getattr(cond_next, "operator", "eq") or "eq"

                        logger.debug("[langgraph] conditional_next: field=%s value=%s operator=%s cases=%s",
                                    cond_next.field, value, operator, list(cond_next.cases.keys()))

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
                                    case_num = float(case_key)
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
                                        return case_key
                                except (ValueError, TypeError):
                                    continue

                            # No numeric match, try default
                            if "default" in cond_next.cases:
                                logger.debug("[langgraph] conditional_next: no numeric match, using default")
                                return "default"
                            logger.debug("[langgraph] conditional_next: no match, ending")
                            return "__end__"

                        # Default: exact string match (eq operator)
                        value_str = str(value) if value is not None else ""

                        if value_str in cond_next.cases:
                            result = value_str
                        elif "default" in cond_next.cases:
                            result = "default"
                        else:
                            result = "__end__"

                        logger.debug("[langgraph] conditional_next: selected path=%s -> target=%s", result, cond_next.cases.get(result, "END"))
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
