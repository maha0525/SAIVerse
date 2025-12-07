from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from media_utils import (
    get_media_summary,
    load_image_bytes_for_llm,
    save_media_summary,
)

try:  # pragma: no cover - optional dependency
    from google.genai import types  # type: ignore
except ImportError:  # pragma: no cover
    types = None  # type: ignore

from llm_clients.gemini_utils import build_gemini_clients

LOGGER = logging.getLogger(__name__)


def ensure_image_summary(path: Path, mime_type: str) -> Optional[str]:
    """Ensure an image summary exists; generate with Gemini if missing."""
    summary = get_media_summary(path)
    if summary:
        return summary
    generated = _generate_image_summary(path, mime_type)
    if generated:
        save_media_summary(path, generated)
        return generated
    return None


def ensure_document_summary(path: Path) -> Optional[str]:
    """Ensure a document summary exists; generate with Gemini if missing."""
    summary = get_media_summary(path)
    if summary:
        return summary
    generated = _generate_document_summary(path)
    if generated:
        save_media_summary(path, generated)
        return generated
    return None


def _generate_image_summary(path: Path, mime_type: str) -> Optional[str]:
    if types is None:
        LOGGER.warning("Gemini SDK not available; cannot summarize image %s", path)
        return None
    try:
        free_client, paid_client, active_client = build_gemini_clients()
    except RuntimeError as exc:
        LOGGER.warning("Cannot initialise Gemini client for image summary: %s", exc)
        return None

    data, effective_mime = load_image_bytes_for_llm(path, mime_type)
    if not data or not effective_mime:
        LOGGER.warning("Image bytes unavailable for summary generation: %s", path)
        return None

    prompt_text = (
        "以下の画像を詳しく説明するのではなく、内容を理解するための要点を300文字以内の日本語で1〜2文にまとめてください。"
    )
    content = [
        types.Content(
            parts=[
                types.Part(text=prompt_text),
                types.Part.from_bytes(data=data, mime_type=effective_mime),
            ],
            role="user",
        )
    ]
    config = types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=256,
    )

    clients = [active_client, paid_client, free_client]
    for client in clients:
        if client is None:
            continue
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=content,
                config=config,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning("Gemini image summary generation failed: %s", exc)
            continue
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        # Some SDK responses place text under candidates[0].content.parts
        try:
            candidates = getattr(response, "candidates", [])
            for candidate in candidates or []:
                parts = getattr(candidate, "content", None)
                if parts and getattr(parts, "parts", None):
                    for part in parts.parts:
                        value = getattr(part, "text", None)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("Failed to parse Gemini summary response", exc_info=True)
    return None


def _generate_document_summary(path: Path) -> Optional[str]:
    """Generate a summary for a text document using Gemini."""
    if types is None:
        LOGGER.warning("Gemini SDK not available; cannot summarize document %s", path)
        return None
    try:
        free_client, paid_client, active_client = build_gemini_clients()
    except RuntimeError as exc:
        LOGGER.warning("Cannot initialise Gemini client for document summary: %s", exc)
        return None

    try:
        document_text = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.exception("Failed to read document for summary generation: %s", path)
        return None

    prompt_text = (
        "以下の文書の内容を300文字以内の日本語で要約してください。要点を簡潔にまとめてください。\n\n"
        f"{document_text}"
    )
    content = [
        types.Content(
            parts=[types.Part(text=prompt_text)],
            role="user",
        )
    ]
    config = types.GenerateContentConfig(
        temperature=0.2,
        max_output_tokens=256,
    )

    clients = [active_client, paid_client, free_client]
    for client in clients:
        if client is None:
            continue
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=content,
                config=config,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            LOGGER.warning("Gemini document summary generation failed: %s", exc)
            continue
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        # Some SDK responses place text under candidates[0].content.parts
        try:
            candidates = getattr(response, "candidates", [])
            for candidate in candidates or []:
                parts = getattr(candidate, "content", None)
                if parts and getattr(parts, "parts", None):
                    for part in parts.parts:
                        value = getattr(part, "text", None)
                        if isinstance(value, str) and value.strip():
                            return value.strip()
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("Failed to parse Gemini summary response", exc_info=True)
    return None
