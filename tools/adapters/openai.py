from typing import Dict, Any
from tools.defs import ToolSchema

def to_openai(tool: ToolSchema) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }