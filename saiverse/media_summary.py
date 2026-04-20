from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .media_utils import (
    get_media_summary,
    save_media_summary,
)

LOGGER = logging.getLogger(__name__)

# Re-entrancy guard: tracks image paths currently being summarised
# to prevent infinite recursion when the summary LLM client itself
# triggers ensure_image_summary() via _convert_messages().
_generating_lock = threading.Lock()
_generating_paths: Set[str] = set()

# Model config key or API model name for summary generation (vision-capable model required for images)
from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL

_IMAGE_SUMMARY_MODEL_RAW = os.getenv("SAIVERSE_IMAGE_SUMMARY_MODEL", BUILTIN_DEFAULT_LITE_MODEL)

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
            "falling back to '%s'",
            _IMAGE_SUMMARY_MODEL_RAW,
            BUILTIN_DEFAULT_LITE_MODEL,
        )
        config_key, config = find_model_config(BUILTIN_DEFAULT_LITE_MODEL)
        if not config:
            LOGGER.error(
                "Fallback model '%s' also not found in model configs",
                BUILTIN_DEFAULT_LITE_MODEL,
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
    """Ensure an image summary exists; generate if missing.

    Includes a re-entrancy guard so that summary generation requests
    (which themselves contain the image) do not trigger another round
    of summary generation, which would otherwise cause infinite recursion.
    """
    summary = get_media_summary(path)
    if summary:
        return summary

    path_key = str(path)
    with _generating_lock:
        if path_key in _generating_paths:
            LOGGER.debug(
                "Skipping image summary for %s (already generating — re-entrancy guard)",
                path,
            )
            return None
        _generating_paths.add(path_key)

    try:
        generated = _generate_image_summary(path, mime_type)
        if generated:
            save_media_summary(path, generated)
            return generated
        return None
    finally:
        with _generating_lock:
            _generating_paths.discard(path_key)


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
                # Signal to LLM clients: do NOT call ensure_image_summary()
                # on images in this message.  This prevents infinite recursion
                # (summary request → _convert_messages → ensure_image_summary
                #  → summary request → …).
                "__skip_image_summary__": True,
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


def generate_contextual_image_description(
    path: Path, mime_type: str, user_message: str, prev_ai_message: str
) -> Optional[str]:
    """Generate a description for an uploaded image using surrounding conversation context.

    Saves the result to the .summary.txt file so that ensure_image_summary()
    won't regenerate a context-free description afterwards.
    """
    client = _get_summary_client()
    if client is None:
        return None

    if not client.supports_images:
        LOGGER.warning(
            "Image summary model '%s' does not support images; cannot generate contextual description for %s",
            _IMAGE_SUMMARY_MODEL_RAW,
            path,
        )
        return None

    context_parts: List[str] = []
    if prev_ai_message:
        context_parts.append(f"【直前のAI発言】{prev_ai_message[:300]}")
    if user_message:
        context_parts.append(f"【ユーザーのメッセージ】{user_message[:300]}")

    context_text = "\n".join(context_parts)
    if context_text:
        prompt_text = (
            "あなたはファイル管理システムのメタデータ生成エンジンです。"
            "添付画像の内容を、客観的・中立的な立場で300文字以内の日本語で説明してください。\n"
            "ルール:\n"
            "- 一人称（私、僕など）を使わない\n"
            "- 感情や意見を含めない\n"
            "- 画像に何が写っているか、何を示しているかを端的に記述する\n"
            "- 以下の会話文脈は「何についての画像か」を判断する補助情報としてのみ使用する\n\n"
            f"{context_text}"
        )
    else:
        prompt_text = (
            "あなたはファイル管理システムのメタデータ生成エンジンです。"
            "添付画像の内容を、客観的・中立的な立場で300文字以内の日本語で説明してください。"
            "一人称や感情表現は使わず、何が写っているかを端的に記述してください。"
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
                "__skip_image_summary__": True,
            },
        },
    ]

    try:
        result = client.generate(messages, temperature=0.2)
        if isinstance(result, str) and result.strip():
            generated = result.strip()
            # Save to .summary.txt so ensure_image_summary() won't regenerate
            save_media_summary(path, generated)
            return generated
        LOGGER.warning("Contextual image description returned empty result for %s", path)
    except Exception:
        LOGGER.exception("Contextual image description generation failed for %s", path)
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
