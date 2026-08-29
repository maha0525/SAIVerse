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
import sqlite3
import sys
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sai_memory.arasuji.estimate import ChronicleCostEstimate

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

load_dotenv()

# Skip tool imports to avoid circular import issue
os.environ["SAIVERSE_SKIP_TOOL_IMPORTS"] = "1"

from sai_memory.memory.storage import init_db, get_messages_paginated, Message
from sai_memory.arasuji import init_arasuji_tables
from sai_memory.arasuji.storage import (
    ArasujiEntry,
    create_entry,
    get_max_level,
    get_all_entries_ordered,
    clear_all_entries,
    update_progress,
    mark_consolidated,
)
from sai_memory.arasuji.generator import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_CONSOLIDATION_SIZE,
    generate_level1_arasuji,
)
from sai_memory.arasuji.context import (
    get_episode_context,
    format_episode_context,
    get_episode_summary_stats,
    get_episode_context_for_timerange,
)
from saiverse.model_configs import find_model_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger(__name__)

# Environment variable configuration for Memory Weave
from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
ENV_MODEL = os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
ENV_BATCH_SIZE = int(os.getenv("MEMORY_WEAVE_BATCH_SIZE", str(DEFAULT_BATCH_SIZE)))
ENV_CONSOLIDATION_SIZE = int(os.getenv("MEMORY_WEAVE_CONSOLIDATION_SIZE", str(DEFAULT_CONSOLIDATION_SIZE)))


def get_persona_db_path(persona_id: str) -> Path:
    """Get the path to a persona's memory.db file."""
    return Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse") / "personas" / persona_id / "memory.db"


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

    # All threads: use shared Chronicle message fetcher
    from sai_memory.memory.storage import get_messages_for_chronicle
    messages = get_messages_for_chronicle(conn, limit=limit, offset=offset)

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


def print_cost_estimate(
    conn,
    persona_id: str,
    *,
    model_name: str,
) -> "ChronicleCostEstimate":
    """未処理メッセージから Chronicle を一括生成した場合の費用を見積もり表示する。

    LLM は一切呼ばない。api/routes/people/arasuji.py の cost-estimate エンドポイント
    (UI 用) と同じロジック (sai_memory.arasuji.estimate = episode 整列計画) を使う。
    """
    from sai_memory.arasuji.estimate import estimate_chronicle_generation_cost

    estimate = estimate_chronicle_generation_cost(
        conn,
        model_name=model_name,
    )

    print("\n" + "=" * 60)
    print(f"Chronicle 一括生成 費用見積もり: {persona_id}")
    print("=" * 60)
    print(f"総メッセージ数:         {estimate.total_messages}")
    print(f"処理済みメッセージ数:   {estimate.processed_messages}")
    print(f"未処理メッセージ数:     {estimate.unprocessed_messages}")
    print(f"圧縮チャンク (LLM):     {estimate.level1_calls}")
    print(f"統合コール数 (概算):    {estimate.consolidation_calls}")
    print(f"LLM コール数合計:       {estimate.estimated_llm_calls}")
    print(f"使用モデル:             {estimate.model_name}")
    if estimate.is_free_tier:
        print("概算費用:               不明 (このモデルには pricing 情報がありません。コール数のみ参考にしてください)")
    else:
        print(f"概算費用:               {estimate.estimated_cost_usd:.4f} {estimate.currency} (目安。実際の入出力量により変動)")
    print("=" * 60)

    return estimate


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
        print("\n[3] Algorithm execution:")
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
    from saiverse.model_configs import MODEL_CONFIGS, get_model_display_name

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
    persona_id: str = None,
    extra_items: Optional[List[dict]] = None,
) -> Optional[Any]:
    """Regenerate a Chronicle entry from messages.

    This function contains the business logic for regeneration:
    - Get LLM client based on model config
    - Call generate_level1_arasuji

    Args:
        conn: Database connection
        messages: Messages to regenerate from
        model_name: Model to use (defaults to MEMORY_WEAVE_MODEL env var)
        persona_id: Optional persona ID for usage tracking
        extra_items: メッセージ行ではない材料 (旧 entry の材料だった知覚
            バッチ等、``{"at", "text"}`` の list)。generate_level1_arasuji へ
            そのまま渡す

    Returns:
        New ArasujiEntry or None on failure
    """
    import os
    from llm_clients.factory import get_llm_client
    
    # Get model from env if not specified
    if model_name is None:
        model_name = os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
    
    # Find model config
    model_id, model_config = find_model_config(model_name)
    
    if not model_config:
        raise ValueError(f"Model '{model_name}' not found in config. Use --list-models to see available options.")
    
    auto_provider = model_config.get("provider")
    if not auto_provider:
        raise ValueError(f"Model '{model_name}' is missing 'provider' in config.")
    
    provider = auto_provider
    context_length = model_config.get("context_length", 128000)
    
    # Get LLM client
    # factory の第一引数は設定キー。API 名を渡すと client.config_key が API 名になり、
    # 使用量が同名の従量課金版設定の単価で記録される (docs/intent/
    # model_provider_management.md「使用量の帰属」)。API 名は config から解決される。
    client = get_llm_client(model_id, provider, context_length, config=model_config)
    
    # Generate Chronicle entry
    new_entry = generate_level1_arasuji(
        client,
        conn,
        messages,
        dry_run=False,
        persona_id=persona_id,
        extra_items=extra_items,
    )

    return new_entry





