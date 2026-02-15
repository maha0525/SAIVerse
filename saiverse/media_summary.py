from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .media_utils import (
    get_media_summary,
    save_media_summary,
)

LOGGER = logging.getLogger(__name__)

# Model config key or API model name for summary generation (vision-capable model required for images)
_IMAGE_SUMMARY_MODEL_RAW = os.getenv("SAIVERSE_IMAGE_SUMMARY_MODEL", "gemini-2.5-flash-lite-preview-09-2025")

# Cached client
_summary_client: Any = None
_summary_model_key: Optional[str] = None


def _get_summary_client() -> Any:
    """Get or create the LLM client for summary generation.

    Uses the standard LLM client factory so any configured provider
    (Gemini, OpenAI, Anthropic, etc.) can be used.
    """
    global _summary_client, _summary_model_key

    if _summary_client is not None and _summary_model_key == _IMAGE_SUMMARY_MODEL_RAW:
        return _summary_client

    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    config_key, config = find_model_config(_IMAGE_SUMMARY_MODEL_RAW)
    if not config:
        LOGGER.warning(
            "Image summary model '%s' not found in model configs; "
            "falling back to 'gemini-2.5-flash-lite-preview-09-2025'",
            _IMAGE_SUMMARY_MODEL_RAW,
        )
        config_key, config = find_model_config("gemini-2.5-flash-lite-preview-09-2025")
        if not config:
            LOGGER.error(
                "Fallback model 'gemini-2.5-flash-lite-preview-09-2025' also not found in model configs"
            )
            return None

    provider = config.get("provider", "gemini")
    context_length = config.get("context_length", 128000)

    try:
        client = get_llm_client(config_key, provider, context_length, config)
        _summary_client = client
        _summary_model_key = _IMAGE_SUMMARY_MODEL_RAW
        LOGGER.info(
            "Image summary client created: config_key=%s, api_model=%s, provider=%s",
            config_key,
            config.get("model", config_key),
            provider,
        )
        return client
    except Exception:
        LOGGER.exception("Failed to create image summary client for '%s'", _IMAGE_SUMMARY_MODEL_RAW)
        return None


def invalidate_summary_client() -> None:
    """Reset the cached summary client (e.g. after API key changes)."""
    global _summary_client, _summary_model_key
    _summary_client = None
    _summary_model_key = None
    LOGGER.info("Image summary client cache invalidated")


def ensure_image_summary(path: Path, mime_type: str) -> Optional[str]:
    """Ensure an image summary exists; generate if missing."""
    summary = get_media_summary(path)
    if summary:
        return summary
    generated = _generate_image_summary(path, mime_type)
    if generated:
        save_media_summary(path, generated)
        return generated
    return None


def ensure_document_summary(path: Path) -> Optional[str]:
    """Ensure a document summary exists; generate if missing."""
    summary = get_media_summary(path)
    if summary:
        return summary
    generated = _generate_document_summary(path)
    if generated:
        save_media_summary(path, generated)
        return generated
    return None


def _generate_image_summary(path: Path, mime_type: str) -> Optional[str]:
    client = _get_summary_client()
    if client is None:
        return None

    if not client.supports_images:
        LOGGER.warning(
            "Image summary model '%s' does not support images; cannot summarize %s",
            _IMAGE_SUMMARY_MODEL_RAW,
            path,
        )
        return None

    prompt_text = (
        "以下の画像を詳しく説明するのではなく、内容を理解するための要点を"
        "300文字以内の日本語で1〜2文にまとめてください。"
    )
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": prompt_text,
            "metadata": {
                "media": [
                    {
                        "path": str(path),
                        "mime_type": mime_type,
                        "uri": str(path),
                    },
                ],
            },
        },
    ]

    try:
        result = client.generate(messages, temperature=0.2)
        if isinstance(result, str) and result.strip():
            return result.strip()
        LOGGER.warning("Image summary generation returned empty result for %s", path)
    except Exception:
        LOGGER.exception("Image summary generation failed for %s", path)
    return None


def _generate_document_summary(path: Path) -> Optional[str]:
    client = _get_summary_client()
    if client is None:
        return None

    try:
        document_text = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.exception("Failed to read document for summary: %s", path)
        return None

    prompt_text = (
        "以下の文書の内容を300文字以内の日本語で要約してください。"
        "要点を簡潔にまとめてください。\n\n"
        f"{document_text}"
    )
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": prompt_text},
    ]

    try:
        result = client.generate(messages, temperature=0.2)
        if isinstance(result, str) and result.strip():
            return result.strip()
        LOGGER.warning("Document summary generation returned empty result for %s", path)
    except Exception:
        LOGGER.exception("Document summary generation failed for %s", path)
    return None
