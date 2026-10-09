"""Multi-language resolution and normalization utilities.

Follows the lingua-franca fallback chain:
1. target_lang (if present and non-empty)
2. 'en' (English as lingua franca, if target_lang != 'en')
3. 'ja' (Japanese as base language)
4. Any first available non-empty translation
5. fallback default string
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, Union


def normalize_i18n_dict(
    value: Union[str, Dict[str, Any], None],
    alt_en: Optional[str] = None,
    alt_ja: Optional[str] = None,
) -> Dict[str, str]:
    """Normalize a multi-language value (string, dict, or None) into a standard Dict[str, str]."""
    result: Dict[str, str] = {}

    if isinstance(value, dict):
        for k, v in value.items():
            if v is not None:
                str_v = str(v).strip()
                if str_v:
                    result[str(k).strip()] = str_v
    elif isinstance(value, str) and value.strip():
        # Single string value is treated as Japanese (system default base) unless specified
        result["ja"] = value.strip()

    if alt_ja and alt_ja.strip() and "ja" not in result:
        result["ja"] = alt_ja.strip()

    if alt_en and alt_en.strip() and "en" not in result:
        result["en"] = alt_en.strip()

    return result


def split_i18n_columns(
    value: Union[str, Dict[str, Any], None],
    alt_en: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Split a multi-language value into ``(base, en)`` for a pair of DB string columns.

    Tables such as ``playbooks`` store a localized field as two plain string
    columns (e.g. ``display_name`` + ``display_name_en``): the base column holds
    the Japanese text (or the plain string as-is), the ``_en`` column holds
    English. Readers recombine them with :func:`normalize_i18n_dict`. A dict
    must never reach the column itself — SQLite cannot bind it.
    """
    norm = normalize_i18n_dict(value, alt_en=alt_en)
    return norm.get("ja"), norm.get("en")


def resolve_i18n_text(
    value: Union[str, Dict[str, Any], None],
    target_lang: str = "ja",
    alt_en: Optional[str] = None,
    alt_ja: Optional[str] = None,
    default: str = "",
) -> str:
    """Resolve localized string based on target_lang with `target -> en -> ja` fallback chain."""
    norm = normalize_i18n_dict(value, alt_en=alt_en, alt_ja=alt_ja)

    if not norm:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    # 1. Target language match
    val = norm.get(target_lang)
    if val and val.strip():
        return val.strip()

    # 2. English (lingua franca) fallback when target_lang is not English
    if target_lang != "en":
        en_val = norm.get("en")
        if en_val and en_val.strip():
            return en_val.strip()

    # 3. Japanese (base language) fallback
    ja_val = norm.get("ja")
    if ja_val and ja_val.strip():
        return ja_val.strip()

    # 4. Any first available translation
    for v in norm.values():
        if v and v.strip():
            return v.strip()

    return default
