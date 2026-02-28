"""Helpers to prepare OpenAI-compatible chat messages."""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm

from .utils import (
    compute_allowed_attachment_keys,
    content_to_text,
    image_summary_note,
    parse_attachment_limit,
)

ALLOWED_FIELDS = {"role", "content", "name", "tool_calls", "tool_call_id"}


def is_empty_message(msg: Dict[str, Any]) -> bool:
    """Check if a message is empty (no content and no tool_calls)."""
    role = msg.get("role")
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")

    if role in ("assistant", "system", "user"):
        content_empty = not content or (isinstance(content, (list, str)) and len(content) == 0)
        if role == "assistant":
            return content_empty and not tool_calls
        return content_empty
    return False


def scan_message_metadata(
    messages: List[Any],
) -> Tuple[Dict[int, List[Dict[str, Any]]], Set[int], Set[int], Optional[Set[Tuple[int, int]]]]:
    """Build metadata-derived caches for attachment and summary handling."""
    max_image_embeds = parse_attachment_limit("OPENAI")
    attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
    skip_summary_indices: Set[int] = set()
    exempt_indices: Set[int] = set()

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata")
        if isinstance(metadata, dict):
            if metadata.get("__skip_image_summary__"):
                skip_summary_indices.add(idx)
            if metadata.get("__visual_context__"):
                exempt_indices.add(idx)
        media_items = iter_image_media(metadata)
        if media_items:
            attachment_cache[idx] = media_items

    allowed_attachment_keys = compute_allowed_attachment_keys(
        attachment_cache, max_image_embeds, exempt_indices,
    )
    logging.debug(
        "[openai] attachment limit=%s, cached=%d msgs with images",
        "∞" if max_image_embeds is None else max_image_embeds,
        len(attachment_cache),
    )
    return attachment_cache, skip_summary_indices, exempt_indices, allowed_attachment_keys


def normalize_message_role(
    role: Any,
    convert_system_to_user: bool,
    seen_non_system: bool,
    original_content: Any,
) -> Optional[Tuple[str, Optional[str]]]:
    """Normalize role and optionally convert later system messages into tagged user content."""
    normalized = role
    if isinstance(normalized, str) and normalized.lower() == "host":
        normalized = "system"

    if convert_system_to_user and normalized == "system" and seen_non_system:
        content = content_to_text(original_content or "")
        return "user", f"<system>\n{content}\n</system>"

    if isinstance(normalized, str):
        return normalized, None
    return None


def build_message_content_with_attachments(
    *,
    role: str,
    original_content: Any,
    attachments: List[Dict[str, Any]],
    supports_images: bool,
    max_image_bytes: Optional[int],
    skip_summary: bool,
    allowed_attachment_keys: Optional[Set[Tuple[int, int]]],
    message_index: int,
) -> Any:
    """Build content with image embedding or textual summaries."""
    if not attachments:
        return original_content

    text = content_to_text(original_content)
    if supports_images and role == "user":
        parts: List[Dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "text": text})
        for att_idx, att in enumerate(attachments):
            should_embed = (
                allowed_attachment_keys is None
                or (message_index, att_idx) in allowed_attachment_keys
            )
            if should_embed:
                data, effective_mime = load_image_bytes_for_llm(
                    att["path"], att["mime_type"], max_bytes=max_image_bytes,
                )
                if data and effective_mime:
                    b64 = base64.b64encode(data).decode("ascii")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{effective_mime};base64,{b64}"},
                        }
                    )
                    continue
                logging.warning(
                    "Image file not found or unreadable, skipping attachment: %s",
                    att.get("uri") or att.get("path"),
                )
            note = image_summary_note(
                att["path"],
                att["mime_type"],
                att.get("uri", att.get("path", "unknown")),
                skip_summary=skip_summary,
            )
            parts.append({"type": "text", "text": note})
        return parts if parts else text

    note_lines: List[str] = []
    if text:
        note_lines.append(text)
    for att in attachments:
        note = image_summary_note(
            att["path"],
            att["mime_type"],
            att.get("uri", att.get("path", "unknown")),
            skip_summary=skip_summary,
        )
        note_lines.append(note)
    return "\n".join(note_lines)
