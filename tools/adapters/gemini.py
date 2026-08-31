from typing import Any

from google.genai import types
from tools.core import ToolSchema


def _remap_oneof_to_anyof(node: Any) -> Any:
    """Recursively rewrite JSON-Schema ``oneOf`` to ``anyOf``.

    google-genai's ``types.Schema`` supports ``anyOf`` but rejects ``oneOf``
    as an extra field (``extra_forbidden``), which otherwise aborts tool
    registration for the whole MCP server. For LLM function-calling the two
    are interchangeable — both mean "the value matches one of these
    subschemas"; the XOR semantics of ``oneOf`` are not enforced by the model
    anyway — so we rename rather than drop, preserving the alternative-types
    information for the model.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            # Only rename when there is no pre-existing anyOf to collide with.
            if key == "oneOf" and "anyOf" not in node:
                key = "anyOf"
            out[key] = _remap_oneof_to_anyof(value)
        return out
    if isinstance(node, list):
        return [_remap_oneof_to_anyof(item) for item in node]
    return node


def to_gemini(tool: ToolSchema) -> types.Tool:
    parameters = _remap_oneof_to_anyof(tool.parameters)
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=types.Schema(**parameters),
                response=types.Schema(type=types.Type(tool.result_type.upper())),
            )
        ]
    )
