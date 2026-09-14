"""Language settings and prompt instructions for personas and cities."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

LOGGER = logging.getLogger(__name__)
LANGUAGES = {"ja": "日本語 (Japanese)", "en": "English"}
DEFAULT_LANGUAGE = "ja"


def validate_language(language: str) -> str:
    """Validate that language code is supported."""
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported language: {language!r}")
    return language


def language_instruction(language: str) -> str:
    """Generate prompt instruction ensuring persona speaks and writes in target language.
    Returns empty string for default language ('ja') to avoid polluting prompt cache.
    """
    if not language or language == DEFAULT_LANGUAGE:
        return ""
    name = LANGUAGES.get(language) or LANGUAGES[DEFAULT_LANGUAGE]
    return (
        f"## Language of your life: {name}\n"
        f"Write your replies, private thoughts, diary, Chronicle summaries and "
        f"Memopedia prose in {name}, regardless of the language of these shared instructions. "
        "Preserve original quotations, proper names, identifiers, tool arguments and JSON keys. "
        "Do not translate or rewrite existing memories. Follow an explicit request to use "
        "another language for a particular response."
    )


def _get_db_path(db_path: Path | str | None = None) -> Path | None:
    if db_path is not None:
        p = Path(db_path)
        return p if p.is_file() else None
    try:
        from database.paths import default_db_path
        p = Path(default_db_path())
        return p if p.is_file() else None
    except Exception:
        return None


def get_city_language(city_id: int | None, db_path: Path | str | None = None) -> str:
    """Get language configured for a city. Defaults to 'ja'."""
    if city_id is None:
        return DEFAULT_LANGUAGE
    resolved_path = _get_db_path(db_path)
    if resolved_path is None:
        return DEFAULT_LANGUAGE

    try:
        with closing(sqlite3.connect(resolved_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(city)')}
            if "LANGUAGE" not in columns:
                return DEFAULT_LANGUAGE
            row = conn.execute('SELECT LANGUAGE FROM city WHERE CITYID = ?', (city_id,)).fetchone()
            if row and row[0]:
                return validate_language(row[0])
    except Exception as e:
        LOGGER.debug("Failed to read city language for city_id=%s: %s", city_id, e)
    return DEFAULT_LANGUAGE


def get_persona_language(persona_id: str | None, db_path: Path | str | None = None) -> str:
    """Get language configured for a persona.

    Falls back to home city language, then to 'ja'.
    Never creates a world DB when called by standalone tools.
    """
    if not persona_id:
        return DEFAULT_LANGUAGE
    resolved_path = _get_db_path(db_path)
    if resolved_path is None:
        return DEFAULT_LANGUAGE

    try:
        with closing(sqlite3.connect(resolved_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(ai)')}
            if "LANGUAGE" not in columns:
                return DEFAULT_LANGUAGE
            row = conn.execute(
                'SELECT LANGUAGE, HOME_CITYID FROM ai WHERE AIID = ?', (persona_id,)
            ).fetchone()
            if not row:
                return DEFAULT_LANGUAGE
            persona_lang, home_city_id = row[0], row[1]
            if persona_lang:
                return validate_language(persona_lang)
            # If persona has no explicit language set, inherit from home city
            if home_city_id is not None:
                city_cols = {r[1] for r in conn.execute('PRAGMA table_info(city)')}
                if "LANGUAGE" in city_cols:
                    city_row = conn.execute(
                        'SELECT LANGUAGE FROM city WHERE CITYID = ?', (home_city_id,)
                    ).fetchone()
                    if city_row and city_row[0]:
                        return validate_language(city_row[0])
    except Exception as e:
        LOGGER.debug("Failed to read persona language for persona_id=%s: %s", persona_id, e)
    return DEFAULT_LANGUAGE


def memory_language_messages(
    messages: list, persona_id: str | None, db_path: Path | str | None = None
) -> list:
    """Prepend language instruction to memory generation messages for non-default languages."""
    language = get_persona_language(persona_id, db_path=db_path)
    instruction = language_instruction(language)
    if not instruction:
        return messages
    LOGGER.debug("Memory generation language: persona=%s language=%s", persona_id, language)
    return [{"role": "system", "content": instruction}, *messages]
