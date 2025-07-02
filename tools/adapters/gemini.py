from google.genai import types
from tools.defs import ToolSchema

def to_gemini(tool: ToolSchema) -> types.Tool:
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                parameters=types.Schema(**tool.parameters),
                response=types.Schema(type=types.Type(tool.result_type.upper())),
            )
        ]
    )