def run_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Build Chronicle (episode memory, part of Memory Weave) from SAIMemory logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # デフォルトモデルで100件処理
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
        help="(deprecated — W4 で廃止。episode 整列は全量から計画するため受理するが無視)"
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
        help="(deprecated — W4 で廃止。episode 整列 + サイズ束ねが分割を決める。受理するが無視)"
    )
    parser.add_argument(
        "--consolidation-size", type=int, default=ENV_CONSOLIDATION_SIZE,
        help="(deprecated — W4 で廃止。帯あふれ束ねが統合を決める。受理するが無視)"
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
        "--estimate", action="store_true",
        help="Show cost estimate for unprocessed messages and exit (no LLM calls, no writes)"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the cost-estimate confirmation prompt before generation"
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

    # Handle --estimate (見積もりのみ、LLM 呼び出しなし・書き込みなし)
    if args.estimate:
        resolved_model_id, model_config = find_model_config(args.model)
        # 価格は設定キーで引く。API 名を渡すと同名の従量課金版設定の単価が出る。
        estimate_model_name = resolved_model_id or args.model
        print_cost_estimate(
            conn,
            args.persona_id,
            model_name=estimate_model_name,
        )
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

    # 実行前の見積もり表示 (LLM 呼び出しなし)。--dry-run は書き込みが起きないので
    # 確認プロンプトは不要 (見積もりだけ表示して続行)。それ以外は --yes 無しなら確認する。
    # 見積もりは保持する — 束ねの実行は承認済みの統合コール数を上限にする
    # (実出力長のブレで連鎖が増えても、表示・承認した件数を超えない)。
    estimate = print_cost_estimate(
        conn,
        args.persona_id,
        # 価格は設定キーで引く (API 名だと同名の従量課金版設定の単価が出る)。
        model_name=resolved_model_id,
    )
    if not args.dry_run and not args.yes:
        answer = input("\nこの内容で Chronicle 生成を実行しますか？ [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            LOGGER.info("ユーザーの選択により中止しました。")
            conn.close()
            sys.exit(0)

    # Fetch messages — 整列は全量から計画し、上限は truncate_plan で切る
    # (fetch を limit で切ると episode の途中で分断され §4-2 に反する —
    # Codex W4 #7。--offset は同じ理由で廃止・無視)。
    from sai_memory.memory.storage import get_messages_for_chronicle
    messages = get_messages_for_chronicle(conn)
    if args.thread:
        messages = [m for m in messages if m.thread_id == args.thread]
    LOGGER.info(f"Fetched {len(messages)} messages (thread={args.thread or 'all'})")

    if not messages:
        LOGGER.warning("No messages found")
        conn.close()
        sys.exit(0)

    # Episode 整列計画 (W4 — Metabolism / API と同じ一点管理)
    from sai_memory.arasuji.alignment import (
        chronicle_band_budget,
        plan_alignment,
        truncate_plan,
    )
    cur = conn.execute(
        "SELECT DISTINCT json_each.value "
        "FROM arasuji_entries, json_each(source_ids_json) "
        "WHERE level = 1"
    )
    processed_ids = {row[0] for row in cur.fetchall()}
    plan = plan_alignment(
        messages,
        processed_ids,
        target_chars=chronicle_band_budget(),
    )
    plan = truncate_plan(plan, args.limit)

    if args.dry_run:
        # W4: dry-run は計画表示のみ (LLM を呼ばない。旧実装は LLM を呼んで
        # 保存だけ抑止していた — 見積もりに費用が発生する矛盾を廃止)
        print("\n[DRY RUN] 整列計画:")
        for i, chunk in enumerate(plan.chunks, 1):
            print(
                f"  #{i} kind={chunk.kind} messages={len(chunk.messages)} "
                f"coverage={chunk.coverage_chars}字 episodes={','.join(chunk.episode_refs) or '-'}"
            )
        print(f"  合計: {len(plan.chunks)} chunks (LLM {plan.llm_calls} 回)")
        conn.close()
        LOGGER.info("Done (dry run)!")
        return

    if not plan.chunks:
        LOGGER.info("No unprocessed messages to compile")
        conn.close()
        sys.exit(0)

    # Import factory directly to avoid circular import
    from llm_clients.factory import get_llm_client

    # 第一引数は設定キー (API 名を渡すと使用量が従量課金版の単価で記録される)。
    client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)

    def progress_callback(processed: int, total: int) -> None:
        if total > 0:
            pct = (processed / total) * 100
            LOGGER.info(f"Progress: {processed}/{total} ({pct:.1f}%)")

    # Set up Memopedia batch callback if --with-memopedia is enabled
    batch_callback = None
    if args.with_memopedia:
        try:
            from sai_memory.memopedia import init_memopedia_tables
            from sai_memory.memory.entity_extractor import make_batch_callback as make_entity_callback

            init_memopedia_tables(conn)
            batch_callback = make_entity_callback(
                client, conn,
                persona_id=args.persona_id,
            )
            LOGGER.info("Memory Weave mode: entity extraction will run per batch (interleaved)")
        except ImportError as e:
            LOGGER.error(f"Failed to import entity extractor modules: {e}")
            LOGGER.error("Memopedia extraction disabled.")

    from sai_memory.arasuji.bands import backfill_coverage, run_band_overflow
    from sai_memory.arasuji.executor import execute_plan

    try:
        backfill_coverage(conn)
    except Exception:
        LOGGER.exception("coverage backfill failed; continuing")

    exec_result = execute_plan(
        plan, client, conn,
        persona_id=args.persona_id,
        include_timestamp=not args.no_timestamp,
        progress_callback=progress_callback,
        batch_callback=batch_callback,
    )

    consolidated_count = 0
    try:
        consolidated_count = run_band_overflow(
            conn, client, persona_id=args.persona_id,
            # 確認時に表示した統合コール数を実行の上限にする
            max_folds=estimate.consolidation_calls,
        )
    except Exception:
        LOGGER.exception("band overflow consolidation failed; continuing")

    LOGGER.info(
        "[Summary] target_messages=%s created_chunks=%s skipped=%s consolidated=%s",
        len(messages), exec_result.created_count,
        exec_result.skipped_duplicates, consolidated_count,
    )

    # Update progress tracking (実際に計画へ載った末尾のみ — 全量の末尾を
    # 記録すると未処理分まで進捗済みに見える)
    if plan.chunks:
        update_progress(conn, plan.chunks[-1].messages[-1].id)

    # Show final state (dry-run は計画表示で早期 return 済み)
    print_stats(conn, args.persona_id)
    print("\n" + "-" * 60)
    print("Episode Context Preview:")
    print("-" * 60)
    print_context_preview(conn)

    conn.close()
    LOGGER.info("Done!")


if __name__ == "__main__":
    run_cli()
