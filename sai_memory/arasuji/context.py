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
# 制御する。予算に収める手段は :func:`_compress_within_budget` (親 entry への
# 置き換え) が担う。
#
# 既定 20,000 字: 現行 max_entries=50 は実質 13〜20k 字相当なので、既定で既存
# ユーザーの weave が急に痩せないようにする (エアの実 DB で 32 entries ≒ 13k 字)。
# env SAIVERSE_CHRONICLE_CHAR_BUDGET で調整可。モデル context_length 連動は v1 では
# しない (§12-2)。
DEFAULT_CHRONICLE_CHAR_BUDGET: int = 20_000

# 予算圧縮の反復上限 (安全弁)。1 回の置き換えで必ず 1 個以上の entry が親へ
# 畳まれるので、正常なデータでは entry 数を超えて回ることはない。
_MAX_COMPRESSION_ROUNDS: int = 1000

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
) -> Optional[ArasujiEntry]:
    """Find the best arasuji that ends at or before the position time.

    **記憶の穴を作らないことが第一制約** (最前線への固定)。走査は
    ``position_time`` を選んだ entry の start_time の手前へ動かしながら過去へ
    降りていくので、「未読の entry を飛び越えて、より古い entry を選ぶ」と、
    飛び越えた範囲は二度と候補にならない — その体験は誰にも記録されないまま
    提示から落ちる。よって選べるのは常に **読み残しの最前線** (position_time
    以前で end_time が最大の未読 entry) と同じ end_time を持つ entry に限る。

    最前線を確定したうえで、同じ end_time の候補 (= 同じ範囲をどの粒度で見るか
    の選択肢。上位 entry は子と end_time が揃う) の中から粒度を選ぶ:

    1. ``max_allowed_level`` 以下の候補があれば、その中で最も粗い (level 最大)
       ものを採る。これが圧縮 — 同じ範囲をより少ない字数で覆う。
    2. 一つも無ければ、昇格制限より穴回避を優先し、候補中で最も細かい
       (level 最小) ものを採る。粒度の見た目より記憶の連続性が上位。

    予算に収める圧縮はこの走査では**行わない**。読むときに飛ばす形の圧縮は、
    飛ばした範囲が二度と拾われず記憶が黙って落ちるため、読み切ったあとの
    :func:`_compress_within_budget` (親 entry への置き換え = 範囲を保存する
    圧縮) に分離してある。

    Args:
        entries: List of arasuji entries sorted by end_time descending
        position_time: The time position to search from
        max_allowed_level: Maximum level allowed (current_level + 1)
        read_ids: Set of entry IDs that have already been read or covered
    """
    # entries は end_time 降順。最初に見つかる未読・position_time 以前の entry が
    # 読み残しの最前線。
    frontier: Optional[int] = None
    for entry in entries:
        if entry.id in read_ids:
            continue
        if entry.end_time is None or entry.end_time > position_time:
            continue
        frontier = entry.end_time
        break
    if frontier is None:
        return None

    best: Optional[ArasujiEntry] = None
    fallback: Optional[ArasujiEntry] = None
    for entry in entries:
        if entry.id in read_ids:
            continue
        if entry.end_time is None or entry.end_time != frontier:
            continue
        if entry.level > max_allowed_level:
            # 昇格制限の外。穴回避のための最後の受け皿として最も細かいものを控える。
            if fallback is None or entry.level < fallback.level:
                fallback = entry
            continue
        # 昇格制限の内側。同じ範囲を覆う候補なので、粗いほど字数が減る。
        if best is None or entry.level > best.level:
            best = entry
    return best if best is not None else fallback


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
) -> List[ContextEntry]:
    """Run one backward traversal of the reverse level promotion algorithm.

    走査は「読み残しの最前線」を飛び越えないので (:func:`_find_arasuji_at_position`)、
    **提示に穴が空かない**。予算に収める圧縮は :func:`_compress_within_budget` の
    仕事で、この走査は関与しない。

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

        # 次の走査位置は原則「選んだ entry の被覆の直前」。ただし被覆が部分的に
        # 重なっている未読 entry をそれで飛び越すと、その体験は以後どの周回でも
        # 候補にならず黙って落ちる (選んだ entry がそれを覆っていないので、
        #  代弁もされない)。飛び越し範囲にそういう entry が居るなら、その
        # end_time まで戻して次の周回で拾う。戻し先は必ず現在の position_time
        # 以下なので、走査の単調降下と結果の時系列降順は保たれる。
        #
        # 包含関係にある entry には戻さない。選んだ entry の内側なら代弁済み、
        # 外側 (= 選んだ entry の親) なら子を既に読んでいるので、後から親を読むと
        # 同じ体験が二重に出る。親の他の子は走査が更に古い側へ降りる過程で拾う。
        next_position = (found_entry.start_time or 0) - 1
        cover_start = found_entry.start_time
        cover_end = found_entry.end_time
        for other in all_arasuji:
            if other.id in read_ids:
                continue
            if other.end_time is None or other.end_time > position_time:
                continue
            if other.end_time <= next_position:
                continue
            nested = (
                cover_start is not None
                and cover_end is not None
                and other.start_time is not None
                and (
                    (cover_start <= other.start_time and other.end_time <= cover_end)
                    or (other.start_time <= cover_start and cover_end <= other.end_time)
                )
            )
            if not nested:
                next_position = other.end_time
        position_time = next_position

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


def _compress_within_budget(
    result: List[ContextEntry],
    all_arasuji: List[ArasujiEntry],
    budget: int,
) -> List[ContextEntry]:
    """予算に収まるまで、古い側から親 entry への置き換えで粗くする。

    置き換えは**被覆範囲を保存する** (親は自分の子の全範囲を覆う) ので、粒度が
    粗くなるだけで体験は落ちない。これが「読むときに飛ばす」形の圧縮 (旧
    昇格 ladder) との決定的な違い — あちらは飛ばした範囲が二度と候補にならず、
    記憶が黙って消えていた (提示の穴)。

    古い側から潰すので「最近は細かく、過去は粗く」も保たれる。置き換えるのは
    **親の子が全員、結果の中で連続して並んでいるとき**に限る。一部しか居ない
    親で置き換えると、その親は結果に残る他の entry と範囲が重なり、同じ体験が
    二重に出る。

    Args:
        result: 走査結果 (新しい → 古い順)
        all_arasuji: 親を引くための全 entry
        budget: 文字数予算

    Returns:
        置き換え後の結果 (新しい → 古い順)。予算に収めきれない場合
        (これ以上畳める親が無い) は、収まらないまま返す — 記憶の連続性が
        軽量化より上位なので、削って穴を作ることはしない。
    """
    if budget <= 0 or _episode_char_count(result) <= budget:
        return result

    # 親は source_ids の逆引きで持つ。``parent_id`` 列は束ね時にしか入らないが、
    # 被覆の正は source_ids 側 (走査の read_ids も source_ids で潰している)。
    parent_of: Dict[str, ArasujiEntry] = {}
    for candidate in all_arasuji:
        for child_id in candidate.source_ids:
            parent_of[child_id] = candidate

    for _ in range(_MAX_COMPRESSION_ROUNDS):
        if _episode_char_count(result) <= budget:
            break
        replaced = False
        # 末尾 = 最も古い側から、同じ親を持つ連続群を探す。
        i = len(result) - 1
        while i >= 0:
            parent = parent_of.get(result[i].source_id)
            if parent is None:
                i -= 1
                continue
            j = i
            while j >= 0:
                if parent_of.get(result[j].source_id) is not parent:
                    break
                j -= 1
            group_ids = {result[k].source_id for k in range(j + 1, i + 1)}
            if set(parent.source_ids) <= group_ids:
                result = result[:j + 1] + [ContextEntry(
                    level=parent.level,
                    content=parent.content,
                    start_time=parent.start_time,
                    end_time=parent.end_time,
                    message_count=parent.message_count,
                    source_id=parent.id,
                )] + result[i + 1:]
                replaced = True
                break
            i = j
        if not replaced:
            break
    return result


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

    # Budget-driven: 穴を作らずに読み切ってから、予算に収まるまで親へ置き換える。
    # 走査と圧縮を分けているのが要点 — 読む側で飛ばす形の圧縮は、飛ばした範囲が
    # 二度と拾われず記憶が黙って落ちる (提示の穴)。
    result = _traverse_episode(
        all_arasuji,
        min_entries_per_level=MIN_ENTRIES_PER_LEVEL,
        max_entries=max_entries,
        start_position_time=start_position_time,
    )
    result = _compress_within_budget(result, all_arasuji, effective_budget)

    chars = _episode_char_count(result)
    reached_oldest = _reached_oldest(result, all_arasuji)

    if chars > effective_budget:
        # これ以上畳める親が無い (未束ねの一次あらすじが並んでいる等)。超過を
        # 受け入れる — 削れば体験が落ちるので、記憶の連続性を優先する。束ねが
        # 効いていないサインでもあるので WARNING で観測可能にする。
        LOGGER.warning(
            "Chronicle char budget exceeded after parent-collapse compression: "
            "%d chars > budget %d (entries=%d, reached_oldest=%s). Nothing was "
            "dropped; no further parent is available to collapse into. Check "
            "band consolidation (unconsolidated Lv1 backlog) or raise "
            "SAIVERSE_CHRONICLE_CHAR_BUDGET (§6.2).",
            chars, effective_budget, len(result), reached_oldest,
        )

    _log_episode_run(result, effective_budget, reached_oldest)

    result.reverse()
    return result


def _log_episode_run(
    result: List[ContextEntry],
    budget: int,
    reached_oldest: bool,
) -> None:
    """DEBUG log of a budget-driven run for budget tuning (§6.2 note 6).

    Emits the adopted level distribution, char count and whether the oldest
    entry was reached — the real-measurement material for tuning the budget.
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
        "Chronicle budget run: entries=%d chars=%d/%d level_dist=%s "
        "span=[%s] reached_oldest=%s",
        len(result), chars, budget,
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
