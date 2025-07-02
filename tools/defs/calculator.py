"""Calculator tool supporting +, -, *, /, exponentiation (^), and factorial (!) with
Python AST evaluation. Designed for OpenAI / Gemini function‑calling.
"""

import ast
import logging
import math
import operator as op
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Callable
from dataclasses import dataclass
from google.genai import types
from tools.defs import ToolSchema


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FILE = Path(os.getenv("SAIVERSE_LOG_PATH", str(Path.cwd() / "saiverse_log.txt")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_FILE.touch(exist_ok=True)

logger = logging.getLogger(__name__)
if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(LOG_FILE) for h in logger.handlers):
    handler = logging.FileHandler(LOG_FILE)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False
logger.info("calculator logger initialized")

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
    # Simulate heavy processing so that tool latency is visible in tests
    time.sleep(5)
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

