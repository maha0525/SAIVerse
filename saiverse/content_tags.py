"""Content tag utilities: <user_only> and <in_heart> processing."""
from __future__ import annotations

import re
from typing import Optional

_SPELL_BLOCK_RE = re.compile(r'<details class="spellBlock">.*?</details>', re.DOTALL)
_USER_ONLY_RE = re.compile(r'<user_only(?:\s[^>]*)?>.*?</user_only>', re.DOTALL)
_IN_HEART_RE = re.compile(r'<in_heart>.*?</in_heart>', re.DOTALL)


def wrap_spell_blocks(text: str) -> str:
    """Wrap each spell HTML block with <user_only alt="SpellName"> for building_histories."""
    def _wrap(m: re.Match) -> str:
        html = m.group(0)
        name_m = re.search(r'</svg></span>\s*<span>([^<]+)</span>', html)
        display_name = name_m.group(1) if name_m else "スペル実行"
        return f'<user_only alt="{display_name}">{html}</user_only>'
    return _SPELL_BLOCK_RE.sub(_wrap, text)


def strip_in_heart(text: str) -> str:
    """Remove <in_heart>...</in_heart> content entirely (for building_histories and UI)."""
    return _IN_HEART_RE.sub("", text).strip()


def strip_for_other_persona(text: str) -> Optional[str]:
    """Strip private tags from a message being ingested by another persona.

    - <user_only alt="xxx">...</user_only>  →  [xxx]  (empty string if no alt)
    - <in_heart>...</in_heart>              →  removed entirely

    Returns None if nothing remains after stripping (caller should skip the message).
    """
    def _replace_user_only(m: re.Match) -> str:
        head = m.group(0)[: m.group(0).index(">")]
        alt_m = re.search(r'\balt=["\']([^"\']*)["\']', head)
        return f"[{alt_m.group(1)}]" if alt_m else ""

    result = _USER_ONLY_RE.sub(_replace_user_only, text)
    result = _IN_HEART_RE.sub("", result).strip()
    return result or None
