"""Content tag utilities: <user_only> and <in_heart> processing."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

_SPELL_BLOCK_RE = re.compile(r'<details class="spellBlock">.*?</details>', re.DOTALL)
_USER_ONLY_RE = re.compile(r'<user_only(?:\s[^>]*)?>.*?</user_only>', re.DOTALL)
_IN_HEART_RE = re.compile(r'<in_heart>.*?</in_heart>', re.DOTALL)
# Matches saiverse://item/<slot_ref>/<rest> where slot_ref is b:N, i:N, b:N>M, etc. (not UUID)
_ITEM_SLOT_URI_RE = re.compile(
    r'(saiverse://item/)([bi]:\d+(?:>\d+)*)(/[^\s\)\]>"\']*)?'
)


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


def strip_user_only(text: str) -> str:
    """Remove ``<user_only>...</user_only>`` blocks entirely (for voice / external).

    The ``<user_only>`` tag literally means "for user UI display only" — its
    contents must not flow to TTS, gateway audio, or other voice/external
    output paths. This includes spell ``<details class="spellBlock">`` blocks
    that ``wrap_spell_blocks`` produces, which would otherwise leak the spell
    name, args, and result text into voice output.

    For inter-persona ingestion, use ``strip_for_other_persona`` instead — it
    preserves the ``alt`` text so the listener perceives a placeholder.
    """
    if not text:
        return text
    return _USER_ONLY_RE.sub("", text).strip()


def resolve_item_slot_uris(
    text: str,
    item_service: Any,
    persona_id: str,
    building_id: Optional[str],
) -> str:
    """Replace slot-based item URI references with UUID equivalents for public text.

    saiverse://item/b:3/image  →  saiverse://item/{UUID}/image

    Applies to b:N (building slot), i:N (inventory slot), and nested b:N>M forms.
    Unresolvable references are left unchanged to avoid silently breaking links.
    Only called for outward-facing text (building history, gateway); SAIMemory
    retains the original slot form so the persona remembers what they wrote.
    """
    def _replace(m: re.Match) -> str:
        prefix = m.group(1)    # "saiverse://item/"
        slot_ref = m.group(2)  # e.g. "b:3"
        suffix = m.group(3) or ""  # e.g. "/image"
        try:
            uuid = item_service.resolve_slot_ref(slot_ref, persona_id, building_id)
            return f"{prefix}{uuid}{suffix}"
        except Exception as exc:
            LOGGER.debug("Could not resolve item slot URI '%s': %s", slot_ref, exc)
            return m.group(0)

    return _ITEM_SLOT_URI_RE.sub(_replace, text)


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
