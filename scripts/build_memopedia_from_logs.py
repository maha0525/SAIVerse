"""Build Memopedia knowledge base from existing chat logs.

Processes messages from oldest to newest, extracting entities and their
knowledge in batches, then reflecting them directly to Memopedia pages.

Usage:
    python scripts/build_memopedia_from_logs.py <persona_id> [options]

Examples:
    # Full run with defaults
    python scripts/build_memopedia_from_logs.py eris_city_a

    # Dry run (show what would be extracted, no DB writes)
    python scripts/build_memopedia_from_logs.py eris_city_a --dry-run

    # Process only first 500 messages
    python scripts/build_memopedia_from_logs.py eris_city_a --limit 500

    # Resume from a specific timestamp
    python scripts/build_memopedia_from_logs.py eris_city_a --start-after 1711900000
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _init_db(persona_id: str):
    from sai_memory.memory.storage import init_db
    from sai_memory.arasuji import init_arasuji_tables

    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)
    return conn


def _init_llm(model_name: str = None):
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    model_name = model_name or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
    resolved_model_id, model_config = find_model_config(model_name)
    if not resolved_model_id:
        print(f"Model '{model_name}' not found")
        sys.exit(1)

    provider = model_config.get("provider", "gemini")
    context_length = model_config.get("context_length", 128000)
    client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
    print(f"Using model: {model_config.get('model', resolved_model_id)} / {provider}")
    return client


def _init_memopedia(conn):
    from sai_memory.memopedia import Memopedia, init_memopedia_tables
    init_memopedia_tables(conn)
    return Memopedia(conn)


def _fetch_messages(conn, *, limit: int = 0, start_after: float = 0):
    from sai_memory.memory.storage import Message

    query = """
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE thread_id NOT IN (SELECT thread_id FROM stelis_threads)
    """
    params = []

    if start_after > 0:
        query += " AND created_at > ?"
        params.append(start_after)

    query += " ORDER BY created_at ASC"

    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    cur = conn.execute(query, params)

    messages = []
    for row in cur.fetchall():
        msg_id, tid, role, content, resource_id, created_at, metadata_raw = row
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
            except Exception:
                pass
        messages.append(Message(
            id=msg_id, thread_id=tid, role=role, content=content,
            resource_id=resource_id, created_at=created_at, metadata=metadata,
        ))

    return messages


def main():
    parser = argparse.ArgumentParser(
        description="Build Memopedia knowledge base from chat logs",
    )
    parser.add_argument("persona_id", help="Persona ID (e.g., eris_city_a)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages to process (0=all)")
    parser.add_argument("--batch-size", type=int, default=20, help="Messages per extraction batch (default: 20)")
    parser.add_argument("--model", type=str, default=None, help="Model to use for extraction")
    parser.add_argument("--start-after", type=float, default=0, help="Process messages after this timestamp (for resuming)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()

    conn = _init_db(args.persona_id)
    client = _init_llm(args.model)
    memopedia = _init_memopedia(conn)

    messages = _fetch_messages(conn, limit=args.limit, start_after=args.start_after)
    if not messages:
        print("No messages found")
        conn.close()
        return

    print(f"Found {len(messages)} messages to process")
    print(f"  Oldest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[0].created_at))}")
    print(f"  Newest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[-1].created_at))}")
    print(f"  Batch size: {args.batch_size}")
    print()

    from sai_memory.memory.entity_extractor import (
        extract_entities,
        reflect_to_memopedia,
        _format_page_list,
    )
    from sai_memory.arasuji.context import get_episode_context_for_timerange

    total_entities = 0
    total_notes = 0
    total_new_pages = 0
    total_updated_pages = 0
    batch_count = 0

    for i in range(0, len(messages), args.batch_size):
        batch = messages[i:i + args.batch_size]
        if len(batch) < args.batch_size // 2:
            print(f"  Skipping small final batch ({len(batch)} messages)")
            continue

        batch_count += 1
        start_time = min(m.created_at for m in batch)
        end_time = max(m.created_at for m in batch)
        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_time))

        # Episode context
        ep_ctx = ""
        try:
            ep_ctx = get_episode_context_for_timerange(
                conn, start_time=start_time, end_time=end_time, max_entries=10,
            )
        except Exception:
            pass

        # Existing pages (refreshed each batch to include newly created pages)
        existing_pages = _format_page_list(memopedia)

        print(f"[Batch {batch_count}] {time_str} | msgs {i+1}-{i+len(batch)}/{len(messages)}", end="")

        entities = extract_entities(
            client, batch,
            episode_context=ep_ctx,
            existing_pages=existing_pages,
            persona_id=args.persona_id,
        )

        if not entities:
            print(" → 0 entities")
            continue

        print(f" → {len(entities)} entities")
        for ent in entities:
            print(f"    [{ent.category}] {ent.name}:")
            for note in ent.notes:
                print(f"      - {note}")

        if not args.dry_run:
            results = reflect_to_memopedia(
                entities, memopedia,
                source_time=int(end_time),
            )
            for r in results:
                status = "NEW" if r.is_new_page else "UPDATE"
                print(f"    → [{status}] {r.entity_name} ({r.notes_appended} notes)")
            total_new_pages += sum(1 for r in results if r.is_new_page)
            total_updated_pages += sum(1 for r in results if not r.is_new_page)

        total_entities += len(entities)
        total_notes += sum(len(e.notes) for e in entities)

    # Summary
    print(f"\n{'='*60}")
    print("Done!")
    print(f"  Messages processed: {batch_count * args.batch_size} (in {batch_count} batches)")
    print(f"  Entities found: {total_entities}")
    print(f"  Notes extracted: {total_notes}")
    if not args.dry_run:
        print(f"  New pages created: {total_new_pages}")
        print(f"  Existing pages updated: {total_updated_pages}")
    if messages:
        print(f"  Last message timestamp: {messages[-1].created_at}")
        print(f"  (Use --start-after {messages[-1].created_at} to resume)")
    if args.dry_run:
        print("  (dry-run mode, nothing saved)")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
