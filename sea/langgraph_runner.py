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
            graph.add_node(node_def.id, tool_node_factory(node_def.action))
        elif node_def.type == NodeType.SPEAK:
            graph.add_node(node_def.id, speak_node)
        elif node_def.type == NodeType.THINK:
            graph.add_node(node_def.id, think_node)

    graph.add_edge(START, playbook.start_node)
    for node_def in playbook.nodes:
        if node_def.next:
            graph.add_edge(node_def.id, node_def.next)
        else:
            graph.add_edge(node_def.id, END)

    compiled = graph.compile()
    return compiled.ainvoke
