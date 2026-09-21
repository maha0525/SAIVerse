"""Display-only API messages. Wording belongs to the frontend message table.

Never use these envelopes for persona speech, memories, prompts or user data.
"""
from typing import Any


def ui_message(key: str, **params: Any) -> dict:
    """Wrap message key and dynamic parameters for frontend localization."""
    return {"$ui": key, "params": params}
