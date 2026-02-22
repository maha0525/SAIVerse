"""Arasuji generation logic using LLM."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sai_memory.memory.storage import Message
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    add_to_parent_source_ids,
    create_entry,
    find_covering_entry,
    get_entry,
    get_unconsolidated_entries,
    get_leaf_entries_by_level,
    get_max_level,
    mark_consolidated,
)
from sai_memory.arasuji.context import (
    get_episode_context,
    format_episode_context,
)

LOGGER = logging.getLogger(__name__)


def _record_llm_usage(client, persona_id: Optional[str], node_type: str) -> None:
    """Record LLM usage from the client to usage tracker."""
    try:
        usage = client.consume_usage()
        if usage:
            from saiverse.usage_tracker import get_usage_tracker
            get_usage_tracker().record_usage(
                model_id=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_ttl=usage.cache_ttl,
                persona_id=persona_id,
                node_type=node_type,
                category="memory_weave_generate",
            )
    except Exception as e:
        LOGGER.warning(f"Failed to record chronicle usage: {e}")

# Default settings
DEFAULT_BATCH_SIZE = 20  # messages per level-1 arasuji
DEFAULT_CONSOLIDATION_SIZE = 10  # entries per higher-level arasuji


def _format_timestamp(ts: Optional[int]) -> str:
    """Format Unix timestamp to readable string."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _format_messages_for_prompt(messages: List[Message], *, include_timestamp: bool = True) -> str:
    """Format messages for the arasuji prompt.

    Args:
        messages: Messages to format
        include_timestamp: If False, omit timestamps from output
    """
    lines: List[str] = []
    for msg in messages:
        role = msg.role
        if role == "model":
            role = "assistant"
        content = (msg.content or "").strip()
        if not content:
            continue
        if include_timestamp:
            ts_str = _format_timestamp(msg.created_at)
            lines.append(f"[{ts_str}] [{role}]: {content}")
        else:
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


def _format_entries_for_prompt(entries: List[ArasujiEntry], *, include_timestamp: bool = True) -> str:
    """Format arasuji entries for consolidation prompt."""
    lines: List[str] = []
    for i, entry in enumerate(entries, 1):
        if include_timestamp:
            start = _format_timestamp(entry.start_time)
            end = _format_timestamp(entry.end_time)
            lines.append(f"### あらすじ {i} ({start} ~ {end})")
        else:
            lines.append(f"### あらすじ {i}")
        lines.append(entry.content)
        lines.append("")
    return "\n".join(lines)


def _get_context_summaries(conn: sqlite3.Connection, current_level: int, *, include_timestamp: bool = True) -> str:
    """Get context summaries from higher levels for generation context.

    Retrieves unconsolidated entries from levels above the current level
    to provide context about what happened before.
    """
    context_parts: List[str] = []
    max_level = get_max_level(conn)

    # Start from highest level down to current_level + 1
    for level in range(max_level, current_level, -1):
        entries = get_leaf_entries_by_level(conn, level)
        if entries:
            # Calculate messages per entry at this level
            # Level 1 = batch_size, Level 2 = batch_size * consolidation_size, etc.
            context_parts.append(f"## レベル{level}のあらすじ（より大きな流れ）")
            for entry in entries:
                if include_timestamp:
                    start = _format_timestamp(entry.start_time)
                    end = _format_timestamp(entry.end_time)
                    context_parts.append(f"【{start} ~ {end}】")
                context_parts.append(entry.content)
                context_parts.append("")

    return "\n".join(context_parts) if context_parts else ""


