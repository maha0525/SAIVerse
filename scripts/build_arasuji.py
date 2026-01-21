#!/usr/bin/env python3
"""
Build Chronicle (episode memory, part of Memory Weave) from existing SAIMemory conversation logs.

This script reads conversation messages from a persona's memory.db and generates
hierarchical summaries (Chronicle) for episode memory.

Usage:
    python scripts/build_arasuji.py <persona_id> [--limit N] [--model MODEL] [--dry-run]

Examples:
    # Build Chronicle from first 100 messages
    python scripts/build_arasuji.py air_city_a --limit 100

    # Process messages 101-200
    python scripts/build_arasuji.py air_city_a --offset 100 --limit 100

    # Preview what would be generated without writing
    python scripts/build_arasuji.py air_city_a --limit 50 --dry-run

    # Show current Chronicle statistics
    python scripts/build_arasuji.py air_city_a --stats

    # Clear all Chronicle entries and start fresh
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
    ArasujiEntry,
    count_entries_by_level,
    count_unconsolidated_by_level,
    create_entry,
    get_total_message_count,
    get_max_level,
    get_all_entries_ordered,
    clear_all_entries,
    get_progress,
    update_progress,
    mark_consolidated,
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
    get_episode_context_for_timerange,
)
from model_configs import find_model_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Environment variable configuration for Memory Weave
ENV_MODEL = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.0-flash")
ENV_BATCH_SIZE = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
ENV_CONSOLIDATION_SIZE = int(os.getenv("MEMORY_WEAVE_CONSOLIDATION_SIZE", str(DEFAULT_CONSOLIDATION_SIZE)))
ENV_MAINTAIN_INTERVAL = int(os.getenv("MEMORY_WEAVE_MAINTAIN_INTERVAL", "0"))


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

    Note:
        Messages are ordered by created_at ASC across all threads to ensure
        consistent chronological ordering (message #1 is always the oldest).
    """
    conn = init_db(str(db_path), check_same_thread=False)

    if thread_id:
        # Single thread: use existing paginated fetch
        all_messages: List[Message] = []
        total_to_fetch = offset + limit
        page = 0
        while len(all_messages) < total_to_fetch:
            batch = get_messages_paginated(conn, thread_id, page=page, page_size=100)
            if not batch:
                break
            all_messages.extend(batch)
            page += 1
        conn.close()
        return all_messages[offset:offset + limit]

    # All threads: fetch globally sorted by created_at
    cur = conn.execute("""
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        ORDER BY created_at ASC
        LIMIT ? OFFSET ?
    """, (limit, offset))

    messages: List[Message] = []
    for row in cur.fetchall():
        msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
        metadata = {}
        if metadata_raw:
            try:
                import json
                metadata = json.loads(metadata_raw)
            except:
                pass
        messages.append(Message(
            id=msg_id,
            thread_id=tid,
            role=role,
            content=content,
            resource_id=resource_id,
            created_at=created_at,
            metadata=metadata,
        ))

    conn.close()
    return messages


def print_stats(conn, persona_id: str) -> None:
    """Print chronicle statistics."""
    stats = get_episode_summary_stats(conn)

    print("\n" + "=" * 60)
    print(f"Chronicle Statistics for: {persona_id}")
    print("=" * 60)
    print(f"Total messages covered: {stats['total_messages_covered']}")
    print(f"Maximum level: {stats['max_level']}")

    if stats['entries_by_level']:
        print("\nEntries by level:")
        for level, count in sorted(stats['entries_by_level'].items()):
            unconsolidated = stats['unconsolidated_by_level'].get(level, 0)
            level_name = "Chronicle" if level == 1 else "Chronicle" + "'s Chronicle" * (level - 1)
            print(f"  Level {level} ({level_name}): {count} total, {unconsolidated} unconsolidated")
    else:
        print("\nNo chronicle entries yet.")

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
        print(f"\n[1] All chronicle in DB (max_level={max_level}):")
        for level in range(1, max_level + 1):
            entries = get_entries_by_level(conn, level, order_by_time=True)
            print(f"  Level {level}: {len(entries)} entries")
            for e in entries[:5]:  # Show first 5
                print(f"    - id={e.id[:8]}... end_time={e.end_time} source_ids={len(e.source_ids)}")
            if len(entries) > 5:
                print(f"    ... and {len(entries) - 5} more")

        # Show sorted list
        all_arasuji = _get_all_arasuji_sorted(conn)
        print(f"\n[2] All chronicle sorted by end_time desc: {len(all_arasuji)} total")
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


