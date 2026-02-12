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
    TOOL_CALL = "tool_call"
    SPEAK = "speak"
    THINK = "think"
    MEMORY = "memorize"
    SAY = "say"
    PASS = "pass"
    SUBPLAY = "subplay"
    SET = "set"
    EXEC = "exec"
    STELIS_START = "stelis_start"
    STELIS_END = "stelis_end"


class LLMNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.LLM]
    action: Optional[str] = Field(default=None, description="Prompt template. Use {variable_name} placeholders.")
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    context_profile: Optional[str] = Field(
        default=None,
        description="Context profile name. Overrides playbook-level context_requirements and model_type. "
                    "Values: 'conversation' (normal model, full context), 'router' (lightweight, full context), "
                    "'worker' (normal, isolated), 'worker_light' (lightweight, isolated)."
    )
    model_type: Optional[str] = Field(
        default="normal",
        description="Which model to use: 'normal' (default) or 'lightweight' for faster/cheaper models. "
                    "Ignored when context_profile is set (profile determines model)."
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
                    "Keys are dot-notated paths in structured output, values are target state variable names."
    )
    available_tools: Optional[List[str]] = Field(
        default=None,
        description="List of tool names that LLM can call. If specified, enables tool calling for this node."
    )
    output_keys: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Map output types to state keys. Examples: [{'text': 'speak_content'}, {'function_call': 'tool_call'}]. "
                    "Supported types: 'text', 'function_call', 'thought'. "
                    "Function calls are stored as nested keys: '<key>.name', '<key>.args.<arg_name>'."
    )
    memorize: Optional[Dict[str, Any]] = Field(
        default=None,
        description="If specified, save prompt and response to SAIMemory. "
                    "Example: {'tags': ['conversation']}. "
                    "Tags will be applied to both user (prompt) and assistant (response) messages."
    )
    speak: Optional[bool] = Field(
        default=None,
        description="If True, output response to Building (UI). "
                    "When SAIVERSE_LLM_STREAMING=true (default), streams response chunks in real-time. "
                    "When false, sends complete response after generation."
    )
    metadata_key: Optional[str] = Field(
        default=None,
        description="State key containing metadata dict to attach to the speak message "
                    "(e.g., media attachments from tool execution). Only used when speak=true."
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


class ToolCallNodeDef(BaseModel):
    """Node that dynamically executes a tool chosen by an LLM node.

    Reads the tool name and arguments from state (stored by an LLM node with
    available_tools + output_keys), looks up the tool in TOOL_REGISTRY, and
    executes it.  This enables agentic loops where the LLM freely picks tools
    without per-tool branching in the playbook graph.
    """
    id: str
    type: Literal[NodeType.TOOL_CALL]
    call_source: str = Field(
        default="fc",
        description="State key prefix where the LLM stored the function call. "
                    "Reads '{call_source}.name' for the tool name and "
                    "'{call_source}.args' for the arguments dict. "
                    "Falls back to legacy state keys 'tool_name'/'tool_args' if not found."
    )
    output_key: Optional[str] = Field(
        default=None,
        description="Key name to store tool result in state for later nodes."
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
    execution: Optional[str] = Field(
        default="inline",
        description="Execution mode: 'inline' (default, runs in parent context) or "
                    "'subagent' (runs in a temporary thread, only result returns to parent)."
    )
    subagent_chronicle: bool = Field(
        default=True,
        description="When execution='subagent', generate a chronicle summary on completion."
    )
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
    execution: Optional[str] = Field(
        default="inline",
        description="Execution mode: 'inline' (default, runs in parent context) or "
                    "'subagent' (runs in a temporary thread, only result returns to parent)."
    )
    subagent_chronicle: bool = Field(
        default=True,
        description="When execution='subagent', generate a chronicle summary on completion."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )
    error_next: Optional[str] = Field(
        default=None,
        description="Node to transition to when sub-playbook execution fails. "
                    "If not set, normal next/conditional_next is used even on error."
    )


class StelisConfig(BaseModel):
    """Configuration for Stelis thread creation."""
    window_ratio: float = Field(
        default=0.8,
        description="Portion of parent's context window to allocate to this Stelis thread (0.0-1.0)."
    )
    max_depth: int = Field(
        default=3,
        description="Maximum allowed nesting depth for Stelis threads."
    )
    chronicle_prompt: Optional[str] = Field(
        default=None,
        description="Prompt to use when generating Chronicle summary on completion."
    )


class StelisStartNodeDef(BaseModel):
    """Node that starts a new Stelis thread for hierarchical context management."""
    id: str
    type: Literal[NodeType.STELIS_START]
    label: Optional[str] = Field(
        default=None,
        description="Human-readable label for this Stelis session (e.g., 'Coding Session')."
    )
    stelis_config: Optional[StelisConfig] = Field(
        default=None,
        description="Configuration for the Stelis thread. Uses defaults if not specified."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


class StelisEndNodeDef(BaseModel):
    """Node that ends the current Stelis thread and returns to parent context."""
    id: str
    type: Literal[NodeType.STELIS_END]
    label: Optional[str] = Field(
        default=None,
        description="Human-readable label for logging purposes."
    )
    generate_chronicle: bool = Field(
        default=True,
        description="Whether to generate a Chronicle summary when ending the Stelis thread."
    )
    next: Optional[str] = None
    conditional_next: Optional[ConditionalNext] = Field(
        default=None,
        description="Conditional routing based on state field. If specified, overrides 'next'."
    )


NodeDef = Union[
    LLMNodeDef, ToolNodeDef, ToolCallNodeDef, SpeakNodeDef, ThinkNodeDef,
    MemorizeNodeDef, SayNodeDef, PassNodeDef, SubPlayNodeDef, SetNodeDef,
    ExecNodeDef, StelisStartNodeDef, StelisEndNodeDef
]

class InputParam(BaseModel):
    name: str
    description: str
    source: Optional[str] = Field(
        default=None,
        description="Optional parent state key (e.g., 'parent.input', 'router.query'). If not specified, defaults to 'input'."
    )

    # Type and validation
    param_type: str = Field(
        default="string",
        description="Parameter type: 'string', 'number', 'boolean', 'enum'"
    )
    required: bool = Field(
        default=True,
        description="Whether this parameter is required"
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default value if not provided"
    )

    # Enum options (for param_type='enum')
    enum_values: Optional[List[str]] = Field(
        default=None,
        description="Static list of allowed values for enum type"
    )
    enum_source: Optional[str] = Field(
        default=None,
        description="Dynamic enum source in format 'collection:scope'. "
                    "Examples: 'playbooks:router_callable', 'buildings:current_city', "
                    "'items:current_building', 'personas:current_city', 'tools:available'"
    )

    # UI display control
    user_configurable: bool = Field(
        default=False,
        description="If true, this parameter is shown in UI for user input"
    )
    ui_widget: Optional[str] = Field(
        default=None,
        description="UI widget type: 'text', 'textarea', 'dropdown', 'radio'. "
                    "Defaults to 'dropdown' for enum, 'text' for string."
    )


class ContextRequirements(BaseModel):
    """Defines what context should be loaded when running a playbook."""
    history_depth: Union[int, str] = Field(
        default="full",
        description="History depth: 'full' (use persona's context_length), number (character count), "
                    "'Nmessages' (e.g., '10messages' for 10 recent messages), or 0/'none' (no history)"
    )
    history_balanced: bool = Field(
        default=False,
        description="If True, balance history across conversation partners (user + other personas)"
    )
    include_internal: bool = Field(
        default=False,
        description="If True, include internal thoughts (internal tag) in history. Useful for autonomous mode."
    )
    inventory: bool = Field(default=True, description="Include persona inventory in system prompt")
    building_items: bool = Field(default=True, description="Include building items in system prompt")
    system_prompt: bool = Field(default=True, description="Include persona and building system prompts")
    available_playbooks: bool = Field(default=False, description="Include available playbooks list in system prompt")
    visual_context: bool = Field(default=False, description="Include visual context (Building/Persona images) after system prompt")
    memory_weave: bool = Field(default=False, description="Include Memory Weave context (Chronicle + Memopedia) after system prompt")
    working_memory: bool = Field(default=False, description="Include working memory contents in system prompt")
    realtime_context: bool = Field(
        default=True,
        description="Include realtime context (current time, previous AI response time, spatial info) near end of context. "
                    "Placing time-sensitive info at the end improves LLM context caching efficiency."
    )


# ---------------------------------------------------------------------------
# Context Profiles — predefined combinations of model_type + context settings
# ---------------------------------------------------------------------------
# conversation / router share the same context (only model_type differs).
# worker / worker_light are fully isolated (no history, no system prompt).

_FULL_CONTEXT_REQUIREMENTS = ContextRequirements(
    history_depth="full",
    history_balanced=False,
    include_internal=False,
    system_prompt=True,
    memory_weave=True,
    working_memory=False,
    inventory=True,
    building_items=True,
    available_playbooks=True,
    visual_context=True,
    realtime_context=True,
)

_ISOLATED_CONTEXT_REQUIREMENTS = ContextRequirements(
    history_depth=0,
    history_balanced=False,
    include_internal=False,
    system_prompt=False,
    memory_weave=False,
    working_memory=False,
    inventory=False,
    building_items=False,
    available_playbooks=False,
    visual_context=False,
    realtime_context=False,
)

CONTEXT_PROFILES: Dict[str, Dict[str, Any]] = {
    "conversation": {
        "model_type": "normal",
        "requirements": _FULL_CONTEXT_REQUIREMENTS,
    },
    "router": {
        "model_type": "lightweight",
        "requirements": _FULL_CONTEXT_REQUIREMENTS,
    },
    "worker": {
        "model_type": "normal",
        "requirements": _ISOLATED_CONTEXT_REQUIREMENTS,
    },
    "worker_light": {
        "model_type": "lightweight",
        "requirements": _ISOLATED_CONTEXT_REQUIREMENTS,
    },
}


class PlaybookSchema(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]+$")
    display_name: Optional[str] = Field(default=None, description="Human-readable display name for UI. Falls back to name if not set.")
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
    dev_only: bool = Field(
        default=False,
        description="If true, this playbook is only available when developer mode is enabled."
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
