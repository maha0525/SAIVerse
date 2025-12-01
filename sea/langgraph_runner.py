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
) -> Optional[Callable[[dict], Any]]:
    """Compile a PlaybookSchema into a LangGraph runnable coroutine.

    Returns None if langgraph is unavailable.
    """

    if StateGraph is None:
        return None

    from sea.playbook_models import NodeType

    graph = StateGraph(dict)

    for node_def in playbook.nodes:
        if node_def.id == "exec" and exec_node_factory is not None:
            graph.add_node(node_def.id, exec_node_factory(node_def))
        elif node_def.type == NodeType.LLM:
            graph.add_node(node_def.id, llm_node_factory(node_def))
        elif node_def.type == NodeType.TOOL:
            graph.add_node(node_def.id, tool_node_factory(node_def))
        elif node_def.type == NodeType.SPEAK:
            graph.add_node(node_def.id, speak_node)
        elif node_def.type == NodeType.SAY and say_node_factory is not None:
            graph.add_node(node_def.id, say_node_factory(node_def))
        elif node_def.type == NodeType.THINK:
            graph.add_node(node_def.id, think_node)
        elif node_def.type == NodeType.MEMORY and memorize_node_factory is not None:
            graph.add_node(node_def.id, memorize_node_factory(node_def))
        elif node_def.type == NodeType.PASS:
            graph.add_node(node_def.id, lambda state: state)

    graph.add_edge(START, playbook.start_node)
    for node_def in playbook.nodes:
        # Check for conditional_next first (takes precedence over next)
        conditional_next = getattr(node_def, "conditional_next", None)
        if conditional_next:
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

                    # Convert to string for matching
                    value_str = str(value) if value is not None else ""

                    logger.debug("[langgraph] conditional_next: field=%s value=%s cases=%s", cond_next.field, value_str, list(cond_next.cases.keys()))

                    # Return the matched case value (not the target node)
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

            graph.add_conditional_edges(node_def.id, make_router(conditional_next), path_map)
        elif node_def.next:
            graph.add_edge(node_def.id, node_def.next)
        else:
            graph.add_edge(node_def.id, END)

    compiled = graph.compile()
    return compiled.ainvoke
