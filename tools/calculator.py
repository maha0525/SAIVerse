import ast
import operator as op
from typing import Any

from google.genai import types

# Supported operators
_OPERATORS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.USub: op.neg,
}


def _eval(node: ast.AST) -> float:
    """Recursively evaluate an AST node."""
    if isinstance(node, ast.Num):
        return float(node.n)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _OPERATORS[ast.USub](_eval(node.operand))
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type}")
        return _OPERATORS[op_type](_eval(node.left), _eval(node.right))
    raise ValueError("Unsupported expression")


def calculate_expression(expression: str) -> float:
    """Evaluate a simple arithmetic expression."""
    tree = ast.parse(expression, mode="eval")
    return float(_eval(tree.body))


def get_gemini_tool() -> types.Tool:
    """Return Tool definition for Gemini function calling."""
    fn_decl = types.FunctionDeclaration(
        name="calculate_expression",
        description=(
            "Evaluate a basic arithmetic expression using +, -, *, / and parentheses."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "expression": types.Schema(
                    type=types.Type.STRING,
                    description="Arithmetic expression to evaluate.",
                )
            },
            required=["expression"],
        ),
        response=types.Schema(type=types.Type.NUMBER),
    )
    return types.Tool(function_declarations=[fn_decl])


def get_openai_tool() -> dict[str, Any]:
    """Return tool specification for OpenAI function calling."""
    return {
        "type": "function",
        "function": {
            "name": "calculate_expression",
            "description": "Evaluate a basic arithmetic expression using +, -, *, / and parentheses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression to evaluate.",
                    }
                },
                "required": ["expression"],
            },
        },
    }
