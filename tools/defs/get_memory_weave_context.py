"""Build Memory Weave context for LLM with Chronicle (Arasuji) and Memopedia."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# Marker to identify Memory Weave context messages
MEMORY_WEAVE_CONTEXT_MARKER = "__memory_weave_context__"


def get_memory_weave_context(
    *,
    persona_id: Optional[str] = None,
    persona_dir: Optional[str] = None,
    max_chronicle_entries: int = 50,
) -> List[Dict[str, Any]]:
    """Build Memory Weave context messages containing Chronicle and Memopedia.

    This provides the persona with:
    - Chronicle: Recent events in detail, older events in summary (hierarchical)
    - Memopedia: Page titles, keywords, and summaries (semantic memory)

    The context is inserted after the system prompt but before visual context
    and conversation history.

    Args:
        persona_id: Persona ID (auto-detected if not provided)
        persona_dir: Persona directory path (auto-detected if not provided)
        max_chronicle_entries: Maximum number of Chronicle entries to include

    Returns:
        List of messages to insert into context.
        Returns empty list if Memory Weave is not available.
    """
    # Check environment variable
    env_value = os.getenv("ENABLE_MEMORY_WEAVE_CONTEXT", "false")
    LOGGER.info("get_memory_weave_context: ENABLE_MEMORY_WEAVE_CONTEXT=%s", env_value)
    if env_value.lower() != "true":
        LOGGER.debug("get_memory_weave_context: Disabled by environment variable")
        return []

    # Get persona context
    from tools.context import get_active_persona_id, get_active_persona_path

    if persona_id is None:
        persona_id = get_active_persona_id()
    if not persona_id:
        LOGGER.debug("get_memory_weave_context: No active persona")
        return []

    # Try to get persona_dir from context if not provided
    if persona_dir is None:
        try:
            path_obj = get_active_persona_path()
            persona_dir = str(path_obj) if path_obj else None
        except Exception:
            pass
    
    if not persona_dir:
        LOGGER.debug("get_memory_weave_context: No persona dir")
        return []

    # Find memory.db
    memory_db_path = Path(persona_dir) / "memory.db"
    if not memory_db_path.exists():
        LOGGER.debug("get_memory_weave_context: memory.db not found at %s", memory_db_path)
        return []

    try:
        conn = sqlite3.connect(str(memory_db_path))
        context_parts: List[str] = []

        # 1. Get Chronicle context (hierarchical episode memory)
        chronicle_text = _get_chronicle_context(conn, max_entries=max_chronicle_entries)
        if chronicle_text:
            context_parts.append("## これまでの出来事（Chronicle）\n\n" + chronicle_text)

        # 2. Get Memopedia context (semantic memory)
        memopedia_text = _get_memopedia_context(conn)
        LOGGER.info("get_memory_weave_context: Memopedia text length=%d", len(memopedia_text))
        if memopedia_text:
            context_parts.append("## 記憶ベース（Memopedia）\n\n" + memopedia_text)
        else:
            LOGGER.warning("get_memory_weave_context: Memopedia context is empty")

        conn.close()

        if not context_parts:
            return []

        # Build message with marker for special handling
        content = "\n\n---\n\n".join(context_parts)
        message = {
            "role": "user",
            "content": f"以下は、あなたの長期記憶です。この情報を参考に会話してください。\n\n{content}",
            "metadata": {MEMORY_WEAVE_CONTEXT_MARKER: True},
        }

        LOGGER.info(
            "get_memory_weave_context: Generated context (%d chars)",
            len(content)
        )
        return [message]

    except Exception as exc:
        LOGGER.warning("get_memory_weave_context: Failed to build context: %s", exc)
        return []


def _get_chronicle_context(conn: sqlite3.Connection, max_entries: int = 50) -> str:
    """Get Chronicle (Arasuji) context using hierarchical algorithm."""
    try:
        from sai_memory.arasuji.context import get_episode_context, format_episode_context
        
        context = get_episode_context(conn, max_entries=max_entries)
        if not context:
            return ""
        
        return format_episode_context(context, include_level_info=True)
    except ImportError:
        LOGGER.debug("Chronicle module not available")
        return ""
    except Exception as exc:
        LOGGER.warning("Failed to get Chronicle context: %s", exc)
        return ""


def _get_memopedia_context(conn: sqlite3.Connection) -> str:
    """Get Memopedia context (page titles, keywords, summaries)."""
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables
        
        # Initialize tables if needed
        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        
        tree = memopedia.get_tree()
        LOGGER.info("_get_memopedia_context: tree keys=%s", list(tree.keys()))
        lines: List[str] = []

        category_names = {
            "people": "人物",
            "terms": "用語",
            "plans": "予定",
        }

        def _list_pages(pages: List[Dict], prefix: str = "") -> None:
            for page in pages:
                # Skip root pages
                if not page["id"].startswith("root_"):
                    keywords = page.get("keywords", [])
                    if keywords:
                        kw_str = f" [キーワード: {', '.join(keywords)}]"
                    else:
                        kw_str = ""
                    lines.append(f"{prefix}- {page['title']}: {page['summary']}{kw_str}")
                children = page.get("children", [])
                if children:
                    _list_pages(children, prefix + "  ")

        for category in ["people", "terms", "plans"]:
            pages = tree.get(category, [])
            LOGGER.debug("_get_memopedia_context: category=%s, pages count=%d", category, len(pages))
            if pages:
                lines.append(f"\n### {category_names[category]}")
                _list_pages(pages)

        LOGGER.info("_get_memopedia_context: Generated %d lines", len(lines))
        if not lines:
            return ""

        return "\n".join(lines)
    except ImportError:
        LOGGER.debug("Memopedia module not available")
        return ""
    except Exception as exc:
        LOGGER.warning("Failed to get Memopedia context: %s", exc)
        return ""


# Tool definition for registry (optional, mainly used via runtime.py)
TOOL_DEF = {
    "name": "get_memory_weave_context",
    "description": "Build Memory Weave context containing Chronicle and Memopedia for LLM context.",
    "parameters": {
        "type": "object",
        "properties": {
            "max_chronicle_entries": {
                "type": "integer",
                "description": "Maximum number of Chronicle entries to include. Default: 50.",
            },
        },
    },
}
