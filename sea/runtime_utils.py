from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict


def _is_llm_streaming_enabled() -> bool:
    """Check whether LLM streaming is enabled via env var (default: enabled)."""
    val = os.getenv("SAIVERSE_LLM_STREAMING", "true")
    result = val.lower() not in ("false", "0", "off", "no")
    logging.info("[DEBUG] _is_llm_streaming_enabled: raw_val=%r, result=%s", val, result)
    return result


def _format(template: str, variables: Dict[str, Any]) -> str:
    """Simple {key} placeholder replacement formatter."""
    lookup: Dict[str, str] = {}
    for key, value in variables.items():
        lookup[str(key)] = "" if value is None else str(value)

    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in lookup:
            return lookup[key]
        return match.group(0)

    return re.sub(r"\{([\w.]+)\}", replacer, template)
