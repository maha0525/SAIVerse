import ast
import operator as op
import logging
import math
import re
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

# Supported functions
_FUNCTIONS = {
    "factorial": lambda x: math.factorial(int(x)),
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
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        func = _FUNCTIONS.get(node.func.id)
        if func is None:
            raise ValueError(f"Unsupported function: {node.func.id}")
        args = [_eval(arg) for arg in node.args]
        return float(func(*args))
    raise ValueError("Unsupported expression")


def _expand_factorial(expression: str) -> str:
    """Expand custom '^' factorial operator into function calls."""
    pattern = re.compile(r"(\d+|\([^()]*\))\^")
    while True:
        new_expr = pattern.sub(r"factorial(\1)", expression)
        if new_expr == expression:
            break
        expression = new_expr
    return expression


def calculate_expression(expression: str) -> float:
    """Evaluate a simple arithmetic expression with factorial support."""
    logging.info("calculate_expression called with: %s", expression)
    expression = _expand_factorial(expression)
    tree = ast.parse(expression, mode="eval")
    return float(_eval(tree.body))


def get_gemini_tool() -> types.Tool:
    """Return Tool definition for Gemini function calling."""
    fn_decl = types.FunctionDeclaration(
        name="calculate_expression",
        description=(
            "Evaluate an arithmetic expression using +, -, *, /, '^' for factorial and parentheses."
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
            "description": "Evaluate an arithmetic expression using +, -, *, /, '^' for factorial and parentheses.",
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
