"""Arasuji generation logic using LLM."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from sai_memory.memory.storage import Message
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    create_entry,
    get_unconsolidated_entries,
    get_leaf_entries_by_level,
    get_max_level,
    mark_consolidated,
    has_overlapping_entries,
)
from sai_memory.arasuji.context import (
    get_episode_context,
    format_episode_context,
)

LOGGER = logging.getLogger(__name__)

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

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )

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
            LOGGER.info(f"[DRY RUN] Would create level-1 arasuji: {content[:100]}...")
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
        LOGGER.info(f"Created level-1 arasuji: {content[:80]}...")
        return entry

    except Exception as e:
        LOGGER.error(f"Error generating level-1 arasuji: {e}")
        return None


def generate_consolidated_arasuji(
    client,
    conn: sqlite3.Connection,
    entries: List[ArasujiEntry],
    target_level: int,
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
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

    try:
        response = client.generate(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )

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
            LOGGER.info(f"[DRY RUN] Would create level-{target_level} arasuji: {content[:100]}...")
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

        # Create the new entry
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

        # Mark source entries as consolidated
        mark_consolidated(conn, source_ids, entry.id)

        LOGGER.info(f"Created level-{target_level} arasuji ({total_messages} messages): {content[:80]}...")
        return entry

    except Exception as e:
        LOGGER.error(f"Error generating level-{target_level} arasuji: {e}")
        return None


def maybe_consolidate(
    client,
    conn: sqlite3.Connection,
    level: int,
    consolidation_size: int = DEFAULT_CONSOLIDATION_SIZE,
    *,
    dry_run: bool = False,
    include_timestamp: bool = True,
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
            )
            created.extend(higher)

    return created


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
    ):
        """Initialize the generator.

        Args:
            client: LLM client with generate() method
            conn: Database connection
            batch_size: Number of messages per level-1 arasuji
            consolidation_size: Number of entries per higher-level arasuji
            include_timestamp: If False, omit timestamps from prompts (useful when dates are unreliable)
            memopedia_context: Optional semantic memory context (page titles, summaries, keywords)
        """
        self.client = client
        self.conn = conn
        self.batch_size = batch_size
        self.consolidation_size = consolidation_size
        self.include_timestamp = include_timestamp
        self.memopedia_context = memopedia_context
        self.debug_log_path = None  # Can be set externally

    def generate_from_messages(
        self,
        messages: List[Message],
        *,
        dry_run: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        batch_callback: Optional[Callable[[List[Message]], None]] = None,
    ) -> Tuple[List[ArasujiEntry], List[ArasujiEntry]]:
        """Generate arasuji entries from messages.

        Args:
            messages: Messages to process
            dry_run: If True, don't save to database
            progress_callback: Optional callback(processed, total) for progress updates
            batch_callback: Optional callback(batch_messages) called after each batch's
                            Chronicle generation and consolidation. Use this to run
                            Memopedia extraction per-batch for interleaved Memory Weave.

        Returns:
            Tuple of (level1_entries, consolidated_entries)
        """
        level1_entries: List[ArasujiEntry] = []
        consolidated_entries: List[ArasujiEntry] = []

        total = len(messages)

        # Process messages in batches
        for i in range(0, total, self.batch_size):
            batch = messages[i:i + self.batch_size]
            
            # Skip incomplete batches (less than batch_size messages)
            if len(batch) < self.batch_size:
                LOGGER.info(f"Skipping incomplete batch: {len(batch)} < {self.batch_size}")
                continue

            if progress_callback:
                progress_callback(i, total)

            LOGGER.info(f"Processing messages {i+1}-{i+len(batch)} of {total}")

            # Check if Chronicle already exists for this time range
            batch_start = min(msg.created_at for msg in batch) if batch else None
            batch_end = max(msg.created_at for msg in batch) if batch else None
            
            if batch_start and batch_end and has_overlapping_entries(self.conn, batch_start, batch_end, level=1):
                LOGGER.info(f"  Skipping: Chronicle already exists for time range {batch_start}-{batch_end}")
                # Still call batch_callback for Memopedia extraction even if Chronicle exists
                if batch_callback:
                    batch_callback(batch)
                continue

            # Generate level-1 arasuji
            entry = generate_level1_arasuji(
                self.client,
                self.conn,
                batch,
                dry_run=dry_run,
                include_timestamp=self.include_timestamp,
                memopedia_context=self.memopedia_context,
                debug_log_path=self.debug_log_path,
            )

            if entry:
                level1_entries.append(entry)

                consolidated = maybe_consolidate(
                    self.client,
                    self.conn,
                    level=1,
                    consolidation_size=self.consolidation_size,
                    dry_run=dry_run,
                    include_timestamp=self.include_timestamp,
                )
                consolidated_entries.extend(consolidated)

            # Call batch callback for Memopedia extraction (Memory Weave interleaved mode)
            if batch_callback:
                batch_callback(batch)

        if progress_callback:
            progress_callback(total, total)

        return level1_entries, consolidated_entries
