#!/usr/bin/env python3
"""Import a JSON chat log into a persona's SAIMemory as a new thread.

Usage:
    python scripts/import_chatlog_json.py <persona_id> <json_file> [--thread <suffix>] [--start-time <ISO8601>]

Example:
    python scripts/import_chatlog_json.py aifi_city_a chatlog.json --thread "imported_2026_01"
    python scripts/import_chatlog_json.py aifi_city_a chatlog.json --start-time "2026-01-01T12:00:00"

JSON format:
[
  {"role": "user", "content": "Hello!", "token_count": 5},
  {"role": "model", "content": "Hi there!", "token_count": 4}
]

Notes:
- "role" can be "user" or "model" (model will be converted to "assistant")
- "token_count" is optional and will be stored in metadata
- Messages will be assigned timestamps starting from --start-time (or now)
  with 1-second intervals
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.storage import add_message, get_or_create_thread, init_db
from pathlib import Path as PathLib

LOGGER = logging.getLogger("import_chatlog_json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import JSON chat log into SAIMemory as a new thread"
    )
    parser.add_argument("persona_id", help="Target persona ID (e.g., aifi_city_a)")
    parser.add_argument("json_file", type=Path, help="Path to JSON chat log file")
    parser.add_argument(
        "--thread",
        dest="thread_suffix",
        help="Thread suffix for the new thread (default: auto-generated from filename)",
    )
    parser.add_argument(
        "--start-time",
        dest="start_time",
        help="Start timestamp in ISO8601 format (default: now)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=1,
        help="Seconds between messages (default: 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be imported without actually writing",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding creation (faster import, but no semantic search)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


def load_chatlog(path: Path) -> list[dict]:
    """Load and validate JSON chat log."""
    if not path.exists():
        raise FileNotFoundError(f"Chat log file not found: {path}")
    
    with path.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    
    if not isinstance(data, list):
        raise ValueError("JSON must be a list of message objects")
    
    messages = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Message {i} is not an object")
        if "role" not in item:
            raise ValueError(f"Message {i} missing 'role' field")
        if "content" not in item:
            raise ValueError(f"Message {i} missing 'content' field")
        messages.append(item)
    
    return messages


def import_messages(
    adapter: SAIMemoryAdapter,
    messages: list[dict],
    thread_suffix: str,
    start_time: datetime,
    interval_seconds: int,
    dry_run: bool = False,
    skip_embed: bool = False,
) -> int:
    """Import messages into SAIMemory.
    
    Returns:
        Number of messages imported
    """
    count = 0
    current_time = start_time
    
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        
        # Build metadata
        metadata: dict = {"tags": ["conversation", "imported"]}
        if "token_count" in msg:
            metadata["token_count"] = msg["token_count"]
        
        timestamp = current_time.isoformat()
        
        if dry_run:
            LOGGER.info(
                "[DRY-RUN] Would import: role=%s, time=%s, content=%s...",
                role,
                timestamp,
                content[:50] if len(content) > 50 else content,
            )
        else:
            message_data = {
                "role": role,
                "content": content,
                "timestamp": timestamp,
                "metadata": metadata,
            }
            if skip_embed:
                message_data["embedding_chunks"] = 0
            adapter._append_message(
                building_id=None,
                message=message_data,
                thread_suffix=thread_suffix,
            )
            LOGGER.debug("Imported message: role=%s, time=%s", role, timestamp)
        
        count += 1
        current_time += timedelta(seconds=interval_seconds)
    
    return count


def import_messages_direct(
    conn,
    persona_id: str,
    messages: list[dict],
    thread_suffix: str,
    start_time: datetime,
    interval_seconds: int,
    dry_run: bool = False,
) -> int:
    """Import messages directly using low-level storage functions (no embeddings).
    
    Returns:
        Number of messages imported
    """
    import json as json_module
    
    count = 0
    current_time = start_time
    thread_id = f"{persona_id}:{thread_suffix}"
    
    # Create thread first
    if not dry_run:
        get_or_create_thread(conn, thread_id, resource_id=persona_id)
    
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        
        # Build metadata
        metadata: dict = {"tags": ["conversation", "imported"]}
        if "token_count" in msg:
            metadata["token_count"] = msg["token_count"]
        
        ts_epoch = int(current_time.timestamp())
        
        if dry_run:
            LOGGER.info(
                "[DRY-RUN] Would import: role=%s, time=%s, content=%s...",
                role,
                current_time.isoformat(),
                content[:50] if len(content) > 50 else content,
            )
        else:
            add_message(
                conn,
                thread_id=thread_id,
                role=role,
                content=content,
                resource_id=persona_id,
                created_at=ts_epoch,
                metadata=metadata,
            )
            LOGGER.debug("Imported message: role=%s, time=%s", role, current_time.isoformat())
        
        count += 1
        current_time += timedelta(seconds=interval_seconds)
    
    return count


def main() -> int:
    args = parse_args()
    
    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    
    # Load chat log
    try:
        messages = load_chatlog(args.json_file)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        LOGGER.error("Failed to load chat log: %s", e)
        return 1
    
    if not messages:
        LOGGER.warning("No messages found in chat log")
        return 0
    
    LOGGER.info("Loaded %d messages from %s", len(messages), args.json_file)
    
    # Determine thread suffix
    if args.thread_suffix:
        thread_suffix = args.thread_suffix
    else:
        # Auto-generate from filename + timestamp
        stem = args.json_file.stem
        now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        thread_suffix = f"imported_{stem}_{now_str}"
    
    LOGGER.info("Thread suffix: %s", thread_suffix)
    
    # Parse start time
    if args.start_time:
        try:
            start_time = datetime.fromisoformat(args.start_time)
        except ValueError as e:
            LOGGER.error("Invalid start time format: %s", e)
            return 1
    else:
        start_time = datetime.now()
    
    LOGGER.info("Start time: %s", start_time.isoformat())
    
    # Import messages
    if args.no_embed:
        LOGGER.info("Embedding creation: SKIPPED (using direct DB access)")
        # Use direct DB access to avoid loading embedder
        persona_dir = PathLib.home() / ".saiverse" / "personas" / args.persona_id
        persona_dir.mkdir(parents=True, exist_ok=True)
        db_path = persona_dir / "memory.db"
        
        try:
            conn = init_db(str(db_path), check_same_thread=True)
        except Exception as e:
            LOGGER.error("Failed to initialize database: %s", e)
            return 1
        
        try:
            count = import_messages_direct(
                conn,
                args.persona_id,
                messages,
                thread_suffix,
                start_time,
                args.interval,
                dry_run=args.dry_run,
            )
        except Exception as e:
            LOGGER.exception("Failed to import messages: %s", e)
            return 1
        finally:
            conn.close()
    else:
        # Use SAIMemoryAdapter (with embedding)
        try:
            adapter = SAIMemoryAdapter(args.persona_id)
            if not adapter.is_ready():
                LOGGER.error("SAIMemory adapter failed to initialize")
                return 1
        except Exception as e:
            LOGGER.error("Failed to initialize adapter: %s", e)
            return 1
        
        try:
            count = import_messages(
                adapter,
                messages,
                thread_suffix,
                start_time,
                args.interval,
                dry_run=args.dry_run,
                skip_embed=False,
            )
        except Exception as e:
            LOGGER.exception("Failed to import messages: %s", e)
            return 1
    
    action = "Would import" if args.dry_run else "Imported"
    LOGGER.info(
        "%s %d messages into thread: %s:%s",
        action,
        count,
        args.persona_id,
        thread_suffix,
    )
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

