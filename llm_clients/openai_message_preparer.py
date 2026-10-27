"""Helpers to prepare OpenAI-compatible chat messages."""
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm

from .utils import (
    attachment_limit_is_off,
    content_to_text,
    image_summary_note,
    parse_attachment_limit,
    select_embedded_attachment_keys,
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
    max_image_embeds_override: Optional[int] = None,
    *,
    supports_images: bool = False,
    max_image_bytes: Optional[int] = None,
) -> Tuple[Dict[int, List[Dict[str, Any]]], Set[int], Set[int], Optional[Set[Tuple[int, int]]]]:
    """Build metadata-derived caches for attachment and summary handling.

    ``supports_images`` / ``max_image_bytes`` are the client's own values; the
    byte valve (llm_clients/utils.select_embedded_attachment_keys) only counts
    images that build_message_content_with_attachments actually embeds
    (user messages of an image-capable model), so with ``supports_images=False``
    nothing is measured.
    """
    max_image_embeds = max_image_embeds_override if max_image_embeds_override is not None else parse_attachment_limit("OPENAI")
    # リアルタイム情報と自動想起を残すモデルの列では上限を使わない
    # (llm_clients/utils.ATTACHMENT_LIMIT_OFF_KEY。モデル設定の上限より優先)。
    if attachment_limit_is_off(messages):
        max_image_embeds = None
    attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
    skip_summary_indices: Set[int] = set()
    exempt_indices: Set[int] = set()
    # 画像を実際に埋め込むメッセージ (build_message_content_with_attachments と
    # 同じ判定: 画像対応のモデルの user。host は system になり埋め込まない)。
    embeddable_indices: Set[int] = set()

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
            if supports_images and msg.get("role") == "user":
                embeddable_indices.add(idx)

    allowed_attachment_keys = select_embedded_attachment_keys(
        attachment_cache, max_image_embeds, exempt_indices,
        provider="OPENAI",
        messages=messages,
        embeddable_indices=embeddable_indices,
        max_image_bytes=max_image_bytes,
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
    notice_metadata: Any = None,
    unresolved_notice_images: Optional[List[Dict[str, Any]]] = None,
) -> Any:
    """Build content with image embedding or textual summaries.

    ``notice_metadata`` is set (to the message metadata) for a system notice
    tagged into a user message (llm_clients/system_notice.is_presented_system_notice).
    Its images that are not embedded become
    :func:`llm_clients.system_notice.system_notice_image_note` (stored summary
    or URI only, never generated on the spot), and ``unresolved_notice_images``
    (images whose file cannot be read) are described the same way instead of
    disappearing.
    """
    unresolved = list(unresolved_notice_images or [])
    if not attachments and not unresolved:
        return original_content

    is_notice = notice_metadata is not None
    if is_notice:
        from .system_notice import system_notice_image_note

    def _note_for(att: Dict[str, Any]) -> str:
        if is_notice:
            return system_notice_image_note(att, notice_metadata)
        return image_summary_note(
            att["path"],
            att["mime_type"],
            att.get("uri", att.get("path", "unknown")),
            skip_summary=skip_summary,
        )

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
                    magic = data[:8].hex()
                    logging.debug(
                        "[openai_prep] image att=%s orig_mime=%s effective_mime=%s "
                        "raw_bytes=%d b64_len=%d magic=%s",
                        att.get("uri") or att.get("path"),
                        att.get("mime_type"),
                        effective_mime,
                        len(data),
                        len(b64),
                        magic,
                    )
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
            parts.append({"type": "text", "text": _note_for(att)})
        for entry in unresolved:
            parts.append({"type": "text", "text": _note_for(entry)})
        return parts if parts else text

    note_lines: List[str] = []
    if text:
        note_lines.append(text)
    for att in attachments:
        note_lines.append(_note_for(att))
    for entry in unresolved:
        note_lines.append(_note_for(entry))
    return "\n".join(note_lines)


