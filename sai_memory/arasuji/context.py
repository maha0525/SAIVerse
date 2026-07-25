"""Episode context retrieval with reverse level promotion algorithm."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from sai_memory.memory.storage import Message, get_messages_paginated
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    get_entries_by_level,
    get_max_level,
)

LOGGER = logging.getLogger(__name__)

# Minimum number of entries to read at a given level before allowing
# promotion to the next level.  This keeps more Lv1 ("あらすじ") entries
# in the context instead of compressing everything to Lv2+ immediately.
MIN_ENTRIES_PER_LEVEL: int = 10

# Chronicle 読み込みの文字数予算 (記憶アーキv2 §6.2, Phase 3, 不変条件 §10-4)。
# 件数上限 (旧 max_entries=50) では超過時に最古が黙って落ちるため、文字数予算で
# 制御し、予算内で歴史の先頭まで到達できない場合は昇格閾値 (MIN_ENTRIES_PER_LEVEL)
# を動的に縮めて「より早く粗いレベルへ昇格」させ、全期間カバーを優先する。
#
# 既定 20,000 字: 現行 max_entries=50 は実質 13〜20k 字相当なので、既定で既存
# ユーザーの weave が急に痩せないようにする (エアの実 DB で 32 entries ≒ 13k 字)。
# env SAIVERSE_CHRONICLE_CHAR_BUDGET で調整可。モデル context_length 連動は v1 では
# しない (§12-2)。
DEFAULT_CHRONICLE_CHAR_BUDGET: int = 20_000

# 予算超過を検知したら一段ずつ粗くして再走行する反復方式の、粗さの段列。
# 各段は (min_entries_per_level, prefer_coarse)。
#   1段目 = (10, False): 現行と完全に同じ挙動 (予算内ならここで確定 → legacy 一致)。
#   2段目以降 = prefer_coarse=True で「近い細粒度より粗いレベルを優先」に切り替え、
#             閾値も 5→3→1 と下げて昇格を早める。これで連続した Lv1 タイムラインでも
#             Lv2/Lv3 へ実際に畳める (prefer_coarse なしだと "closest end_time wins"
#             で細粒度が勝ち続け、閾値だけ下げても文字数が減らない)。
_PROMOTION_LADDER: Tuple[Tuple[int, bool], ...] = (
    (MIN_ENTRIES_PER_LEVEL, False),
    (5, True),
    (3, True),
    (1, True),
)

# ``char_budget`` の sentinel。呼び出し側が「予算制で読みたい (具体値は env/既定に
# 委ねる)」と表明するときに渡す。``char_budget=None`` (未指定) は旧来の件数上限
# ベース (Track Chronicle 等、予算制を持ち込みたくない経路) を意味し、区別する。
USE_DEFAULT_BUDGET: int = -1


def _resolve_char_budget(char_budget: int) -> int:
    """Resolve the effective char budget from a requested value.

    Called only when budget mode is active (``char_budget is not None``).
    ``USE_DEFAULT_BUDGET`` → env ``SAIVERSE_CHRONICLE_CHAR_BUDGET`` or the
    built-in default. Any other value is used verbatim (0 = disable).
    """
    if char_budget != USE_DEFAULT_BUDGET:
        return char_budget
    env_val = os.getenv("SAIVERSE_CHRONICLE_CHAR_BUDGET")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            LOGGER.warning(
                "Invalid SAIVERSE_CHRONICLE_CHAR_BUDGET=%r, using default %d",
                env_val, DEFAULT_CHRONICLE_CHAR_BUDGET,
            )
    return DEFAULT_CHRONICLE_CHAR_BUDGET


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


def _get_all_arasuji_sorted(
    conn: sqlite3.Connection,
    *,
    origin_track_id: Optional[str] = None,
) -> List[ArasujiEntry]:
    """Get all arasuji entries sorted by end_time descending (newest first).

    Args:
        origin_track_id: Track Chronicle 用 (v0.32, 2026-05-09)。set すると
            該当 Track の entry のみ。NULL なら General Chronicle (origin_track_id IS NULL) のみ。
    """
    if origin_track_id is not None:
        from sai_memory.arasuji.storage import get_track_entries
        # Track Chronicle: 当該 Track の全 level、incomplete 含む
        all_entries = get_track_entries(
            conn, origin_track_id, level=None, only_unconsolidated=False, include_incomplete=True
        )
    else:
        # General Chronicle: origin_track_id IS NULL の entry のみ。
        # Track Chronicle が混在しないようにフィルタ (v0.32)
        max_level = get_max_level(conn)
        all_entries: List[ArasujiEntry] = []
        for level in range(1, max_level + 1):
            entries = get_entries_by_level(conn, level, order_by_time=True)
            # Track 紐付き entry を除外
            all_entries.extend([e for e in entries if e.origin_track_id is None])

    # Sort by end_time descending (newest first)
    all_entries.sort(key=lambda e: e.end_time or 0, reverse=True)
    return all_entries


def _find_arasuji_at_position(
    entries: List[ArasujiEntry],
    position_time: int,
    max_allowed_level: int,
    read_ids: Set[str],
    *,
    prefer_coarse: bool = False,
) -> Optional[ArasujiEntry]:
    """Find the best arasuji that ends at or before the position time.

    Default selection priority (legacy):
    1. Closest end_time to position_time (most recent)
    2. If same end_time, prefer higher level (more compression)

    ``prefer_coarse=True`` (budget re-runs only): flips the priority to favor
    compression — highest allowed level first, then closest end_time. This is
    what lets a budget-constrained re-run actually collapse a contiguous fine
    (Lv1) timeline onto coarser Lv2/Lv3 entries. Without it, the "closest
    end_time wins" rule keeps picking the fine entries whenever they exist,
    so lowering the promotion threshold alone would not reduce char volume.

    Args:
        entries: List of arasuji entries sorted by end_time descending
        position_time: The time position to search from
        max_allowed_level: Maximum level allowed (current_level + 1)
        read_ids: Set of entry IDs that have already been read or covered
        prefer_coarse: Prefer higher (coarser) levels over closer end_time.
    """
    best: Optional[ArasujiEntry] = None
    for entry in entries:
        if entry.level > max_allowed_level:
            continue
        if entry.id in read_ids:
            continue
        if entry.end_time is None or entry.end_time > position_time:
            continue
        # Found a candidate
        if best is None:
            best = entry
        elif prefer_coarse:
            # Compression-first: higher level wins, then closer end_time.
            if entry.level > best.level:
                best = entry
            elif entry.level == best.level and entry.end_time > best.end_time:
                best = entry
        else:
            # Legacy: closest end_time wins, then higher level on ties.
            if entry.end_time > best.end_time:
                best = entry
            elif entry.end_time == best.end_time and entry.level > best.level:
                best = entry
    return best


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


def _traverse_episode(
    all_arasuji: List[ArasujiEntry],
    *,
    min_entries_per_level: int,
    max_entries: int,
    start_position_time: int,
    prefer_coarse: bool = False,
) -> List[ContextEntry]:
    """Run one backward traversal of the reverse level promotion algorithm.

    Extracted so the budget-driven wrapper can re-run more aggressively
    (smaller ``min_entries_per_level`` = faster promotion, and
    ``prefer_coarse=True`` = compression-first selection) when the char budget
    would be exceeded → coarser levels → fewer chars.

    The loop always runs to the beginning of history (or ``max_entries``); the
    char budget never terminates it early — that is what guarantees the oldest
    entry is reached (不変条件 §10-4).

    Returns entries in newest-to-oldest order (caller reverses).
    """
    result: List[ContextEntry] = []
    read_ids: Set[str] = set()  # IDs of entries that have been read or covered
    current_level = 0  # Start at level 0 (raw messages)
    level_counts: Dict[int, int] = {}  # Track how many entries read per level

    position_time = start_position_time

    # Main loop: traverse backwards in time
    while len(result) < max_entries:
        # Find the best arasuji. We can go up to current_level + 1.
        max_allowed_level = current_level + 1
        found_entry = _find_arasuji_at_position(
            all_arasuji, position_time, max_allowed_level, read_ids,
            prefer_coarse=prefer_coarse,
        )

        if found_entry is None:
            # No suitable arasuji found, we've reached the beginning
            break

        found_level = found_entry.level

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

        # Update level count and check promotion eligibility
        level_counts[found_level] = level_counts.get(found_level, 0) + 1
        if level_counts.get(found_level, 0) >= min_entries_per_level:
            # Read enough entries at this level, allow promotion
            current_level = found_level
        # else: stay at current_level (don't promote yet)

        # Subtract 1 to move to the position just before this entry's coverage
        position_time = (found_entry.start_time or 0) - 1

        if position_time <= 0:
            break

    return result


def _episode_char_count(result: List[ContextEntry]) -> int:
    """Approximate the rendered char cost of a set of entries.

    Uses raw content length as a stable, formatting-agnostic proxy for the
    budget (the format wrappers add a small fixed per-entry overhead which we
    ignore — the budget only needs to be monotone in content volume).
    """
    return sum(len(e.content) for e in result)


def _reached_oldest(result: List[ContextEntry], all_arasuji: List[ArasujiEntry]) -> bool:
    """Whether the traversal reached the earliest point in history.

    True when the oldest entry in ``result`` starts at or before the earliest
    start_time present in ``all_arasuji`` (i.e. the beginning of the story is
    covered — 不変条件 §10-4).
    """
    if not result:
        return False
    starts = [e.start_time for e in all_arasuji if e.start_time is not None]
    if not starts:
        return True  # can't determine; treat as covered
    earliest = min(starts)
    # result is newest-to-oldest; last element is the oldest covered entry
    oldest = result[-1]
    return oldest.start_time is not None and oldest.start_time <= earliest


def get_episode_context(
    conn: sqlite3.Connection,
    *,
    max_entries: int = 100,
    include_raw_messages: bool = True,
    origin_track_id: Optional[str] = None,
    char_budget: Optional[int] = None,
    exclude_entry_ids: Optional[Set[str]] = None,
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

    Budget control (記憶アーキv2 §6.2, Phase 3, 不変条件 §10-4):
    When ``char_budget`` is given (or the ``SAIVERSE_CHRONICLE_CHAR_BUDGET`` env
    is set — see :func:`_resolve_char_budget`), reading is governed by a char
    budget instead of a pure entry count. If a full traversal would exceed the
    budget before reaching the beginning of history, the promotion threshold
    (``MIN_ENTRIES_PER_LEVEL``) is lowered one step (10→5→3→1) and the traversal
    is re-run, so coarser levels are promoted to sooner and the whole timespan
    is covered more cheaply. The traversal loop never terminates on budget — the
    oldest entry is always reached — so "人生の冒頭が weave から消える" cannot
    happen. If even the coarsest run (threshold 1) exceeds the budget (extremely
    deep history), the overflow is accepted and a WARNING is logged; the oldest
    is still included.

    Backward compatibility: ``char_budget=None`` (the default) keeps the legacy
    pure count-based behavior (used by Track Chronicle, which must not change —
    §6.2 item 5). To opt into budget mode with the env/default value, pass
    ``char_budget=USE_DEFAULT_BUDGET``; to pin an explicit budget, pass an int.

    Args:
        conn: Database connection
        max_entries: Maximum number of context entries to return. Retained for
            backward compatibility. When ``char_budget`` is active, ``max_entries``
            still bounds the traversal (safety valve) but the budget is the
            primary control and takes precedence.
        include_raw_messages: Whether to include raw messages for unprocessed content
        char_budget: ``None`` → legacy count-based behavior (no budget).
            ``USE_DEFAULT_BUDGET`` → resolve from env / default (§6.2).
            Explicit int → use that budget (0 disables → count-based).
        exclude_entry_ids: 掲示から外すエントリ id。**提示提示コンテキストの中で digest に
            置き換えて見せている範囲**を head の Chronicle 枠から外すために使う
            (docs/intent/chronicle_eviction.md §6)。同じあらすじが提示コンテキストの中と head
            の両方に出ると、体験が二重化して時系列の錯覚を招くため。

    Returns:
        List of ContextEntry objects, ordered from oldest to newest
    """
    # Get all arasuji sorted by end_time descending (Track filter applied if set)
    all_arasuji = _get_all_arasuji_sorted(conn, origin_track_id=origin_track_id)
    if exclude_entry_ids:
        all_arasuji = [e for e in all_arasuji if e.id not in exclude_entry_ids]

    if not all_arasuji:
        # No arasuji yet, return empty
        return []

    # Find the latest end_time across all arasuji
    latest_arasuji = all_arasuji[0]
    if latest_arasuji.end_time is None:
        return []

    # Start position: just after the latest arasuji
    # (raw messages after this are "unprocessed")
    start_position_time = latest_arasuji.end_time

    # Budget mode is opt-in: char_budget=None means legacy count-based (Track
    # Chronicle keeps its behavior unchanged, §6.2 item 5).
    effective_budget = _resolve_char_budget(char_budget) if char_budget is not None else 0

    # Budget disabled (None or explicit 0): legacy pure count-based behavior.
    if effective_budget <= 0:
        result = _traverse_episode(
            all_arasuji,
            min_entries_per_level=MIN_ENTRIES_PER_LEVEL,
            max_entries=max_entries,
            start_position_time=start_position_time,
        )
        result.reverse()
        return result

    # Budget-driven: try progressively coarser runs until the rendered volume
    # fits the budget while still reaching the oldest entry.
    # 全 Chronicle エントリはメモリ上の走査なので、再走行コストは無視できる。
    best_result: List[ContextEntry] = []
    final_threshold, final_coarse = _PROMOTION_LADDER[-1]
    for threshold, prefer_coarse in _PROMOTION_LADDER:
        result = _traverse_episode(
            all_arasuji,
            min_entries_per_level=threshold,
            max_entries=max_entries,
            start_position_time=start_position_time,
            prefer_coarse=prefer_coarse,
        )
        best_result = result
        final_threshold, final_coarse = threshold, prefer_coarse
        chars = _episode_char_count(result)
        if chars <= effective_budget:
            # Fits the budget. This is the least-coarse run that fits, so it is
            # the most detailed acceptable context — stop here.
            break
        # Exceeded budget → go one step coarser and re-run. Continue.

    result = best_result
    chars = _episode_char_count(result)
    reached_oldest = _reached_oldest(result, all_arasuji)

    if chars > effective_budget:
        # Even the coarsest run exceeds the budget (extremely deep history).
        # Accept the overflow — the oldest entry is still included — and warn so
        # the threshold can be re-tuned from real measurements (§6.2 note 3).
        LOGGER.warning(
            "Chronicle char budget exceeded even at coarsest run: "
            "%d chars > budget %d (entries=%d, threshold=%d, prefer_coarse=%s, "
            "reached_oldest=%s). Oldest entry retained; consider raising "
            "SAIVERSE_CHRONICLE_CHAR_BUDGET or adding higher-level consolidation (§6.2).",
            chars, effective_budget, len(result), final_threshold, final_coarse,
            reached_oldest,
        )

    _log_episode_run(
        result, effective_budget, final_threshold, final_coarse, reached_oldest
    )

    result.reverse()
    return result


