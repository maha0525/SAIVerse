"""
Utility helpers for persona modules.
"""

import os


def env_int(name: str, default: int) -> int:
    """Read a positive integer from the environment with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except ValueError:
        return default