def export_arasuji(conn, output_path: Path) -> int:
    """Export all chronicle entries to a JSON file.

    Args:
        conn: Database connection
        output_path: Path to the output JSON file

    Returns:
        Number of entries exported
    """
    import json

    entries = get_all_entries_ordered(conn)
    data = {
        "version": 1,
        "exported_at": int(__import__("time").time()),
        "entries": [e.to_dict() for e in entries],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return len(entries)


def import_arasuji(conn, input_path: Path, clear_existing: bool = False) -> int:
    """Import chronicle entries from a JSON file.

    Args:
        conn: Database connection
        input_path: Path to the input JSON file
        clear_existing: If True, clear existing entries before import

    Returns:
        Number of entries imported
    """
    import json

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if clear_existing:
        clear_all_entries(conn)

    entries_data = data.get("entries", [])
    imported = 0

    # First pass: Create all entries without parent references
    for entry_data in entries_data:
        create_entry(
            conn,
            level=entry_data["level"],
            content=entry_data["content"],
            source_ids=entry_data.get("source_ids", []),
            start_time=entry_data.get("start_time"),
            end_time=entry_data.get("end_time"),
            source_count=entry_data.get("source_count", 0),
            message_count=entry_data.get("message_count", 0),
            entry_id=entry_data["id"],
        )
        imported += 1

    # Second pass: Restore consolidation relationships
    for entry_data in entries_data:
        if entry_data.get("is_consolidated") and entry_data.get("parent_id"):
            mark_consolidated(conn, [entry_data["id"]], entry_data["parent_id"])

    return imported


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


def regenerate_entry_from_messages(
    conn: sqlite3.Connection,
    messages: List[Message],
    model_name: str = None,
) -> Optional[Any]:
    """Regenerate a Chronicle entry from messages.
    
    This function contains the business logic for regeneration:
    - Get LLM client based on model config
    - Call generate_level1_arasuji
    
    Args:
        conn: Database connection
        messages: Messages to regenerate from
        model_name: Model to use (defaults to MEMORY_WEAVE_MODEL env var)
        
    Returns:
        New ArasujiEntry or None on failure
    """
    import os
    from model_configs import find_model_config
    from llm_clients.factory import get_llm_client
    from sai_memory.arasuji.generator import generate_level1_arasuji
    
    # Get model from env if not specified
    if model_name is None:
        model_name = os.getenv("MEMORY_WEAVE_MODEL", "gemini-2.0-flash")
    
    # Find model config
    model_id, model_config = find_model_config(model_name)
    
    if not model_config:
        raise ValueError(f"Model '{model_name}' not found in config. Use --list-models to see available options.")
    
    actual_model_id = model_config.get("model", model_name)
    auto_provider = model_config.get("provider")
    if not auto_provider:
        raise ValueError(f"Model '{model_name}' is missing 'provider' in config.")
    
    provider = auto_provider
    context_length = model_config.get("context_length", 128000)
    
    # Get LLM client
    client = get_llm_client(actual_model_id, provider, context_length, config=model_config)
    
    # Generate Chronicle entry
    new_entry = generate_level1_arasuji(
        client,
        conn,
        messages,
        dry_run=False
    )
    
    return new_entry


def main():
    parser = argparse.ArgumentParser(
        description="Build Chronicle (episode memory, part of Memory Weave) from SAIMemory logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # デフォルトモデル (gemini-2.0-flash) で100件処理
  python scripts/build_arasuji.py air_city_a --limit 100

  # 101件目から100件処理
  python scripts/build_arasuji.py air_city_a --offset 100 --limit 100

  # ドライラン（保存せずにプレビュー）
  python scripts/build_arasuji.py air_city_a --limit 50 --dry-run

  # 統計情報を表示
  python scripts/build_arasuji.py air_city_a --stats

  # コンテキストプレビューを表示
  python scripts/build_arasuji.py air_city_a --preview-context

  # 全てのChronicleをクリア
  python scripts/build_arasuji.py air_city_a --clear-chronicle

  # 全てのMemopediaをクリア
  python scripts/build_memopedia.py air_city_a --clear-memopedia

  # 両方をクリア (Memory Weave全体)
  python scripts/build_arasuji.py air_city_a --clear-chronicle --clear-memopedia

  # バッチサイズと統合サイズを変更
  python scripts/build_arasuji.py air_city_a --batch-size 30 --consolidation-size 5

  # 日時情報を省略（インポートしたログで日時が不正確な場合）
  python scripts/build_arasuji.py air_city_a --no-timestamp

  # ChronicleをJSONにエクスポート
  python scripts/build_arasuji.py air_city_a --export chronicle_backup.json

  # ChronicleをJSONからインポート（既存を保持して追加）
  python scripts/build_arasuji.py air_city_a --import chronicle_backup.json

  # ChronicleをJSONからインポート（既存をクリアして置換）
  python scripts/build_arasuji.py air_city_a --import chronicle_backup.json --clear

  # Memory Weave: Chronicle と Memopedia を同時生成
  python scripts/build_arasuji.py air_city_a --limit 100 --with-memopedia

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
        "--model", default=ENV_MODEL,
        help=f"Model to use for generation (default: {ENV_MODEL}, env: MEMORY_WEAVE_MODEL)"
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
        "--batch-size", type=int, default=ENV_BATCH_SIZE,
        help=f"Number of messages per level-1 Chronicle (default: {ENV_BATCH_SIZE}, env: MEMORY_WEAVE_BATCH_SIZE)"
    )
    parser.add_argument(
        "--consolidation-size", type=int, default=ENV_CONSOLIDATION_SIZE,
        help=f"Number of entries per higher-level Chronicle (default: {ENV_CONSOLIDATION_SIZE}, env: MEMORY_WEAVE_CONSOLIDATION_SIZE)"
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="List available models and exit"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Show Chronicle statistics (part of Memory Weave) and exit"
    )
    parser.add_argument(
        "--preview-context", action="store_true",
        help="Preview the Chronicle context that would be injected"
    )
    parser.add_argument(
        "--clear-chronicle", action="store_true",
        help="Clear all Chronicle entries and exit"
    )
    parser.add_argument(
        "--clear-memopedia", action="store_true",
        help="Clear all Memopedia pages and exit"
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
    parser.add_argument(
        "--export", type=str, metavar="FILE",
        help="Export all Chronicle entries to a JSON file"
    )
    parser.add_argument(
        "--import", dest="import_file", type=str, metavar="FILE",
        help="Import Chronicle entries from a JSON file"
    )
    parser.add_argument(
        "--with-memopedia", action="store_true",
        help="Also generate Memopedia pages from the same messages (Memory Weave)"
    )
    parser.add_argument(
        "--debug-log", type=str, metavar="FILE",
        help="Output prompts and LLM responses to a log file for debugging"
    )
    parser.add_argument(
        "--maintain-interval", type=int, metavar="N", default=ENV_MAINTAIN_INTERVAL,
        help=f"Run maintain_memopedia every N messages processed (default: {ENV_MAINTAIN_INTERVAL}, env: MEMORY_WEAVE_MAINTAIN_INTERVAL)"
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

    # Handle --clear-chronicle (standalone, without --import)
    if args.clear_chronicle and not args.import_file:
        LOGGER.info("Clearing all Chronicle entries...")
        deleted = clear_all_entries(conn)
        LOGGER.info(f"Deleted {deleted} Chronicle entries")
        if not args.clear_memopedia:
            conn.close()
            sys.exit(0)

    # Handle --clear-memopedia
    if args.clear_memopedia:
        LOGGER.info("Clearing all Memopedia pages...")
        try:
            from sai_memory.memopedia import Memopedia, init_memopedia_tables
            init_memopedia_tables(conn)
            memopedia = Memopedia(conn)
            deleted = memopedia.clear_all_pages()
            LOGGER.info(f"Deleted {deleted} Memopedia pages")
        except ImportError as e:
            LOGGER.error(f"Memopedia module not available: {e}")
        except Exception as e:
            LOGGER.error(f"Failed to clear Memopedia: {e}")
        conn.close()
        sys.exit(0)

    # Handle --export
    if args.export:
        output_path = Path(args.export)
        LOGGER.info(f"Exporting chronicle to: {output_path}")
        count = export_arasuji(conn, output_path)
        LOGGER.info(f"Exported {count} entries to {output_path}")
        conn.close()
        sys.exit(0)

    # Handle --import
    if args.import_file:
        input_path = Path(args.import_file)
        if not input_path.exists():
            LOGGER.error(f"Import file not found: {input_path}")
            conn.close()
            sys.exit(1)
        LOGGER.info(f"Importing chronicle from: {input_path}")
        if args.clear_chronicle:
            LOGGER.info("Clearing existing entries before import...")
        count = import_arasuji(conn, input_path, clear_existing=args.clear_chronicle)
        LOGGER.info(f"Imported {count} entries from {input_path}")
        print_stats(conn, args.persona_id)
        conn.close()
        sys.exit(0)

    LOGGER.info(f"Building chronicle for persona: {args.persona_id}")
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
        LOGGER.error(f"Model '{args.model}' not found in config.")
        LOGGER.error("Use --list-models to see available options.")
        conn.close()
        sys.exit(1)

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

    # Get Memopedia context for semantic memory (Memory Weave)
    memopedia_context = None
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables

        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        memopedia_context = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if memopedia_context and memopedia_context != "(まだページはありません)":
            LOGGER.info(f"Using Memopedia context for semantic memory ({len(memopedia_context)} chars)")
        else:
            memopedia_context = None
    except ImportError as e:
        LOGGER.debug(f"Memopedia modules not available: {e}")
    except Exception as e:
        LOGGER.warning(f"Failed to get Memopedia context: {e}")

    # Generate chronicle
    generator = ArasujiGenerator(
        client,
        conn,
        batch_size=args.batch_size,
        consolidation_size=args.consolidation_size,
        include_timestamp=not args.no_timestamp,
        memopedia_context=memopedia_context,
    )

    # Set debug log path if specified
    if args.debug_log:
        generator.debug_log_path = Path(args.debug_log)
        LOGGER.info(f"Debug logging enabled: {args.debug_log}")

    def progress_callback(processed: int, total: int) -> None:
        if total > 0:
            pct = (processed / total) * 100
            LOGGER.info(f"Progress: {processed}/{total} ({pct:.1f}%)")

    # Set up Memopedia batch callback if --with-memopedia is enabled (interleaved Memory Weave)
    batch_callback = None
    memopedia_pages_total = 0
    messages_since_maintain = 0

    if args.with_memopedia:
        try:
            from sai_memory.memopedia import Memopedia, init_memopedia_tables
            from scripts.build_memopedia import extract_knowledge

            # Initialize Memopedia tables
            init_memopedia_tables(conn)
            memopedia = Memopedia(conn)
            debug_log_path = Path(args.debug_log) if args.debug_log else None

            def memopedia_batch_callback(batch_messages):
                """Extract Memopedia pages for each batch (interleaved with Chronicle)."""
                nonlocal memopedia_pages_total, messages_since_maintain

                if not batch_messages:
                    return

                LOGGER.info(f"  [Memory Weave] Extracting Memopedia from batch ({len(batch_messages)} messages)...")

                # Update Memopedia context for Generator (semantic memory gets updated per batch)
                updated_memopedia_context = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
                if updated_memopedia_context and updated_memopedia_context != "(まだページはありません)":
                    generator.memopedia_context = updated_memopedia_context

                try:
                    pages = extract_knowledge(
                        client,
                        batch_messages,
                        memopedia,
                        batch_size=len(batch_messages),  # Process as single batch
                        dry_run=args.dry_run,
                        refine_writes=True,  # Use LLM to integrate content instead of simple append
                        episode_context_conn=conn,
                        debug_log_path=debug_log_path,
                    )
                    memopedia_pages_total += len(pages)
                    LOGGER.info(f"  [Memory Weave] Extracted {len(pages)} pages (total: {memopedia_pages_total})")

                    # Track messages for maintain_memopedia
                    messages_since_maintain += len(batch_messages)

                    # Run maintain_memopedia if interval is reached
                    if args.maintain_interval > 0 and messages_since_maintain >= args.maintain_interval:
                        LOGGER.info(f"  [Memory Weave] Running maintain_memopedia (interval: {args.maintain_interval})...")
                        try:
                            from scripts.maintain_memopedia import run_merge_similar, run_fix_markdown
                            run_fix_markdown(memopedia, dry_run=args.dry_run)
                            run_merge_similar(memopedia, client, dry_run=args.dry_run)
                            messages_since_maintain = 0
                            LOGGER.info("  [Memory Weave] Maintenance complete")
                        except Exception as e:
                            LOGGER.warning(f"  [Memory Weave] Maintenance failed: {e}")

                except Exception as e:
                    LOGGER.error(f"  [Memory Weave] Memopedia extraction failed: {e}")

            batch_callback = memopedia_batch_callback
            LOGGER.info("Memory Weave mode: Memopedia extraction will run per batch (interleaved)")

        except ImportError as e:
            LOGGER.error(f"Failed to import Memopedia modules: {e}")
            LOGGER.error("Memopedia extraction disabled. Make sure sai_memory.memopedia is available.")

    level1_entries, consolidated_entries = generator.generate_from_messages(
        messages,
        dry_run=args.dry_run,
        progress_callback=progress_callback,
        batch_callback=batch_callback,
    )

    LOGGER.info(f"Generated {len(level1_entries)} level-1 chronicle")
    LOGGER.info(f"Generated {len(consolidated_entries)} consolidated chronicle")
    if args.with_memopedia:
        LOGGER.info(f"Generated {memopedia_pages_total} Memopedia pages (interleaved)")

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
