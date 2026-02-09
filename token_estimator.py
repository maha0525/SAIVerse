"""
Token estimation utilities for context window management.

Provides heuristic-based token count estimation for text and images
across different LLM providers. These are approximations used for
pre-flight context budget checks, not exact counts.
"""

import unicodedata


def _is_cjk(char: str) -> bool:
    """Check if a character is CJK (Chinese/Japanese/Korean)."""
    try:
        block = unicodedata.name(char, "")
    except ValueError:
        return False
    return any(
        keyword in block
        for keyword in (
            "CJK",
            "HIRAGANA",
            "KATAKANA",
            "HANGUL",
            "IDEOGRAPH",
        )
    )


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a text string.

    Heuristics:
    - CJK characters: ~1.5 tokens per character
    - ASCII/Latin characters: ~0.25 tokens per character (4 chars/token)
    """
    if not text:
        return 0
    cjk_count = 0
    other_count = 0
    for ch in text:
        if _is_cjk(ch):
            cjk_count += 1
        else:
            other_count += 1
    return int(cjk_count * 1.5 + other_count * 0.25)


def estimate_image_tokens(provider: str) -> int:
    """Estimate token cost of a single image for a given provider.

    Uses fixed estimates to avoid I/O overhead of reading image dimensions.
    These are conservative averages for typical chat images.

    - OpenAI high-detail: ~765 tokens (assumes ~2 tiles)
    - Anthropic: ~1600 tokens (assumes ~1000x1200 image)
    - Gemini: ~258 tokens (768px = 1 tile)
    """
    estimates = {
        "openai": 765,
        "anthropic": 1600,
        "gemini": 258,
    }
    return estimates.get(provider, 500)


def _count_images_in_message(msg: dict) -> int:
    """Count the number of images attached to a message."""
    metadata = msg.get("metadata") or {}
    media_list = metadata.get("media") or []
    return sum(1 for m in media_list if m.get("type") == "image")


def estimate_messages_tokens(messages: list, provider: str) -> int:
    """Estimate total token count for a list of LLM messages.

    Accounts for:
    - Text content (CJK-aware)
    - Image attachments (provider-specific estimates)
    - Per-message overhead (~4 tokens for role/formatting)
    """
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        total += estimate_text_tokens(part.get("text", ""))
                    elif part.get("type") in ("image_url", "image"):
                        total += estimate_image_tokens(provider)
                elif isinstance(part, str):
                    total += estimate_text_tokens(part)

        # Image attachments in metadata
        total += _count_images_in_message(msg) * estimate_image_tokens(provider)

        # Per-message overhead (role, formatting tokens)
        total += 4

    return total
