"""Shared pytest fixtures and helpers for SAIVerse test suite."""

import sys
from pathlib import Path

import pytest  # noqa: F401

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Re-export for backward compatibility
from tool_loader import load_builtin_tool  # noqa: E402, F401
