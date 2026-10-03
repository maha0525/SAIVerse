"""Calculator tool supporting +, -, *, /, exponentiation (^), and factorial (!) with
Python AST evaluation. Designed for OpenAI / Gemini function‑calling.
"""

import ast
import logging
import math
import operator as op
import re
from typing import Any, Dict, Callable
from dataclasses import dataclass
from google.genai import types
from tools.core import ToolSchema


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core evaluation helpers
# ---------------------------------------------------------------------------
_OPERATORS: dict[type, Any] = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,   # "**" after normalisation implements ^
    ast.USub: op.neg,
}

_FUNCTIONS: dict[str, Any] = {
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


# ---------------------------------------------------------------------------
# Pre‑processing utilities
# ---------------------------------------------------------------------------

def _expand_factorial(expression: str) -> str:
    """Replace trailing "!" with factorial() calls so that AST can parse."""
    pattern = re.compile(r"(\d+|\([^()]*\))!")
    while True:
        new_expr = pattern.sub(r"factorial(\1)", expression)
        if new_expr == expression:
            break
        expression = new_expr
    return expression


def _normalize_power(expression: str) -> str:
    """Convert caret (^) to Python exponentiation (**) when appropriate.

    We replace only when ^ is between a digit/closing‑paren and a digit/opening‑paren
    to avoid touching bitwise XOR cases like "a ^ b" (spaces act as a guard).
    """
    return re.sub(r"(?<=[\d\)])\^(?=[\d\(])", "**", expression)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_expression(expression: str) -> float:
    """Evaluate an arithmetic expression with +, -, *, /, ^ (power), ! (factorial)."""
    logger.info("calculate_expression called with: %s", expression)

    # Normalise factorial first, then exponentiation
    expression = _expand_factorial(expression)
    expression = _normalize_power(expression)

    tree = ast.parse(expression, mode="eval")
    return float(_eval(tree.body))

def schema() -> ToolSchema:
    return ToolSchema(
        name="calculate_expression",
        description="Evaluate arithmetic expression with ^ (power) and ! (factorial).",
        parameters={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Expression to evaluate"}
            },
            "required": ["expression"],
        },
        result_type="number",
    )

