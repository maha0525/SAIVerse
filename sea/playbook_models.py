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


class LLMNodeDef(BaseModel):
    id: str
    type: Literal[NodeType.LLM]
    action: str = Field(description="Prompt template. Use {variable_name} placeholders.")
    next: Optional[str] = None
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


NodeDef = Union[LLMNodeDef, ToolNodeDef, SpeakNodeDef, ThinkNodeDef]


class InputParam(BaseModel):
    name: str
    description: str


class PlaybookSchema(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9_]+$")
    description: str
    input_schema: List[InputParam]
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
