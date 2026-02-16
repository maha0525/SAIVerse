"""Utility helpers shared by LLM client implementations."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Set, Tuple

_log = logging.getLogger(__name__)


def content_to_text(content: Any) -> str:
    """Extract plain text from SDK message payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: List[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if text:
                texts.append(str(text))
        return "".join(texts)
    return ""


def obj_to_dict(obj: Any) -> Any:
    """Best-effort conversion to plain dict for SDK objects."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump") and callable(getattr(obj, "model_dump")):
        try:
            return obj.model_dump()
        except Exception:
            _log.warning("obj_to_dict: model_dump() failed for %s", type(obj).__name__, exc_info=True)
    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        try:
            return obj.to_dict()
        except Exception:
            _log.warning("obj_to_dict: to_dict() failed for %s", type(obj).__name__, exc_info=True)
    return obj


def is_truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def merge_reasoning_strings(chunks: List[str]) -> List[Dict[str, str]]:
    if not chunks:
        return []
    text = "".join(chunks).strip()
    if not text:
        return []
    return [{"title": "", "text": text}]


# ---------------------------------------------------------------------------
# Image attachment helpers (shared across providers)
# ---------------------------------------------------------------------------

def parse_attachment_limit(provider: str = "") -> Optional[int]:
    """Parse image attachment embed limit from environment variables.

    Checks ``SAIVERSE_{PROVIDER}_ATTACHMENT_LIMIT`` first, then the
    universal ``SAIVERSE_ATTACHMENT_LIMIT`` as fallback.
    Returns ``None`` when no limit is configured.
    """
    limit_str = None
    if provider:
        limit_str = os.getenv(f"SAIVERSE_{provider.upper()}_ATTACHMENT_LIMIT")
    if limit_str is None:
        limit_str = os.getenv("SAIVERSE_ATTACHMENT_LIMIT")
    if limit_str is None:
        return None
    try:
        val = int(limit_str.strip())
        return max(val, 0)
    except ValueError:
        _log.warning("Invalid attachment limit '%s'; ignoring", limit_str)
        return None


def compute_allowed_attachment_keys(
    attachment_cache: Dict[int, list],
    max_embeds: Optional[int],
    exempt_indices: Optional[Set[int]] = None,
) -> Optional[Set[Tuple[int, int]]]:
    """Determine which ``(msg_idx, att_idx)`` pairs should be embedded.

    Selects the *max_embeds* most-recent attachments (by message index,
    most recent first).  Attachments in *exempt_indices* (e.g.
    ``__visual_context__`` messages) are always allowed and do not count
    towards the limit.

    Returns ``None`` when *max_embeds* is ``None`` (unlimited).
    """
    if max_embeds is None:
        return None
    if not attachment_cache:
        return set()

    exempt = exempt_indices or set()
    ordered: List[Tuple[int, int]] = []
    for msg_idx in sorted(attachment_cache.keys(), reverse=True):
        if msg_idx in exempt:
            continue
        for att_idx in range(len(attachment_cache[msg_idx])):
            ordered.append((msg_idx, att_idx))

    allowed: Set[Tuple[int, int]] = set(ordered[:max_embeds]) if max_embeds > 0 else set()

    # Exempt attachments are always allowed
    for msg_idx in exempt:
        if msg_idx in attachment_cache:
            for att_idx in range(len(attachment_cache[msg_idx])):
                allowed.add((msg_idx, att_idx))
            _log.debug(
                "visual context at idx=%d exempted from attachment limit (%d images)",
                msg_idx,
                len(attachment_cache[msg_idx]),
            )

    return allowed


def image_summary_note(
    path: str,
    mime_type: str,
    uri: str,
    skip_summary: bool = False,
) -> str:
    """Build a text note for an image that will not be embedded as binary.

    Tries ``ensure_image_summary()`` for a meaningful description.
    Falls back to a bare ``[画像: {uri}]`` reference with a warning
    when the summary is unavailable.
    """
    if not skip_summary:
        try:
            from pathlib import Path as _Path

            from saiverse.media_summary import ensure_image_summary

            summary = ensure_image_summary(_Path(path), mime_type)
            if summary:
                return f"[画像: {uri}] {summary}"
        except Exception:
            _log.exception("Image summary generation error for %s", path)
        _log.warning(
            "Image summary unavailable for %s; using URI-only reference",
            path,
        )
    return f"[画像: {uri}]"