def prepare_openai_messages(
    messages: List[Any],
    *,
    supports_images: bool,
    max_image_bytes: Optional[int] = None,
    max_image_embeds: Optional[int] = None,
    convert_system_to_user: bool = False,
    reasoning_passback_field: Optional[str] = None,
    keep_metadata_keys: Tuple[str, ...] = (),
    convert_system_notices: Optional[bool] = None,
) -> List[Any]:
    """Prepare messages for OpenAI-compatible APIs by removing internal-only fields.

    ``keep_metadata_keys``: metadata keys a caller still needs after
    preparation (the result keeps ``{"metadata": {key: value}}`` with only
    those keys). Empty by default, so the result carries no metadata.

    System messages (docs/intent/system_notice_delivery.md):

    - A list that carries the head's system message (metadata
      ``HEAD_MESSAGE_KEY``) goes through
      :func:`llm_clients.system_notice.present_system_notices` first. The head's
      system message stays a system message; later role=system notices become
      passphrase-tagged user messages in place (keeping their media) when
      ``convert_system_notices`` is true, and stay system messages otherwise.
      ``convert_system_notices`` defaults to ``convert_system_to_user`` (the
      model config flag: true = the model cannot take a mid-conversation
      system message). The Codex client passes ``convert_system_to_user=False``
      with ``convert_system_notices=True``: it lifts the system messages into
      ``instructions`` itself, but the notices must stay in place.
    - A list without the head's system message (summaries, judgments and other
      calls that build their own prompt) keeps the older behaviour:
      ``convert_system_to_user`` turns system messages that follow a non-system
      message into ``<system>``-tagged user messages, leading ones stay.

    Options are keyword-only: several neighbours accept None/bool, so a positional
    call that skips one shifts the rest without any error (NIM once passed
    convert_system_to_user into max_image_embeds, which embedded zero images).
    """
    from .system_notice import (
        is_head_system_message,
        is_presented_system_notice,
        present_system_notices,
        system_notice_image_entries,
        system_notice_indices,
        unresolved_system_notice_images,
        with_system_notice_image_notes,
    )

    # system のまま送る途中のシステム通知の位置 (画像を説明の文に置き換える)。
    direct_notice_indices: Set[int] = set()
    if any(is_head_system_message(m) for m in messages):
        convert_notices = (
            convert_system_to_user if convert_system_notices is None
            else convert_system_notices
        )
        messages = present_system_notices(
            messages, accepts_mid_system=not convert_notices,
        )
        if not convert_notices:
            direct_notice_indices = system_notice_indices(messages)
        # 途中の通知は上で扱い済み。残る system は head とその前の指示だけ
        # なので、位置による変換は掛けない。
        convert_system_to_user = False

    attachment_cache, skip_summary_indices, _, allowed_attachment_keys = scan_message_metadata(
        messages, max_image_embeds_override=max_image_embeds,
        supports_images=supports_images,
        max_image_bytes=max_image_bytes,
    )

    prepared: List[Any] = []
    seen_non_system = False

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            prepared.append(msg)
            continue

        attachments = attachment_cache.get(idx, [])
        # タグ化した通知の画像のうち、ファイルが読めないもの (下で説明の文にする)
        unresolved_notice_images: List[Dict[str, Any]] = []
        notice_metadata: Any = None
        if idx in direct_notice_indices:
            if system_notice_image_entries(msg.get("metadata")):
                # system のまま送る通知は文字しか持てない。画像は空判定より前に
                # 説明の文として本文へ足す — 本文が空で画像だけの通知も、画像の
                # ファイルが消えた通知も落とさない。説明は保存済みの要約か URI
                # だけで、その場では生成しない (生成の成否で送信本文が揺れない
                # ように — llm_clients/system_notice.py の system_notice_image_note)。
                msg = {
                    **msg,
                    "content": with_system_notice_image_notes(
                        content_to_text(msg.get("content") or ""), msg.get("metadata"),
                    ),
                }
            attachments = []
        elif is_presented_system_notice(msg):
            notice_metadata = msg.get("metadata")
            unresolved_notice_images = unresolved_system_notice_images(
                notice_metadata, attachments,
            )

        # 本文の空の user でも、画像を持っていれば画像だけの発言として残す
        # (画像か画像の説明が下の build_message_content_with_attachments で入る)
        keeps_images = msg.get("role") == "user" and bool(
            attachments or unresolved_notice_images
        )

        if is_empty_message(msg) and not keeps_images:
            logging.debug("Skipping empty message with role=%s", msg.get("role"))
            continue

        normalized = normalize_message_role(
            msg.get("role"),
            convert_system_to_user,
            seen_non_system,
            msg.get("content"),
        )
        role = normalized[0] if normalized else None
        converted_content = normalized[1] if normalized else None

        metadata = msg.get("metadata")
        skip_summary = idx in skip_summary_indices

        clean_msg: Dict[str, Any] = {}
        for field in ALLOWED_FIELDS:
            if field in msg:
                clean_msg[field] = msg[field]

        if role:
            clean_msg["role"] = role

        if reasoning_passback_field and role == "assistant":
            rd = (metadata or {}).get("reasoning_details") if isinstance(metadata, dict) else None
            if isinstance(rd, list):
                clean_msg[reasoning_passback_field] = rd

        if keep_metadata_keys and isinstance(metadata, dict):
            kept = {key: metadata[key] for key in keep_metadata_keys if key in metadata}
            if kept:
                clean_msg["metadata"] = kept

        if is_empty_message(clean_msg) and not keeps_images:
            logging.debug("Skipping empty cleaned message with role=%s", role)
            continue

        if converted_content is not None:
            clean_msg["content"] = converted_content
            prepared.append(clean_msg)
            continue

        if role != "system":
            seen_non_system = True

        if not attachments and not unresolved_notice_images:
            prepared.append(clean_msg)
            continue

        clean_msg["content"] = build_message_content_with_attachments(
            role=role,
            original_content=msg.get("content"),
            attachments=attachments,
            supports_images=supports_images,
            max_image_bytes=max_image_bytes,
            skip_summary=skip_summary,
            allowed_attachment_keys=allowed_attachment_keys,
            message_index=idx,
            notice_metadata=notice_metadata,
            unresolved_notice_images=unresolved_notice_images,
        )
        prepared.append(clean_msg)

    return prepared