def generate_level1_arasuji(
    client,
    conn: sqlite3.Connection,
    messages: List[Message],
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
    memopedia_context: Optional[str] = None,
    debug_log_path: Optional[Path] = None,
    persona_id: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Generate a level-1 arasuji from messages.

    Args:
        client: LLM client with generate() method
        conn: Database connection
        messages: Messages to summarize
        dry_run: If True, don't save to database
        include_timestamp: If False, omit timestamps from prompt (useful when dates are unreliable)
        memopedia_context: Optional semantic memory context (page titles, summaries, keywords)

    Returns:
        Created ArasujiEntry or None on failure
    """
    if not messages:
        return None

    # Extract time range from messages first (needed for temporal isolation)
    start_time = min(msg.created_at for msg in messages) if messages else None
    end_time = max(msg.created_at for msg in messages) if messages else None

    # Get episode context BEFORE this time range (temporal isolation)
    # This ensures we only see past Chronicles, not future ones during regeneration
    if start_time and end_time:
        from sai_memory.arasuji.context import get_episode_context_for_timerange
        context = get_episode_context_for_timerange(
            conn, 
            start_time=start_time, 
            end_time=end_time,
            max_entries=20
        )
    else:
        context = ""

    # Format messages
    conversation = _format_messages_for_prompt(messages, include_timestamp=include_timestamp)
    if not conversation.strip():
        return None

    # Build prompt
    prompt_parts = [
        "あなたは記憶の記録者です。以下の会話から、出来事のあらすじを書いてください。",
        "",
    ]

    if context:
        prompt_parts.extend([
            "## これまでの流れ（参考）",
            context,
            "",
        ])

    if memopedia_context:
        prompt_parts.extend([
            "## 意味記憶（人物・用語の背景情報）",
            memopedia_context,
            "",
        ])

    prompt_parts.extend([
        "## 今回記録する会話",
        conversation,
        "",
        "## 指示",
        "- 3〜5文程度で、何が起きたか、誰と何を話したかを要約",
        "- 時系列の流れがわかるように書く",
        "- 固有名詞や重要な詳細は保持する",
        "- 感情や雰囲気も含める",
        "- 「〜について話した」のような抽象的な記述は避け、具体的に書く",
        "- **日時情報（【2025-01-07 23:56 ~】など）は書かないでください**（自動で付与されます）",
        "- **「あらすじ」などの見出しは書かないでください**（本文のみ出力）",
        "",
        "あらすじを日本語で書いてください。",
    ])

    prompt = "\n".join(prompt_parts)

    # Debug log: write prompt
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"[CHRONICLE Lv1] {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n")
            f.write("--- PROMPT ---\n")
            f.write(prompt)
            f.write("\n")

    # --- LLM call (no retry here; provider handles retry internally) ---
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        _record_llm_usage(client, persona_id, "chronicle_level1")
    except Exception as e:
        LOGGER.error(f"LLM call failed for level-1 arasuji: {e}")
        from llm_clients.exceptions import LLMError
        if isinstance(e, LLMError):
            raise  # Propagate all LLM errors (empty, safety, timeout, etc.)
        return None

    # Debug log: write response
    if debug_log_path:
        with open(debug_log_path, "a", encoding="utf-8") as f:
            f.write("--- RESPONSE ---\n")
            f.write(response or "(empty)")
            f.write("\n")

    if not response or not response.strip():
        LOGGER.warning("Empty response from LLM for level-1 arasuji")
        return None

    content = response.strip()

    # Extract message IDs (time range already calculated at the beginning)
    source_ids = [msg.id for msg in messages]

    if dry_run:
        LOGGER.info(f"[DRY RUN] Would create level-1 arasuji: {content}")
        return ArasujiEntry(
            id="dry-run",
            level=1,
            content=content,
            source_ids=source_ids,
            start_time=start_time,
            end_time=end_time,
            source_count=len(messages),
            message_count=len(messages),
            parent_id=None,
            is_consolidated=False,
            created_at=0,
        )

    # --- DB save with retry (LLM result is already obtained, no re-call) ---
    max_db_retries = 3
    for attempt in range(max_db_retries):
        try:
            entry = create_entry(
                conn,
                level=1,
                content=content,
                source_ids=source_ids,
                start_time=start_time,
                end_time=end_time,
                source_count=len(messages),
                message_count=len(messages),
            )
            LOGGER.info(f"Created level-1 arasuji: {content}")
            return entry
        except Exception as e:
            LOGGER.warning(
                "DB save failed for level-1 arasuji (attempt %d/%d): %s",
                attempt + 1, max_db_retries, e,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < max_db_retries - 1:
                time.sleep(2 ** attempt)

    LOGGER.error("DB save failed after %d attempts for level-1 arasuji", max_db_retries)
    return None


def generate_consolidated_arasuji(
    client,
    conn: sqlite3.Connection,
    entries: List[ArasujiEntry],
    target_level: int,
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
    persona_id: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Generate a consolidated arasuji from lower-level entries.

    Args:
        client: LLM client with generate() method
        conn: Database connection
        entries: Entries to consolidate (should all be at target_level - 1)
        target_level: The level of the new consolidated arasuji
        dry_run: If True, don't save to database

    Returns:
        Created ArasujiEntry or None on failure
    """
    if not entries:
        return None

    # Validate all entries are at the correct level
    expected_level = target_level - 1
    for entry in entries:
        if entry.level != expected_level:
            LOGGER.error(f"Entry {entry.id} is at level {entry.level}, expected {expected_level}")
            return None

    # Calculate time range from entries for temporal isolation
    start_time = min(e.start_time for e in entries if e.start_time) if any(e.start_time for e in entries) else None
    end_time = max(e.end_time for e in entries if e.end_time) if any(e.end_time for e in entries) else None

    # Get context from BEFORE this time range (temporal isolation)
    # Uses same hierarchical algorithm as level-1 generation
    if start_time and end_time:
        from sai_memory.arasuji.context import get_episode_context_for_timerange
        context = get_episode_context_for_timerange(
            conn,
            start_time=start_time,
            end_time=end_time,
            max_entries=10
        )
    else:
        context = ""

    # Format entries
    entries_text = _format_entries_for_prompt(entries, include_timestamp=include_timestamp)

    # Build prompt
    level_desc = "あらすじ" + "のあらすじ" * (target_level - 1)
    prompt_parts = [
        f"以下の{len(entries)}個のあらすじを統合し、「{level_desc}」としてまとめてください。",
        "",
    ]

    if context:
        prompt_parts.extend([
            "## さらに前の出来事（参考）",
            context,
            "",
        ])

    prompt_parts.extend([
        "## 統合対象のあらすじ",
        entries_text,
        "",
        "## 指示",
        "- 5〜8文程度で、全体の流れを俯瞰できるようにまとめる",
        "- 重要な転換点や印象的なエピソードを保持する",
        "- 個々の詳細より「どんな時期だったか」を重視する",
        "- 時系列順に書く",
        "",
        "統合されたあらすじを日本語で書いてください。",
    ])

    prompt = "\n".join(prompt_parts)

    # LLM call (no retry here; provider handles retry internally)
    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )
        _record_llm_usage(client, persona_id, f"chronicle_level{target_level}")
    except Exception as e:
        LOGGER.error(f"LLM call failed for level-{target_level} arasuji: {e}")
        from llm_clients.exceptions import LLMError
        if isinstance(e, LLMError):
            raise  # Propagate all LLM errors (empty, safety, timeout, etc.)
        return None

    if not response or not response.strip():
        LOGGER.warning(f"Empty response from LLM for level-{target_level} arasuji")
        return None

    content = response.strip()

    # Calculate aggregated values
    source_ids = [e.id for e in entries]
    start_time = min(e.start_time for e in entries if e.start_time) if any(e.start_time for e in entries) else None
    end_time = max(e.end_time for e in entries if e.end_time) if any(e.end_time for e in entries) else None
    total_messages = sum(e.message_count for e in entries)

    if dry_run:
        LOGGER.info(f"[DRY RUN] Would create level-{target_level} arasuji: {content}")
        LOGGER.info(f"[DRY RUN] Would mark {len(entries)} entries as consolidated")
        return ArasujiEntry(
            id="dry-run",
            level=target_level,
            content=content,
            source_ids=source_ids,
            start_time=start_time,
            end_time=end_time,
            source_count=len(entries),
            message_count=total_messages,
            parent_id=None,
            is_consolidated=False,
            created_at=0,
        )

    # --- DB save with retry (LLM result is already obtained, no re-call) ---
    max_db_retries = 3
    entry = None
    for attempt in range(max_db_retries):
        try:
            entry = create_entry(
                conn,
                level=target_level,
                content=content,
                source_ids=source_ids,
                start_time=start_time,
                end_time=end_time,
                source_count=len(entries),
                message_count=total_messages,
            )
            break
        except Exception as e:
            LOGGER.warning(
                "DB save failed for level-%d arasuji (attempt %d/%d): %s",
                target_level, attempt + 1, max_db_retries, e,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < max_db_retries - 1:
                time.sleep(2 ** attempt)

    if not entry:
        LOGGER.error(
            "DB save failed after %d attempts for level-%d arasuji",
            max_db_retries, target_level,
        )
        return None

    # Mark source entries as consolidated (retry separately)
    for attempt in range(max_db_retries):
        try:
            mark_consolidated(conn, source_ids, entry.id)
            break
        except Exception as e:
            LOGGER.warning(
                "mark_consolidated failed for level-%d arasuji (attempt %d/%d): %s",
                target_level, attempt + 1, max_db_retries, e,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < max_db_retries - 1:
                time.sleep(2 ** attempt)
    else:
        LOGGER.error(
            "mark_consolidated failed after %d attempts for level-%d arasuji "
            "(entry %s created but children not marked)",
            max_db_retries, target_level, entry.id,
        )

    LOGGER.info(f"Created level-{target_level} arasuji ({total_messages} messages): {content}")
    return entry


def maybe_consolidate(
    client,
    conn: sqlite3.Connection,
    level: int,
    consolidation_size: int = DEFAULT_CONSOLIDATION_SIZE,
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
    persona_id: Optional[str] = None,
) -> List[ArasujiEntry]:
    """Check if consolidation is needed at a level and perform it recursively.

    Args:
        client: LLM client
        conn: Database connection
        level: Level to check for consolidation
        consolidation_size: Number of entries to consolidate
        dry_run: If True, don't save to database

    Returns:
        List of newly created consolidated entries
    """
    created: List[ArasujiEntry] = []

    # Get unconsolidated entries at this level
    pending = get_unconsolidated_entries(conn, level)

    while len(pending) >= consolidation_size:
        # Take the first consolidation_size entries
        batch = pending[:consolidation_size]
        pending = pending[consolidation_size:]

        # Generate consolidated arasuji
        entry = generate_consolidated_arasuji(
            client,
            conn,
            batch,
            target_level=level + 1,
            dry_run=dry_run,
            include_timestamp=include_timestamp,
            persona_id=persona_id,
        )

        if entry:
            created.append(entry)

            # Recursively check the next level
            higher = maybe_consolidate(
                client,
                conn,
                level + 1,
                consolidation_size=consolidation_size,
                dry_run=dry_run,
                include_timestamp=include_timestamp,
                persona_id=persona_id,
            )
            created.extend(higher)
        else:
            LOGGER.warning(
                f"Consolidation at level {level + 1} failed, "
                f"will retry on next generation"
            )

    return created


def regenerate_consolidated_content(
    client,
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    include_timestamp: bool = True,
    persona_id: Optional[str] = None,
) -> Optional[ArasujiEntry]:
    """Re-generate a consolidated entry's content from its current sources.

    Unlike regenerate_entry() which deletes and recreates, this only
    updates the content field in-place, preserving all relationships
    (parent_id, is_consolidated, source_ids).

    Used when gap-fill entries are integrated into existing hierarchy
    and the parent's content needs to reflect the new sources.

    Args:
        client: LLM client with generate() method
        conn: Database connection
        entry_id: ID of the consolidated entry to regenerate
        include_timestamp: If False, omit timestamps from prompt
        persona_id: Optional persona ID for usage tracking

    Returns:
        Updated ArasujiEntry or None on failure
    """
    # 1. Get the entry
    entry = get_entry(conn, entry_id)
    if not entry:
        LOGGER.error(f"Entry {entry_id} not found for content regeneration")
        return None

    if entry.level < 2:
        LOGGER.error(f"Cannot regenerate content for level-{entry.level} entry {entry_id}")
        return None

    # 2. Get all source entries
    source_entries: List[ArasujiEntry] = []
    for sid in entry.source_ids:
        src = get_entry(conn, sid)
        if src:
            source_entries.append(src)
        else:
            LOGGER.warning(f"Source entry {sid} not found for entry {entry_id[:8]}")

    if not source_entries:
        LOGGER.error(f"No source entries found for entry {entry_id[:8]}")
        return None

    # Sort by time
    source_entries.sort(key=lambda e: e.start_time or 0)

    # 3. Recalculate time range and message count
    start_time = (
        min(e.start_time for e in source_entries if e.start_time)
        if any(e.start_time for e in source_entries)
        else None
    )
    end_time = (
        max(e.end_time for e in source_entries if e.end_time)
        if any(e.end_time for e in source_entries)
        else None
    )
    total_messages = sum(e.message_count for e in source_entries)

    # 4. Build prompt (same structure as generate_consolidated_arasuji)
    entries_text = _format_entries_for_prompt(source_entries, include_timestamp=include_timestamp)
    level_desc = "あらすじ" + "のあらすじ" * (entry.level - 1)

    # Get context from before time range (temporal isolation)
    context = ""
    if start_time and end_time:
        from sai_memory.arasuji.context import get_episode_context_for_timerange
        context = get_episode_context_for_timerange(
            conn, start_time=start_time, end_time=end_time, max_entries=10
        )

    prompt_parts = [
        f"以下の{len(source_entries)}個のあらすじを統合し、「{level_desc}」としてまとめてください。",
        "",
    ]
    if context:
        prompt_parts.extend([
            "## さらに前の出来事（参考）",
            context,
            "",
        ])
    prompt_parts.extend([
        "## 統合対象のあらすじ",
        entries_text,
        "",
        "## 指示",
        "- 5〜8文程度で、全体の流れを俯瞰できるようにまとめる",
        "- 重要な転換点や印象的なエピソードを保持する",
        "- 個々の詳細より「どんな時期だったか」を重視する",
        "- 時系列順に書く",
        "",
        "統合されたあらすじを日本語で書いてください。",
    ])
    prompt = "\n".join(prompt_parts)

    # 5. LLM call with retry
    max_retries = 3
    content = None
    for attempt in range(max_retries):
        try:
            response = client.generate(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
            )
            _record_llm_usage(client, persona_id, f"chronicle_level{entry.level}_regen")
            if response and response.strip():
                content = response.strip()
                break
            LOGGER.warning(
                f"Empty response for entry {entry_id[:8]} regeneration "
                f"(attempt {attempt + 1}/{max_retries})"
            )
        except Exception as e:
            LOGGER.warning(
                f"LLM error for entry {entry_id[:8]} regeneration "
                f"(attempt {attempt + 1}/{max_retries}): {e}"
            )
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    if not content:
        LOGGER.error(
            f"Failed to regenerate content for entry {entry_id[:8]} "
            f"after {max_retries} attempts"
        )
        return None

    # 6. UPDATE in-place (preserve parent_id, is_consolidated, etc.)
    conn.execute(
        """
        UPDATE arasuji_entries
        SET content = ?, start_time = ?, end_time = ?,
            message_count = ?, source_count = ?
        WHERE id = ?
        """,
        (content, start_time, end_time, total_messages, len(source_entries), entry_id),
    )
    conn.commit()

    LOGGER.info(
        f"Regenerated content for level-{entry.level} entry {entry_id[:8]} "
        f"({total_messages} messages)"
    )

    # Return updated entry
    return get_entry(conn, entry_id)


def integrate_gap_fill(
    client,
    conn: sqlite3.Connection,
    new_entry: ArasujiEntry,
    *,
    include_timestamp: bool = True,
    persona_id: Optional[str] = None,
) -> List[ArasujiEntry]:
    """Integrate a gap-fill level-1 entry into the existing hierarchy.

    When a gap-fill generates a new level-1 entry that falls within an
    existing level-2's time range, this function:
    1. Adds the new entry to the level-2's source_ids
    2. Re-generates the level-2 content with the new source
    3. Cascades re-generation up through all parent levels

    Args:
        client: LLM client with generate() method
        conn: Database connection
        new_entry: The newly created gap-fill level-1 entry
        include_timestamp: If False, omit timestamps from prompt
        persona_id: Optional persona ID for usage tracking

    Returns:
        List of re-generated entries (level-2 and above)
    """
    regenerated: List[ArasujiEntry] = []

    # 1. Find covering level-2 entry
    if new_entry.start_time is None or new_entry.end_time is None:
        LOGGER.warning(
            f"Gap-fill entry {new_entry.id[:8]} has no time range, "
            f"cannot integrate into hierarchy"
        )
        return []

    covering_l2 = find_covering_entry(
        conn, new_entry.start_time, new_entry.end_time, level=2
    )

    if not covering_l2:
        # No covering level-2 found — let normal consolidation handle it
        LOGGER.info(
            f"No covering level-2 found for gap-fill entry {new_entry.id[:8]}, "
            f"falling back to normal consolidation"
        )
        return []

    # 2. Add new entry to level-2's source_ids and mark as consolidated
    success = add_to_parent_source_ids(conn, new_entry.id, covering_l2.id)
    if not success:
        LOGGER.error(
            f"Failed to add gap-fill entry {new_entry.id[:8]} "
            f"to level-2 {covering_l2.id[:8]}"
        )
        return []

    # Re-read covering entry to get updated source_ids count
    updated_covering = get_entry(conn, covering_l2.id)
    source_count = len(updated_covering.source_ids) if updated_covering else "?"
    LOGGER.info(
        f"Added gap-fill entry {new_entry.id[:8]} to level-2 {covering_l2.id[:8]} "
        f"(sources: {covering_l2.source_count} -> {source_count})"
    )

    # 3. Re-generate covering level-2 content
    regen_l2 = regenerate_consolidated_content(
        client, conn, covering_l2.id,
        include_timestamp=include_timestamp,
        persona_id=persona_id,
    )
    if regen_l2:
        regenerated.append(regen_l2)

        # 4. Cascade: re-generate all parent levels
        current = regen_l2
        while current.parent_id:
            LOGGER.info(
                f"Cascade: propagating to level-{current.level + 1} "
                f"parent {current.parent_id[:8]}..."
            )
            parent_regen = regenerate_consolidated_content(
                client, conn, current.parent_id,
                include_timestamp=include_timestamp,
                persona_id=persona_id,
            )
            if parent_regen:
                regenerated.append(parent_regen)
                current = parent_regen
            else:
                LOGGER.warning(
                    f"Failed to re-generate parent {current.parent_id[:8]} "
                    f"during cascade, stopping propagation"
                )
                break
    else:
        LOGGER.warning(
            f"Failed to re-generate level-2 {covering_l2.id[:8]} "
            f"after gap-fill integration"
        )

    if regenerated:
        LOGGER.info(
            f"Gap-fill integration complete: re-generated {len(regenerated)} entries "
            f"(levels: {', '.join(str(e.level) for e in regenerated)})"
        )

    return regenerated


class ArasujiGenerator:
    """High-level interface for arasuji generation."""

    def __init__(
        self,
        client,
        conn: sqlite3.Connection,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        consolidation_size: int = DEFAULT_CONSOLIDATION_SIZE,
        include_timestamp: bool = True,
        memopedia_context: Optional[str] = None,
        persona_id: Optional[str] = None,
    ):
        """Initialize the generator.

        Args:
            client: LLM client with generate() method
            conn: Database connection
            batch_size: Number of messages per level-1 arasuji
            consolidation_size: Number of entries per higher-level arasuji
            include_timestamp: If False, omit timestamps from prompts (useful when dates are unreliable)
            memopedia_context: Optional semantic memory context (page titles, summaries, keywords)
            persona_id: Optional persona ID for usage tracking
        """
        self.client = client
        self.conn = conn
        self.batch_size = batch_size
        self.consolidation_size = consolidation_size
        self.include_timestamp = include_timestamp
        self.memopedia_context = memopedia_context
        self.persona_id = persona_id
        self.debug_log_path = None  # Can be set externally

    def generate_from_messages(
        self,
        messages: List[Message],
        *,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        batch_callback: Optional[Callable[[List[Message]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[ArasujiEntry], List[ArasujiEntry]]:
        """Generate arasuji entries from messages.

        Args:
            messages: Messages to process
            dry_run: If True, don't save to database
            progress_callback: Optional callback(processed, total) for progress updates
            batch_callback: Optional callback(batch_messages) called after each batch's
                            Chronicle generation and consolidation. Use this to run
                            Memopedia extraction per-batch for interleaved Memory Weave.
            cancel_check: Optional callback that returns True if generation should stop.
                          Checked before each batch. On cancel, returns partial results.

        Returns:
            Tuple of (level1_entries, consolidated_entries)
        """
        from llm_clients.exceptions import LLMError

        level1_entries: List[ArasujiEntry] = []
        consolidated_entries: List[ArasujiEntry] = []
        # Track Level-2 entries created during THIS run to exclude from
        # gap-fill detection.  Gap-fill is meant for Level-2 entries from
        # PREVIOUS runs — entries created in the current run are sequential
        # consolidation results, not gap-fill targets.
        created_l2_ids: set = set()

        total = len(messages)

        # Process messages in batches
        for i in range(0, total, self.batch_size):
            # Check for cancellation before processing each batch
            if cancel_check and cancel_check():
                LOGGER.info("Chronicle generation cancelled by user")
                break

            batch = messages[i:i + self.batch_size]

            # Skip incomplete batches (less than batch_size messages)
            if len(batch) < self.batch_size:
                LOGGER.info(f"Skipping incomplete batch: {len(batch)} < {self.batch_size}")
                continue

            if progress_callback:
                progress_callback(i, total)

            LOGGER.info(f"Processing messages {i+1}-{i+len(batch)} of {total}")

            # Generate level-1 arasuji (retries are handled inside each LLM client)
            try:
                entry = generate_level1_arasuji(
                    self.client,
                    self.conn,
                    batch,
                    dry_run=dry_run,
                    include_timestamp=self.include_timestamp,
                    memopedia_context=self.memopedia_context,
                    debug_log_path=self.debug_log_path,
                    persona_id=self.persona_id,
                )
            except LLMError as e:
                # Add batch context to user_message and re-raise
                e.user_message = (
                    f"メッセージ {i+1}〜{i+len(batch)} の処理中: {e.user_message}"
                )
                # Attach batch metadata for frontend navigation
                e.batch_meta = {
                    "message_ids": [m.id for m in batch],
                    "start_time": min(m.created_at for m in batch),
                    "end_time": max(m.created_at for m in batch),
                }
                raise
            if not entry:
                raise RuntimeError(
                    f"Level-1 generation failed for messages {i+1}-{i+len(batch)}"
                )

            level1_entries.append(entry)

            # Check if this is a gap-fill (covered by existing level-2+)
            if not dry_run and entry.start_time and entry.end_time:
                covering = find_covering_entry(
                    self.conn, entry.start_time, entry.end_time, level=2
                )
            else:
                covering = None

            # Exclude Level-2 entries created during this run — they are
            # sequential consolidation results, not gap-fill targets.
            if covering and covering.id in created_l2_ids:
                LOGGER.info(
                    "Skipping gap-fill for entry %s: covering level-2 %s "
                    "was created in the current run",
                    entry.id[:8], covering.id[:8],
                )
                covering = None

            if covering:
                # Gap-fill: integrate into existing hierarchy
                LOGGER.info(
                    "Gap-fill detected: integrating entry %s "
                    "(time %s-%s) into level-2 %s (time %s-%s)",
                    entry.id[:8], entry.start_time, entry.end_time,
                    covering.id[:8], covering.start_time, covering.end_time,
                )
                regenerated = integrate_gap_fill(
                    self.client,
                    self.conn,
                    entry,
                    include_timestamp=self.include_timestamp,
                    persona_id=self.persona_id,
                )
                consolidated_entries.extend(regenerated)
            else:
                # Normal: try regular consolidation
                consolidated = maybe_consolidate(
                    self.client,
                    self.conn,
                    level=1,
                    consolidation_size=self.consolidation_size,
                    dry_run=dry_run,
                    include_timestamp=self.include_timestamp,
                    persona_id=self.persona_id,
                )
                # Track Level-2 entries created in this run
                for c in consolidated:
                    if c.level == 2:
                        created_l2_ids.add(c.id)
                consolidated_entries.extend(consolidated)

            # Call batch callback for Memopedia extraction (Memory Weave interleaved mode)
            if batch_callback:
                batch_callback(batch)

        if progress_callback:
            progress_callback(total, total)

        return level1_entries, consolidated_entries

    def generate_unprocessed(
        self,
        messages: List[Message],
        *,
        max_messages: Optional[int] = None,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        batch_callback: Optional[Callable[[List[Message]], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[ArasujiEntry], List[ArasujiEntry]]:
        """Filter out already-processed messages, group into contiguous runs, and generate.

        This is the main entry point for Chronicle generation. It handles:
        1. Querying existing level-1 source_ids to find already-processed message IDs
        2. Grouping unprocessed messages into contiguous runs (separated by processed messages)
        3. Filtering out runs smaller than batch_size
        4. Applying max_messages limit across all runs
        5. Calling generate_from_messages() for each qualifying run

        Args:
            messages: All messages (chronologically ordered). Already-processed ones
                      will be filtered out automatically.
            max_messages: Maximum number of unprocessed messages to process.
                          None or 0 means no limit.
            dry_run: If True, don't save to database
            progress_callback: Optional callback(processed, total) for progress updates.
                               Reports global progress across all runs.
            batch_callback: Optional callback(batch_messages) called after each batch
            cancel_check: Optional callback that returns True if generation should stop

        Returns:
            Tuple of (level1_entries, consolidated_entries)
        """
        # 1. Determine already-processed message IDs from level-1 source_ids
        cur = self.conn.execute(
            "SELECT DISTINCT json_each.value "
            "FROM arasuji_entries, json_each(source_ids_json) "
            "WHERE level = 1"
        )
        processed_ids = {row[0] for row in cur.fetchall()}

        # 2. Group unprocessed messages into contiguous runs
        runs: List[List[Message]] = []
        current_run: List[Message] = []
        for msg in messages:
            if msg.id in processed_ids:
                if current_run:
                    runs.append(current_run)
                    current_run = []
                continue
            current_run.append(msg)
        if current_run:
            runs.append(current_run)

        # 3. Filter qualifying runs (>= batch_size)
        qualifying_runs = [r for r in runs if len(r) >= self.batch_size]
        total_unprocessed = sum(len(r) for r in runs)
        total_qualifying = sum(len(r) for r in qualifying_runs)
        isolated_count = total_unprocessed - total_qualifying

        LOGGER.info(
            "Chronicle: %d processed, %d unprocessed in %d runs "
            "(%d qualifying with %d msgs, %d isolated skipped)",
            len(processed_ids), total_unprocessed, len(runs),
            len(qualifying_runs), total_qualifying, isolated_count,
        )

        # 4. Apply max_messages limit
        if max_messages and max_messages > 0:
            limited_runs: List[List[Message]] = []
            remaining = max_messages
            for run in qualifying_runs:
                if remaining <= 0:
                    break
                if len(run) <= remaining:
                    limited_runs.append(run)
                    remaining -= len(run)
                else:
                    limited_runs.append(run[:remaining])
                    remaining = 0
            qualifying_runs = limited_runs
            total_qualifying = sum(len(r) for r in qualifying_runs)
            LOGGER.info(
                "Chronicle: max_messages=%d applied, %d msgs in %d runs after limit",
                max_messages, total_qualifying, len(qualifying_runs),
            )

        if not qualifying_runs:
            return [], []

        # 5. Generate for each qualifying run with global progress tracking
        level1_total: List[ArasujiEntry] = []
        consolidated_total: List[ArasujiEntry] = []
        global_offset = 0
        for run_idx, run in enumerate(qualifying_runs):
            if cancel_check and cancel_check():
                LOGGER.info("Chronicle generation cancelled after %d runs", run_idx)
                break
            LOGGER.info(
                "Processing run %d/%d (%d messages)",
                run_idx + 1, len(qualifying_runs), len(run),
            )

            # Wrap progress_callback to report global progress across all runs
            run_progress: Optional[Callable[[int, int], None]] = None
            if progress_callback:
                _offset = global_offset
                _global_total = total_qualifying

                def _make_progress(offset: int, global_total: int):
                    def _progress(processed: int, _local_total: int):
                        progress_callback(offset + processed, global_total)
                    return _progress

                run_progress = _make_progress(_offset, _global_total)

            level1, consolidated = self.generate_from_messages(
                run,
                dry_run=dry_run,
                progress_callback=run_progress,
                batch_callback=batch_callback,
                cancel_check=cancel_check,
            )
            level1_total.extend(level1)
            consolidated_total.extend(consolidated)
            global_offset += len(run)

        return level1_total, consolidated_total
