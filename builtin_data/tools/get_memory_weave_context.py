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
    memopedia_index_limit: int = 100,
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
            LOGGER.warning("Failed to get active persona path", exc_info=True)
    
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

        # 1. Get Chronicle context (hierarchical episode memory)
        chronicle_text = _get_chronicle_context(conn, max_entries=max_chronicle_entries)

        # 1.5. Get Track Chronicle context for active track (v0.32, 2026-05-09)
        # アクティブ Track が無いペルソナや track_manager 利用不可な環境では空文字
        track_chronicle_text, track_title = _get_track_chronicle_context(
            conn, persona_id, max_entries=max_chronicle_entries
        )

        # 2. Get Memopedia context (semantic memory)
        memopedia_text = _get_memopedia_context(conn, index_limit=memopedia_index_limit)
        LOGGER.info("get_memory_weave_context: Memopedia text length=%d", len(memopedia_text))
        if not memopedia_text:
            LOGGER.warning("get_memory_weave_context: Memopedia context is empty")

        conn.close()

        # Build separate messages for Chronicle / Track Chronicle / Memopedia
        # so the context preview can show token breakdown per source
        messages: List[Dict[str, Any]] = []
        if chronicle_text:
            messages.append({
                "role": "user",
                "content": f"以下は、あなたの長期記憶（Chronicle: これまでの出来事）です。\n\n{chronicle_text}",
                "metadata": {
                    MEMORY_WEAVE_CONTEXT_MARKER: True,
                    "__memory_weave_type__": "chronicle",
                },
            })
        if track_chronicle_text:
            messages.append({
                "role": "user",
                "content": (
                    f"以下は、現在アクティブなトラック「{track_title}」での作業履歴です。"
                    f"このトラックを再開・継続する際の文脈情報として参照してください。\n\n"
                    f"{track_chronicle_text}"
                ),
                "metadata": {
                    MEMORY_WEAVE_CONTEXT_MARKER: True,
                    "__memory_weave_type__": "track_chronicle",
                },
            })
        if memopedia_text:
            memopedia_intro = (
                "以下は、あなたの長期記憶（Memopedia: 記憶ベース）です。\n"
                "タイトルと概要のみが表示されているページは、ここに載っている以上の詳細な内容を持っています。"
                "特定のページの詳細を確認したい場合は、memopedia_get_page ツールを使って本文を読んでください。"
            )
            messages.append({
                "role": "user",
                "content": f"{memopedia_intro}\n\n{memopedia_text}",
                "metadata": {
                    MEMORY_WEAVE_CONTEXT_MARKER: True,
                    "__memory_weave_type__": "memopedia",
                },
            })

        total_chars = sum(len(m["content"]) for m in messages)
        LOGGER.info(
            "get_memory_weave_context: Generated %d messages (%d chars total, track_chronicle=%s)",
            len(messages), total_chars, "yes" if track_chronicle_text else "no",
        )
        return messages

    except Exception as exc:
        LOGGER.warning("get_memory_weave_context: Failed to build context: %s", exc)
        return []


def _get_chronicle_context(conn: sqlite3.Connection, max_entries: int = 50) -> str:
    """Get General Chronicle (Arasuji) context using hierarchical algorithm.

    v0.32 (2026-05-09): Track Chronicle と排他にするため、内部で
    origin_track_id IS NULL の entry のみが対象になる (get_episode_context が
    origin_track_id=None で呼ばれると General Chronicle のみフィルタする実装)。
    """
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


def _get_track_chronicle_context(
    conn: sqlite3.Connection,
    persona_id: str,
    max_entries: int = 50,
) -> tuple[str, str]:
    """Get Track Chronicle context for the persona's currently active track.

    v0.32 (2026-05-09): Track 内必要情報の維持機構の読み込み側。
    アクティブ Track が無い場合は ("", "") を返す。

    Chronicle 化されていない時間帯の Track 紐付きメッセージ (押し出されたが
    1000 字未満でスキップされた等) は、SAIMemory から直接取得して
    「### Chronicle 化されていないメッセージ」セクションとして添える。
    詳細は docs/intent/persona_cognition/track_chronicle.md §5

    Returns:
        (formatted_text, track_title) — テキストが空なら表示不要
    """
    try:
        from tools.context import get_active_manager
        manager = get_active_manager()
        if not manager:
            return "", ""
        track_manager = getattr(manager, "track_manager", None)
        if not track_manager:
            return "", ""

        track = track_manager.get_running(persona_id)
        if track is None:
            return "", ""

        track_id = getattr(track, "track_id", None)
        if not track_id:
            return "", ""

        # ユーザー会話 Track は親スレッド保持機構で別途扱うため Track Chronicle セクションは出さない
        # (v0.32, 2026-05-09)。詳細: docs/intent/persona_cognition/track_chronicle.md §11
        if getattr(track, "track_type", None) == "user_conversation":
            return "", ""

        from sai_memory.arasuji.context import get_episode_context, format_episode_context
        context = get_episode_context(
            conn, max_entries=max_entries, origin_track_id=track_id
        )

        title = getattr(track, "title", None) or "(無題)"
        parts: List[str] = []
        if context:
            parts.append(format_episode_context(context, include_level_info=True))

        # Chronicle 化されていない Track 紐付きメッセージを取得 (v0.32 §5-3)。
        # 1000 字未満でスキップされた分や、まだ Metabolism が走っていない分が対象。
        raw_text = _get_track_unprocessed_messages_text(conn, track_id)
        if raw_text:
            parts.append("### Chronicle 化されていないメッセージ")
            parts.append(raw_text)

        if not parts:
            return "", ""
        return "\n\n".join(parts), title
    except ImportError:
        LOGGER.debug("Track Chronicle module not available")
        return "", ""
    except Exception:
        LOGGER.warning("Failed to get Track Chronicle context", exc_info=True)
        return "", ""


