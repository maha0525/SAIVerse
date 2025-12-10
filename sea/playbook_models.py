from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union, Set

from pydantic import BaseModel, Field
from typing_extensions import Literal


class ConditionalNext(BaseModel):
    """Conditional edge routing based on state field value."""
    field: str = Field(description="State key to evaluate (e.g., 'router.playbook'). Supports nested keys with dot notation.")
    operator: Optional[str] = Field(
        default="eq",
        description="Comparison operator: 'eq' (default, exact match), 'gte' (>=), 'gt' (>), 'lte' (<=), 'lt' (<), 'ne' (!=)"
    )
    cases: Dict[str, Optional[str]] = Field(
        description="Mapping of values to next node IDs. Use 'default' key for fallback. Value can be null to end execution. "
                    "For numeric operators (gte/gt/lte/lt), use numeric string keys like '5' and they will be compared numerically."
    )


class NodeType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    SPEAK = "speak"
    THINK = "think"
    MEMORY = "memorize"
    SAY = "say"
    PASS = "pass"
    SUBPLAY = "subplay"
    SET = "set"
    EXEC = "exec"


class LLMNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.LLM]
    action: Optional[str] = Field(default=None, description="Prompt template. Use {variable_name} placeholders.")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    model_type: Optional[str] = Field(
        default="normal",
        description="Which model to use: 'normal' (default) or 'lightweight' for faster/cheaper models."
    )
    response_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional JSON schema to enforce structured output."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store structured output for later nodes. Defaults to node id."
    )
    output_mapping: Optional[Dict[str, str]] = Field(
        default=None,
        description="Map structured output fields to state variables. "
                    "Example: {'router.playbook': 'selected_playbook', 'router.args': 'selected_args'}. "
                    "Keys are dot-notated paths in the structured output, values are target state variable names."
    )
    available_tools: Optional[List[str]] = Field(
        default=None,
        description="List of tool names that the LLM can call. If specified, enables tool calling for this node."
    )
    output_keys: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Map output types to state keys. Examples: [{'text': 'speak_content'}, {'function_call': 'tool_call'}]. "
                    "Supported types: 'text', 'function_call', 'thought'. "
                    "Function calls are stored as nested keys: '<key>.name', '<key>.args.<arg_name>'."
    )


class ToolNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.TOOL]
    action: str = Field(description="Tool name registered in tools registry.")
    args_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Map of argument names to state keys or literal values. Strings are treated as state keys (e.g. {'query': 'search_query.query'}). Non-string values (int/float/bool) are passed as-is to the tool."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store tool result in state for later nodes."
    )
    output_keys: Optional[list] = Field(
        default=None,
        description="List of keys to store tuple results. E.g. ['text', 'snippet', 'file_path'] for multi-value tool returns."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class SpeakNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SPEAK]
    action: Optional[str] = Field(
        default=None, description="Optional template for final output. Defaults to last message content."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class ThinkNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.THINK]
    action: Optional[str] = Field(default=None, description="Optional note to store internally.")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )






class SayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SAY]
    action: Optional[str] = Field(
        default=None, description="Template for UI output only (no SAIMemory record). Defaults to last message content."
    )
    metadata_key: Optional[str] = Field(
        default=None, description="State key containing metadata dict to attach to the message."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class PassNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.PASS]
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )

class MemorizeNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.MEMORY]
    action: Optional[str] = Field(
        default=None, description="Template for the text to store. Defaults to last message content."
    )
    role: str = Field(default="assistant", description="Role name to store in SAIMemory.")
    tags: Optional[List[str]] = Field(default=None, description="Optional tags for SAIMemory metadata.")
    metadata_key: Optional[str] = Field(
        default=None, description="State key containing metadata dict to attach to the message."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )





class SubPlayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SUBPLAY]
    playbook: str = Field(description="Name of the sub-playbook to execute")
    input_template: Optional[str] = Field(default="{input}", description="Template for the input passed to the sub-playbook")
    propagate_output: bool = Field(default=False, description="If true, append sub-playbook outputs to parent outputs")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class SetNodeDef(BaseModel):
    """Node that sets or modifies state variables."""
    id: str
    type: Literal[NodeType.SET]
    assignments: Dict[str, Any] = Field(
        description="Mapping of state keys to values. Values can be: "
                    "- Literal values (number, string, bool): {\"count\": 0, \"name\": \"test\"} "
                    "- Template strings with {var} placeholders: {\"greeting\": \"Hello {name}\"} "
                    "- Arithmetic expressions: {\"count\": \"{count} + 1\"}, {\"total\": \"{a} * {b}\"}"
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class ExecNodeDef(BaseModel):
    """Node that executes a dynamically selected sub-playbook."""
    id: str
    type: Literal[NodeType.EXEC]
    playbook_source: str = Field(
        default="selected_playbook",
        description="State variable name containing the playbook name to execute."
    )
    args_source: Optional[str] = Field(
        default="selected_args",
        description="State variable name containing args dict for the sub-playbook. "
                    "The 'input' or 'query' key from this dict is passed as sub_input."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


NodeDef = Union[LLMNodeDef, ToolNodeDef, SpeakNodeDef, ThinkNodeDef, MemorizeNodeDef, SayNodeDef, PassNodeDef, SubPlayNodeDef, SetNodeDef, ExecNodeDef]

class InputParam(BaseModel):
    name: str
    description: str
    source: Optional[str] = Field(
        default=None,
        description="Optional parent state key (e.g., 'parent.input', 'router.query'). If not specified, defaults to 'input'."
    )


class ContextRequirements(BaseModel):
    """Defines what context should be loaded when running a playbook."""
    history_depth: Union[int, str] = Field(
        default="full",
        description="History depth: number (character count), 'full' (use persona's context_length), or 0/'none' (no history)"
    )
    inventory: bool = Field(default=True, description="Include persona inventory in system prompt")
    building_items: bool = Field(default=True, description="Include building items in system prompt")
    system_prompt: bool = Field(default=True, description="Include persona and building system prompts")
    available_playbooks: bool = Field(default=False, description="Include available playbooks list in system prompt")


class PlaybookSchema(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]+$")
    description: str
    input_schema: List[InputParam]
    output_schema: Optional[List[str]] = Field(
        default=None,
        description="List of state keys to propagate to parent playbook when this sub-playbook completes."
    )
    context_requirements: Optional[ContextRequirements] = Field(
        default=None,
        description="Context requirements for this playbook. If not specified, uses full context (backward compatible)."
    )
    router_callable: bool = Field(
        default=False,
        description="If true, this playbook can be called from the router in meta playbooks."
    )
    user_selectable: bool = Field(
        default=False,
        description="If true, this meta playbook can be selected by user in the UI."
    )
    nodes: List[NodeDef]
    start_node: str

    def node_map(self):
        return {n.id: n for n in self.nodes}


class PlaybookValidationError(ValueError):
    """Raised when a Playbook graph is invalid (unreachable nodes, cycles, etc.)."""


def validate_playbook_graph(playbook: PlaybookSchema) -> None:
    node_map = playbook.node_map()
    start_id = playbook.start_node
    if start_id not in node_map:
        raise PlaybookValidationError(f"start_node '{start_id}' is not defined in nodes")

    # Collect all edges (including conditional ones)
    all_edges: Dict[str, List[Optional[str]]] = {}
    for node in playbook.nodes:
        edges: List[Optional[str]] = []

        # Check conditional_next first (takes precedence over next)
        conditional_next = getattr(node, "conditional_next", None)
        if conditional_next:
            for target in conditional_next.cases.values():
                if target is not None and target not in node_map:
                    raise PlaybookValidationError(
                        f"Node '{node.id}' conditional_next references missing target '{target}'"
                    )
                edges.append(target)
        else:
            # Use regular next
            next_id = getattr(node, "next", None)
            if next_id is not None:
                if next_id not in node_map:
                    raise PlaybookValidationError(
                        f"Node '{node.id}' references missing next '{next_id}'"
                    )
                edges.append(next_id)

        all_edges[node.id] = edges

    # BFS to find all reachable nodes (avoiding cycle check for branching graphs)
    visited: Set[str] = set()
    queue = [start_id]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)

        for next_id in all_edges.get(current, []):
            if next_id is not None and next_id not in visited:
                queue.append(next_id)

    unreachable = [node_id for node_id in node_map.keys() if node_id not in visited]
    if unreachable:
        raise PlaybookValidationError("Unreachable node(s): " + ", ".join(unreachable))
