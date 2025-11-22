from __future__ import annotations

from enum import Enum
from typing import List, Optional, Union

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

