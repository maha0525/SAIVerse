"""Episode context retrieval with reverse level promotion algorithm."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Set, Tuple

from sai_memory.memory.storage import Message, get_messages_paginated
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    get_entries_by_level,
    get_max_level,
)


@dataclass
class ContextEntry:
    """A single entry in the episode context."""

    level: int  # 0 = raw message, 1+ = arasuji level
    content: str
    start_time: Optional[int]
    end_time: Optional[int]
    message_count: int  # 1 for raw message, N for arasuji
    source_id: str  # message ID or arasuji ID


def _format_timestamp(ts: Optional[int]) -> str:
    """Format Unix timestamp to readable string."""
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _get_all_arasuji_sorted(conn: sqlite3.Connection) -> List[ArasujiEntry]:
    """Get all arasuji entries sorted by end_time descending (newest first)."""
    max_level = get_max_level(conn)
    all_entries: List[ArasujiEntry] = []

    for level in range(1, max_level + 1):
        entries = get_entries_by_level(conn, level, order_by_time=True)
        all_entries.extend(entries)

    # Sort by end_time descending (newest first)
    all_entries.sort(key=lambda e: e.end_time or 0, reverse=True)
    return all_entries


def _find_arasuji_at_position(
    entries: List[ArasujiEntry],
    position_time: int,
    target_level: int,
) -> Optional[ArasujiEntry]:
    """Find an arasuji at a specific level that ends at or before the position time.

    Args:
        entries: List of arasuji entries sorted by end_time descending
        position_time: The time position to search from
        target_level: The level to search for
    """
    for entry in entries:
        if entry.level != target_level:
            continue
        if entry.end_time is not None and entry.end_time <= position_time:
            return entry
    return None


def _check_overlap(
    entry: ArasujiEntry,
    read_ranges: List[Tuple[int, int]],
) -> bool:
    """Check if an arasuji overlaps with already-read time ranges."""
    if entry.start_time is None or entry.end_time is None:
        return False  # Can't determine overlap, assume no overlap

    entry_start = entry.start_time
    entry_end = entry.end_time

    for range_start, range_end in read_ranges:
        # Check for any overlap
        if not (entry_end < range_start or entry_start > range_end):
            return True  # Overlap detected

    return False


def get_episode_context(
    conn: sqlite3.Connection,
    *,
    max_entries: int = 50,
    include_raw_messages: bool = True,
) -> List[ContextEntry]:
    """Get episode context using the reverse level promotion algorithm.

    Algorithm:
    1. Start from the most recent position and go backwards in time
    2. Current level starts at 0 (raw messages)
    3. Level can only increase by +1 at a time
    4. No overlap with already-read content is allowed (tracked by ID, not time range)
    5. Prefer higher levels (compression) when available within allowed range

    This ensures:
    - Recent events are remembered in detail (low level)
    - Distant past is compressed (high level)
    - No information gaps or duplicates

    Args:
        conn: Database connection
        max_entries: Maximum number of context entries to return
        include_raw_messages: Whether to include raw messages for unprocessed content

    Returns:
        List of ContextEntry objects, ordered from oldest to newest
    """
    result: List[ContextEntry] = []
    read_ids: Set[str] = set()  # IDs of entries that have been read or covered
    current_level = 0  # Start at level 0 (raw messages)

    # Get all arasuji sorted by end_time descending
    all_arasuji = _get_all_arasuji_sorted(conn)

    if not all_arasuji:
        # No arasuji yet, return empty
        return result

    # Find the latest end_time across all arasuji
    latest_arasuji = all_arasuji[0] if all_arasuji else None
    if latest_arasuji is None or latest_arasuji.end_time is None:
        return result

    # Start position: just after the latest arasuji
    # (raw messages after this are "unprocessed")
    position_time = latest_arasuji.end_time

    # Main loop: traverse backwards in time
    while len(result) < max_entries:
        # Try to find an arasuji at the allowed level
        # We can go up to current_level + 1
        found_entry: Optional[ArasujiEntry] = None
        found_level = 0

        # Try levels from current_level + 1 down to 1 (prefer higher levels for compression)
        # Level can only increase by +1 at a time from current_level
        max_allowed_level = current_level + 1
        for try_level in range(max_allowed_level, 0, -1):
            candidate = _find_arasuji_at_position(all_arasuji, position_time, try_level)
            # Check if this entry or its sources have already been read
            if candidate and candidate.id not in read_ids:
                found_entry = candidate
                found_level = try_level
                break  # Use the highest level that works

        if found_entry is None:
            # No suitable arasuji found, we've reached the beginning
            break

        # Add to result
        result.append(ContextEntry(
            level=found_level,
            content=found_entry.content,
            start_time=found_entry.start_time,
            end_time=found_entry.end_time,
            message_count=found_entry.message_count,
            source_id=found_entry.id,
        ))

        # Mark this entry as read
        read_ids.add(found_entry.id)
        # Also mark all source entries as read (prevents reading Level 1 after Level 2 covers it)
        for source_id in found_entry.source_ids:
            read_ids.add(source_id)

        # Update current level and position
        current_level = found_level
        position_time = (found_entry.start_time or 0) - 1

        if position_time <= 0:
            break

    # Reverse to get oldest-to-newest order
    result.reverse()

    return result


def format_episode_context(
    context: List[ContextEntry],
    *,
    include_level_info: bool = True,
) -> str:
    """Format episode context as a string for system prompt injection.

    Args:
        context: List of ContextEntry objects
        include_level_info: Whether to include level information in headers

    Returns:
        Formatted string
    """
    if not context:
        return ""

    parts: List[str] = []
    prev_level = -1

    for entry in context:
        # Add level header if level changed
        if include_level_info and entry.level != prev_level:
            if entry.level == 0:
                parts.append("\n### 最近の出来事")
            elif entry.level == 1:
                parts.append("\n### あらすじ")
            else:
                level_name = "あらすじ" + "のあらすじ" * (entry.level - 1)
                parts.append(f"\n### {level_name}")
            prev_level = entry.level

        # Format time range
        start = _format_timestamp(entry.start_time)
        end = _format_timestamp(entry.end_time)

        if entry.level == 0:
            # Raw message
            parts.append(f"- {entry.content}")
        else:
            # Arasuji
            parts.append(f"【{start} ~ {end}】")
            parts.append(entry.content)
            parts.append("")

    return "\n".join(parts)


def get_episode_context_for_timerange(
    conn: sqlite3.Connection,
    start_time: int,
    end_time: int,
) -> str:
    """Get episode context relevant to a specific time range.

    Useful for providing context when generating Memopedia entries.

    Args:
        conn: Database connection
        start_time: Start of the time range
        end_time: End of the time range

    Returns:
        Formatted context string
    """
    # Get arasuji that overlap with or precede this time range
    all_arasuji = _get_all_arasuji_sorted(conn)

    relevant: List[ArasujiEntry] = []
    for entry in all_arasuji:
        if entry.end_time is None:
            continue

        # Include if:
        # 1. Ends before or at the start of our range (provides context)
        # 2. Overlaps with our range
        if entry.end_time <= end_time:
            relevant.append(entry)

        # Limit to reasonable number
        if len(relevant) >= 10:
            break

    if not relevant:
        return ""

    # Sort by end_time ascending (oldest first)
    relevant.sort(key=lambda e: e.end_time or 0)

    parts: List[str] = []
    for entry in relevant:
        start = _format_timestamp(entry.start_time)
        end = _format_timestamp(entry.end_time)
        level_name = "あらすじ" if entry.level == 1 else "あらすじ" + "のあらすじ" * (entry.level - 1)
        parts.append(f"【{level_name}: {start} ~ {end}】")
        parts.append(entry.content)
        parts.append("")

    return "\n".join(parts)


def get_episode_summary_stats(conn: sqlite3.Connection) -> dict:
    """Get statistics about the episode memory.

    Returns:
        Dictionary with stats like total_messages, levels, entries_per_level
    """
    from sai_memory.arasuji.storage import (
        count_entries_by_level,
        count_unconsolidated_by_level,
        get_total_message_count,
    )

    total_by_level = count_entries_by_level(conn)
    unconsolidated_by_level = count_unconsolidated_by_level(conn)
    total_messages = get_total_message_count(conn)
    max_level = get_max_level(conn)

    return {
        "total_messages_covered": total_messages,
        "max_level": max_level,
        "entries_by_level": total_by_level,
        "unconsolidated_by_level": unconsolidated_by_level,
    }
