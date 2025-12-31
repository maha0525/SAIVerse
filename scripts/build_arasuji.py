#!/usr/bin/env python3
"""
Build Arasuji (episode memory) from existing SAIMemory conversation logs.

This script reads conversation messages from a persona's memory.db and generates
hierarchical summaries (arasuji) for episode memory.

Usage:
    python scripts/build_arasuji.py <persona_id> [--limit N] [--model MODEL] [--dry-run]

Examples:
    # Build from first 100 messages
    python scripts/build_arasuji.py air_city_a --limit 100

    # Process messages 101-200 (for testing)
    python scripts/build_arasuji.py air_city_a --offset 100 --limit 100

    # Preview what would be generated without writing
    python scripts/build_arasuji.py air_city_a --limit 50 --dry-run

    # Show current arasuji statistics
    python scripts/build_arasuji.py air_city_a --stats

    # Clear all arasuji and start fresh
    python scripts/build_arasuji.py air_city_a --clear
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

load_dotenv()

# Skip tool imports to avoid circular import issue
os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"

from sai_memory.memory.storage import init_db, get_messages_paginated, Message
from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import (
    count_entries_by_level,
    count_unconsolidated_by_level,
    get_total_message_count,
    get_max_level,
    clear_all_entries,
    get_progress,
    update_progress,
)
from sai_memory.arasuji.generator import (
    ArasujiGenerator,
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONSOLIDATION_SIZE,
)
from sai_memory.arasuji.context import (
    get_episode_context,
    format_episode_context,
    get_episode_summary_stats,
)
from model_configs import find_model_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)


def get_persona_db_path(persona_id: str) -> Path:
    """Get the path to a persona's memory.db file."""
    return Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"


def fetch_messages(
    db_path: Path,
    limit: int = 100,
    offset: int = 0,
    thread_id: str | None = None,
) -> List[Message]:
    """Fetch messages from the database.

    Args:
        db_path: Path to the memory.db file
        limit: Maximum number of messages to return
        offset: Number of messages to skip from the beginning
        thread_id: If specified, only fetch from this thread. Otherwise fetch from all threads.
    """
    conn = init_db(str(db_path), check_same_thread=False)

    # Get threads to process
    if thread_id:
        threads = [thread_id]
    else:
        # Order threads by their earliest message timestamp
        cur = conn.execute("""
            SELECT t.id, MIN(m.created_at) as first_msg_ts
            FROM threads t
            LEFT JOIN messages m ON t.id = m.thread_id
            GROUP BY t.id
            ORDER BY first_msg_ts ASC NULLS LAST
        """)
        threads = [row[0] for row in cur.fetchall()]

    all_messages: List[Message] = []
    total_to_fetch = offset + limit  # Need to fetch offset+limit then slice

    for tid in threads:
        page = 0
        while len(all_messages) < total_to_fetch:
            batch = get_messages_paginated(conn, tid, page=page, page_size=100)
            if not batch:
                break
            all_messages.extend(batch)
            page += 1
            if len(all_messages) >= total_to_fetch:
                break
        if len(all_messages) >= total_to_fetch:
            break

    conn.close()
    # Apply offset and limit
    return all_messages[offset:offset + limit]


def print_stats(conn, persona_id: str) -> None:
    """Print arasuji statistics."""
    stats = get_episode_summary_stats(conn)

    print("\n" + "=" * 60)
    print(f"Arasuji Statistics for: {persona_id}")
    print("=" * 60)
    print(f"Total messages covered: {stats['total_messages_covered']}")
    print(f"Maximum level: {stats['max_level']}")

    if stats['entries_by_level']:
        print("\nEntries by level:")
        for level, count in sorted(stats['entries_by_level'].items()):
            unconsolidated = stats['unconsolidated_by_level'].get(level, 0)
            level_name = "あらすじ" if level == 1 else "あらすじ" + "のあらすじ" * (level - 1)
            print(f"  Level {level} ({level_name}): {count} total, {unconsolidated} unconsolidated")
    else:
        print("\nNo arasuji entries yet.")

    print("=" * 60)