def _get_track_unprocessed_messages_text(
    conn: sqlite3.Connection,
    track_id: str,
    *,
    max_messages: int = 100,
) -> str:
    """Track 紐付きで Chronicle 化されていないメッセージを生で取得して整形する。

    v0.32 (2026-05-09): Chronicle 化されていない時間帯の補完。
    Chronicle entry の source_ids 集合に含まれないメッセージが対象。
    """
    import json as _json
    from datetime import datetime as _dt

    # 当該 Track の正規 Lv1 entry の source_ids を「処理済み」として収集
    cur = conn.execute(
        "SELECT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1 AND origin_track_id = ? AND is_incomplete = 0",
        (track_id,),
    )
    processed_ids = {row[0] for row in cur.fetchall()}

    # Track 紐付きメッセージから処理済みを除外して取得
    cur = conn.execute(
        "SELECT id, role, content, created_at FROM messages "
        "WHERE origin_track_id = ? "
        "AND line_role = 'main_line' AND scope = 'committed' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM json_each(metadata, '$.tags') WHERE json_each.value IN ('handy_tool', 'spell', 'event_message')"
        ") "
        "ORDER BY created_at ASC "
        f"LIMIT {int(max_messages)}",
        (track_id,),
    )
    unprocessed = [
        row for row in cur.fetchall() if row[0] not in processed_ids
    ]
    if not unprocessed:
        return ""

    lines: List[str] = ["```"]
    for msg_id, role, content, created_at in unprocessed:
        try:
            ts = _dt.fromtimestamp(int(created_at)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            ts = "?"
        # role 表記の整形
        role_label = {"user": "ユーザー", "assistant": "ペルソナ"}.get(role, role or "?")
        # content の前後空白除去 + 過度に長いものは末尾省略
        content_clean = (content or "").strip()
        if len(content_clean) > 500:
            content_clean = content_clean[:500] + "…"
        lines.append(f"- [{ts}] {role_label}: {content_clean}")
    lines.append("```")
    return "\n".join(lines)


def _get_memopedia_context(
    conn: sqlite3.Connection,
    *,
    index_limit: int = 100,
) -> str:
    """Get Memopedia context (page titles, summaries, optionally content for vivid pages).

    Args:
        conn: Database connection to memory.db
        index_limit: Max pages per category to include (sorted by most recently
                     referenced/updated). 0 = unlimited.
    """
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables

        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)

        tree = memopedia.get_tree()
        LOGGER.info("_get_memopedia_context: tree keys=%s", list(tree.keys()))
        lines: List[str] = []

        category_names = {
            "people": "人物",
            "terms": "用語",
            "plans": "予定",
            "events": "出来事",
        }

        def _sort_key(page: Dict) -> int:
            return max(
                page.get("last_referenced_at") or 0,
                page.get("updated_at") or 0,
            )

        def _list_pages(pages: List[Dict], prefix: str = "") -> None:
            for page in pages:
                if not page["id"].startswith("root_"):
                    vividness = page.get("vividness", "rough")
                    sid = page.get("short_id")
                    id_suffix = f" [id: m:{sid}]" if sid else ""

                    if vividness == "buried":
                        continue
                    elif vividness == "faint":
                        lines.append(f"{prefix}- {page['title']}{id_suffix}")
                    elif vividness == "rough":
                        lines.append(f"{prefix}- {page['title']}{id_suffix}: {page['summary']}")
                    elif vividness == "vivid":
                        summary = page['summary']
                        lines.append(f"{prefix}- **{page['title']}**{id_suffix}: {summary}")
                        body = memopedia.render_page_body(page["id"])
                        if body:
                            for line in body.split("\n"):
                                lines.append(f"{prefix}  {line}")

                children = page.get("children", [])
                if children:
                    _list_pages(children, prefix + "  ")

        for category in ["people", "terms", "plans", "events"]:
            pages = tree.get(category, [])
            LOGGER.debug("_get_memopedia_context: category=%s, pages count=%d", category, len(pages))
            if pages:
                # Sort by most recently referenced/updated, apply limit
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
