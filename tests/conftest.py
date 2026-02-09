"""Shared pytest fixtures and helpers for SAIVerse test suite."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_builtin_tool(name: str):
    """Load a tool module from builtin_data/tools/ by name.

    Usage::

        calculator = load_builtin_tool("calculator")
        result = calculator.calculate_expression("1+2")
    """
    tool_path = PROJECT_ROOT / "builtin_data" / "tools" / f"{name}.py"
    if not tool_path.exists():
        raise FileNotFoundError(f"Tool not found: {tool_path}")
    spec = importlib.util.spec_from_file_location(f"_builtin_tools.{name}", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