def print_context_preview(conn, max_entries: int = 100, debug: bool = False) -> None:
    """Print a preview of the episode context that would be injected."""
    if debug:
        # Debug mode: step through the algorithm manually
        from sai_memory.arasuji.context import _get_all_arasuji_sorted, _find_arasuji_at_position
        from sai_memory.arasuji.storage import get_entries_by_level, get_max_level

        print("\n" + "=" * 60)
        print("DEBUG: Arasuji Algorithm Step-by-Step")
        print("=" * 60)

        # Show all arasuji in DB
        max_level = get_max_level(conn)
        print(f"\n[1] All arasuji in DB (max_level={max_level}):")
        for level in range(1, max_level + 1):
            entries = get_entries_by_level(conn, level, order_by_time=True)
            print(f"  Level {level}: {len(entries)} entries")
            for e in entries[:5]:  # Show first 5
                print(f"    - id={e.id[:8]}... end_time={e.end_time} source_ids={len(e.source_ids)}")
            if len(entries) > 5:
                print(f"    ... and {len(entries) - 5} more")

        # Show sorted list
        all_arasuji = _get_all_arasuji_sorted(conn)
        print(f"\n[2] All arasuji sorted by end_time desc: {len(all_arasuji)} total")
        for i, e in enumerate(all_arasuji[:10]):
            print(f"  {i}: level={e.level} end_time={e.end_time} id={e.id[:8]}...")
        if len(all_arasuji) > 10:
            print(f"  ... and {len(all_arasuji) - 10} more")

        # Step through algorithm
        print(f"\n[3] Algorithm execution:")
        read_ids = set()
        current_level = 0  # Start at level 0
        position_time = all_arasuji[0].end_time if all_arasuji else 0
        print(f"  Initial position_time={position_time}, current_level={current_level}")

        for step in range(min(max_entries, 15)):
            max_allowed_level = current_level + 1
            print(f"\n  Step {step + 1}: position_time={position_time}, max_allowed={max_allowed_level}, read_ids={len(read_ids)}")

            found_entry = _find_arasuji_at_position(all_arasuji, position_time, max_allowed_level, read_ids)

            if found_entry is None:
                print("    -> No entry found, stopping")
                break

            found_level = found_entry.level
            print(f"    -> Selected: level={found_level}, end_time={found_entry.end_time}, id={found_entry.id[:8]}...")
            print(f"    -> source_ids: {found_entry.source_ids[:3]}..." if len(found_entry.source_ids) > 3 else f"    -> source_ids: {found_entry.source_ids}")

            read_ids.add(found_entry.id)
            for source_id in found_entry.source_ids:
                read_ids.add(source_id)

            current_level = found_level
            position_time = found_entry.start_time or 0
            print(f"    -> Updated: current_level={current_level}, position_time={position_time}, read_ids={len(read_ids)}")

        print("\n" + "=" * 60)

    context = get_episode_context(conn, max_entries=max_entries)

    print("\n" + "=" * 60)
    print("Episode Context Preview (what would be injected)")
    print("=" * 60)

    if not context:
        print("(No episode context available)")
    else:
        print(f"Total entries: {len(context)}")
        print("-" * 60)
        formatted = format_episode_context(context)
        print(formatted)

    print("=" * 60)


def list_available_models() -> None:
    """Print available models and exit."""
    from model_configs import MODEL_CONFIGS, get_model_display_name

    print("\n利用可能なモデル一覧:")
    print("-" * 60)
    for model_id, config in sorted(MODEL_CONFIGS.items()):
        provider = config.get("provider", "unknown")
        display_name = get_model_display_name(model_id)
        if display_name != model_id:
            print(f"  {model_id}")
            print(f"    表示名: {display_name}")
            print(f"    Provider: {provider}")
        else:
            print(f"  {model_id} (provider: {provider})")
    print("-" * 60)
    print(f"合計: {len(MODEL_CONFIGS)} モデル\n")