def _log_episode_run(
    result: List[ContextEntry],
    budget: int,
    final_threshold: int,
    final_coarse: bool,
    reached_oldest: bool,
) -> None:
    """DEBUG log of a budget-driven run for threshold tuning (§6.2 note 6).

    Emits the adopted level distribution, char count, final promotion settings
    and whether the oldest entry was reached — the real-measurement material for
    tuning the budget/ladder.
    """
    if not LOGGER.isEnabledFor(logging.DEBUG):
        return
    level_dist: Dict[int, int] = {}
    for e in result:
        level_dist[e.level] = level_dist.get(e.level, 0) + 1
    chars = _episode_char_count(result)
    span = "?"
    if result:
        oldest = result[-1]
        newest = result[0]
        span = f"{_format_timestamp(oldest.start_time)} ~ {_format_timestamp(newest.end_time)}"
    LOGGER.debug(
        "Chronicle budget run: entries=%d chars=%d/%d threshold=%d "
        "prefer_coarse=%s level_dist=%s span=[%s] reached_oldest=%s",
        len(result), chars, budget, final_threshold, final_coarse,
        dict(sorted(level_dist.items())), span, reached_oldest,
    )


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
    *,
    max_entries: int = 10,
) -> str:
    """Get episode context for events BEFORE the specified time range.

    Uses the same hierarchical level promotion algorithm as get_episode_context:
    - Events close to start_time use lower levels (Lv1 = detailed)
    - Events far from start_time use higher levels (Lv2+ = compressed)
    - Level can only increase by +1 at a time as we go further back

    This provides appropriate context density: detailed info for recent
    past, compressed summaries for distant past.

    Args:
        conn: Database connection
        start_time: Start of the time range (context is for events BEFORE this)
        end_time: End of the time range (not used, kept for API compatibility)
        max_entries: Maximum number of context entries to return

    Returns:
        Formatted context string
    """
    all_arasuji = _get_all_arasuji_sorted(conn)

    if not all_arasuji:
        return ""

    result: List[ContextEntry] = []
    read_ids: Set[str] = set()
    current_level = 0  # Start at level 0
    level_counts: Dict[int, int] = {}  # Track how many entries read per level

    # Start just before the batch start time
    position_time = start_time - 1

    # Main loop: traverse backwards in time (same algorithm as get_episode_context)
    while len(result) < max_entries:
        max_allowed_level = current_level + 1
        found_entry = _find_arasuji_at_position(all_arasuji, position_time, max_allowed_level, read_ids)

        if found_entry is None:
            break

        # Add to result
        result.append(ContextEntry(
            level=found_entry.level,
            content=found_entry.content,
            start_time=found_entry.start_time,
            end_time=found_entry.end_time,
            message_count=found_entry.message_count,
            source_id=found_entry.id,
        ))

        # Mark as read
        read_ids.add(found_entry.id)
        for source_id in found_entry.source_ids:
            read_ids.add(source_id)

        # Update level count and check promotion eligibility
        level_counts[found_entry.level] = level_counts.get(found_entry.level, 0) + 1
        if level_counts.get(found_entry.level, 0) >= MIN_ENTRIES_PER_LEVEL:
            current_level = found_entry.level

        position_time = (found_entry.start_time or 0) - 1

        if position_time <= 0:
            break

    if not result:
        return ""

    # Reverse to get oldest-to-newest order
    result.reverse()

    # Format output
    parts: List[str] = []
    for entry in result:
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
