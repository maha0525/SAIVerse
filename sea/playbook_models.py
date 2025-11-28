from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union, Set

from pydantic import BaseModel, Field
from typing_extensions import Literal


class NodeType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    SPEAK = "speak"
    THINK = "think"
    MEMORY = "memorize"
    SAY = "say"
    PASS = "pass"
    SUBPLAY = "subplay"


class LLMNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.LLM]
    action: Optional[str] = Field(default=None, description="Prompt template. Use {variable_name} placeholders.")
    next: Optional[str] = None
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


class ToolNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.TOOL]
    action: str = Field(description="Tool name registered in tools registry.")
    args_input: Optional[Dict[str, str]] = Field(
        default=None,
        description="Map of argument names to state keys. E.g. {'query': 'search_query.query'} passes state['search_query.query'] as 'query' arg."
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


class SpeakNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SPEAK]
    action: Optional[str] = Field(
        default=None, description="Optional template for final output. Defaults to last message content."
    )
    next: Optional[str] = None


class ThinkNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.THINK]
    action: Optional[str] = Field(default=None, description="Optional note to store internally.")
    next: Optional[str] = None






class SayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SAY]
    action: Optional[str] = Field(
        default=None, description="Template for UI output only (no SAIMemory record). Defaults to last message content."
    )
    next: Optional[str] = None


class PassNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.PASS]
    next: Optional[str] = None
class MemorizeNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.MEMORY]
    action: Optional[str] = Field(
        default=None, description="Template for the text to store. Defaults to last message content."
    )
    role: str = Field(default="assistant", description="Role name to store in SAIMemory.")
    tags: Optional[List[str]] = Field(default=None, description="Optional tags for SAIMemory metadata.")
    next: Optional[str] = None





class SubPlayNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.SUBPLAY]
    playbook: str = Field(description="Name of the sub-playbook to execute")
    input_template: Optional[str] = Field(default="{input}", description="Template for the input passed to the sub-playbook")
    propagate_output: bool = Field(default=False, description="If true, append sub-playbook outputs to parent outputs")
    next: Optional[str] = None

NodeDef = Union[LLMNodeDef, ToolNodeDef, SpeakNodeDef, ThinkNodeDef, MemorizeNodeDef, SayNodeDef, PassNodeDef, SubPlayNodeDef]

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


class PlaybookSchema(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]+$")
    description: str
    input_schema: List[InputParam]
    context_requirements: Optional[ContextRequirements] = Field(
        default=None,
        description="Context requirements for this playbook. If not specified, uses full context (backward compatible)."
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

    visited: Set[str] = set()
    current = start_id
    while current:
        if current in visited:
            raise PlaybookValidationError(f"Detected cycle involving node '{current}'")
        visited.add(current)
        node = node_map[current]
        next_id = getattr(node, "next", None)
        if not next_id:
            break
        if next_id not in node_map:
            raise PlaybookValidationError(f"Node '{current}' references missing next '{next_id}'")
        current = next_id

    unreachable = [node_id for node_id in node_map.keys() if node_id not in visited]
    if unreachable:
        raise PlaybookValidationError("Unreachable node(s): " + ", ".join(unreachable))