def main():
    parser = argparse.ArgumentParser(
        description="Build Arasuji (episode memory) from SAIMemory conversation logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # デフォルトモデル (gemini-2.0-flash) で100件処理
  python scripts/build_arasuji.py air_city_a --limit 100

  # 101件目から100件処理（テスト用）
  python scripts/build_arasuji.py air_city_a --offset 100 --limit 100

  # ドライラン（保存せずにプレビュー）
  python scripts/build_arasuji.py air_city_a --limit 50 --dry-run

  # 統計情報を表示
  python scripts/build_arasuji.py air_city_a --stats

  # コンテキストプレビューを表示
  python scripts/build_arasuji.py air_city_a --preview-context

  # 全てのあらすじをクリア
  python scripts/build_arasuji.py air_city_a --clear

  # バッチサイズと統合サイズを変更
  python scripts/build_arasuji.py air_city_a --batch-size 30 --consolidation-size 5

  # 日時情報を省略（インポートしたログで日時が不正確な場合）
  python scripts/build_arasuji.py air_city_a --no-timestamp

  # 利用可能なモデル一覧を表示
  python scripts/build_arasuji.py --list-models
""",
    )
    parser.add_argument("persona_id", nargs="?", help="Persona ID to process")
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Maximum number of messages to process (default: 100)"
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="Number of messages to skip (for testing, e.g., --offset 100 to skip first 100)"
    )
    parser.add_argument(
        "--model", default="gemini-2.0-flash",
        help="Model to use for generation (default: gemini-2.0-flash)"
    )
    parser.add_argument(
        "--provider",
        help="Override provider detection (openai, anthropic, gemini, ollama)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview generation without writing to database"
    )
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
        help=f"Number of messages per level-1 arasuji (default: {DEFAULT_BATCH_SIZE})"
    )
    parser.add_argument(
        "--consolidation-size", type=int, default=DEFAULT_CONSOLIDATION_SIZE,
        help=f"Number of entries per higher-level arasuji (default: {DEFAULT_CONSOLIDATION_SIZE})"
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="List available models and exit"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show arasuji statistics and exit"
    )
    parser.add_argument(
        "--preview-context", action="store_true",
        help="Preview the episode context that would be injected"
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Clear all arasuji entries and exit"
    )
    parser.add_argument(
        "--thread", type=str, metavar="THREAD_ID",
        help="Process only messages from this thread ID"
    )
    parser.add_argument(
        "--no-timestamp", action="store_true",
        help="Omit timestamps from prompts (useful when dates are unreliable due to log import)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show detailed debug output for --preview-context"
    )

    args = parser.parse_args()

    # Handle --list-models
    if args.list_models:
        list_available_models()
        sys.exit(0)

    # Require persona_id for most operations
    if not args.persona_id:
        parser.error("persona_id is required (unless using --list-models)")

    # Check if persona exists
    db_path = get_persona_db_path(args.persona_id)
    if not db_path.exists():
        LOGGER.error(f"Persona database not found: {db_path}")
        sys.exit(1)

    # Initialize database connection
    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)

    # Handle --stats
    if args.stats:
        print_stats(conn, args.persona_id)
        conn.close()
        sys.exit(0)

    # Handle --preview-context
    if args.preview_context:
        print_context_preview(conn, debug=args.debug)
        conn.close()
        sys.exit(0)

    # Handle --clear
    if args.clear:
        LOGGER.info("Clearing all arasuji entries...")
        deleted = clear_all_entries(conn)
        LOGGER.info(f"Deleted {deleted} entries")
        conn.close()
        sys.exit(0)

    LOGGER.info(f"Building Arasuji for persona: {args.persona_id}")
    LOGGER.info(f"Database: {db_path}")
    LOGGER.info(f"Message range: offset={args.offset}, limit={args.limit}")
    LOGGER.info(f"Batch size: {args.batch_size}")
    LOGGER.info(f"Consolidation size: {args.consolidation_size}")
    LOGGER.info(f"Dry run: {args.dry_run}")
    if args.no_timestamp:
        LOGGER.info("Timestamps will be omitted from prompts")

    # Initialize LLM client
    resolved_model_id, model_config = find_model_config(args.model)

    if resolved_model_id:
        if resolved_model_id != args.model:
            LOGGER.info(f"Resolved model '{args.model}' -> '{resolved_model_id}'")
        actual_model_id = model_config.get("model", resolved_model_id)
        context_length = model_config.get("context_length", 128000)
        auto_provider = model_config.get("provider", "gemini")
    else:
        LOGGER.warning(f"Model '{args.model}' not found in config, using default provider 'gemini'")
        actual_model_id = args.model
        context_length = 128000
        auto_provider = "gemini"

    provider = args.provider if args.provider else auto_provider

    LOGGER.info(f"Using model: {actual_model_id}")
    LOGGER.info(f"Using provider: {provider}")

    # Import factory directly to avoid circular import
    from llm_clients.factory import get_llm_client

    client = get_llm_client(actual_model_id, provider, context_length, config=model_config)

    # Fetch messages
    LOGGER.info(f"Fetching messages (offset={args.offset}, limit={args.limit}, thread={args.thread or 'all'})...")
    messages = fetch_messages(db_path, limit=args.limit, offset=args.offset, thread_id=args.thread)
    LOGGER.info(f"Fetched {len(messages)} messages")

    if not messages:
        LOGGER.warning("No messages found")
        conn.close()
        sys.exit(0)

    # Generate arasuji
    generator = ArasujiGenerator(
        client,
        conn,
        batch_size=args.batch_size,
        consolidation_size=args.consolidation_size,
        include_timestamp=not args.no_timestamp,
    )

    def progress_callback(processed: int, total: int) -> None:
        if total > 0:
            pct = (processed / total) * 100
            LOGGER.info(f"Progress: {processed}/{total} ({pct:.1f}%)")

    level1_entries, consolidated_entries = generator.generate_from_messages(
        messages,
        dry_run=args.dry_run,
        progress_callback=progress_callback,
    )

    LOGGER.info(f"Generated {len(level1_entries)} level-1 arasuji")
    LOGGER.info(f"Generated {len(consolidated_entries)} consolidated arasuji")

    # Update progress tracking
    if not args.dry_run and messages:
        last_msg = messages[-1]
        update_progress(conn, last_msg.id)

    # Show final state
    if not args.dry_run:
        print_stats(conn, args.persona_id)
        print("\n" + "-" * 60)
        print("Episode Context Preview:")
        print("-" * 60)
        print_context_preview(conn)

    conn.close()
    LOGGER.info("Done!")


if __name__ == "__main__":
    main()
