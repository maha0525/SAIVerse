"""Extract memory notes from existing messages for a persona.

Usage:
    python scripts/extract_memory_notes.py <persona_id> [--limit N] [--dry-run]

Examples:
    # Extract from the last 100 messages (default)
    python scripts/extract_memory_notes.py air_city_a

    # Extract from the last 200 messages
    python scripts/extract_memory_notes.py air_city_a --limit 200

    # Dry run (show what would be extracted, don't save)
    python scripts/extract_memory_notes.py air_city_a --dry-run

    # Use a specific model
    python scripts/extract_memory_notes.py air_city_a --model gemini-2.0-flash
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    parser = argparse.ArgumentParser(description="Extract memory notes from persona messages")
    parser.add_argument("persona_id", help="Persona ID (e.g., air_city_a)")
    parser.add_argument("--limit", type=int, default=100, help="Number of recent messages to process (default: 100)")
    parser.add_argument("--batch-size", type=int, default=20, help="Messages per extraction batch (default: 20)")
    parser.add_argument("--model", type=str, default=None, help="Model to use (default: MEMORY_WEAVE_MODEL or built-in lite)")
    parser.add_argument("--dry-run", action="store_true", help="Show extracted notes without saving")
    args = parser.parse_args()

    from sai_memory.memory.storage import init_db, Message
    from sai_memory.arasuji import init_arasuji_tables

    # Find database
    db_path = Path.home() / ".saiverse" / "personas" / args.persona_id / "memory.db"
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(1)

    print(f"Opening database: {db_path}")
    conn = init_db(str(db_path), check_same_thread=False)
    init_arasuji_tables(conn)

    # Fetch recent messages
    cur = conn.execute(
        """
        SELECT id, thread_id, role, content, resource_id, created_at, metadata
        FROM messages
        WHERE thread_id NOT IN (SELECT thread_id FROM stelis_threads)
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (args.limit,),
    )

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

    # Reverse to chronological order
    messages.reverse()

    if not messages:
        print("No messages found")
        conn.close()
        sys.exit(0)

    print(f"Found {len(messages)} messages (oldest: {messages[0].created_at}, newest: {messages[-1].created_at})")

    # Initialize LLM client
    from saiverse.model_defaults import BUILTIN_DEFAULT_LITE_MODEL
    from saiverse.model_configs import find_model_config
    from llm_clients.factory import get_llm_client

    model_name = args.model or os.getenv("MEMORY_WEAVE_MODEL", BUILTIN_DEFAULT_LITE_MODEL)
    resolved_model_id, model_config = find_model_config(model_name)
    if not resolved_model_id:
        print(f"Model '{model_name}' not found")
        conn.close()
        sys.exit(1)

    provider = model_config.get("provider", "gemini")
    context_length = model_config.get("context_length", 128000)
    client = get_llm_client(resolved_model_id, provider, context_length, config=model_config)
    print(f"Using model: {model_config.get('model', resolved_model_id)} / {provider}")

    # Get Memopedia context
    memopedia_context = ""
    try:
        from sai_memory.memopedia import Memopedia, init_memopedia_tables
        init_memopedia_tables(conn)
        memopedia = Memopedia(conn)
        memopedia_context = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if memopedia_context == "(まだページはありません)":
            memopedia_context = ""
        if memopedia_context:
            print(f"Memopedia context loaded ({len(memopedia_context)} chars)")
    except Exception as e:
        print(f"Warning: Could not load Memopedia context: {e}")

    # Process in batches
    from sai_memory.memory.note_extractor import extract_memory_notes
    from sai_memory.memory.storage import add_memory_notes, get_unresolved_notes
    from sai_memory.arasuji.context import get_episode_context_for_timerange

    total_notes = 0
    batch_count = 0

    for i in range(0, len(messages), args.batch_size):
        batch = messages[i:i + args.batch_size]
        if len(batch) < args.batch_size // 2:
            print(f"  Skipping small final batch ({len(batch)} messages)")
            continue

        batch_count += 1
        start_time = min(m.created_at for m in batch)
        end_time = max(m.created_at for m in batch)

        # Episode context
        ep_ctx = ""
        try:
            ep_ctx = get_episode_context_for_timerange(
                conn, start_time=start_time, end_time=end_time, max_entries=10,
            )
        except Exception:
            pass

        # Existing notes for dedup
        existing = get_unresolved_notes(conn, limit=200)
        existing_contents = [n.content for n in existing]

        print(f"\nBatch {batch_count}: messages {i+1}-{i+len(batch)} "
              f"({start_time} - {end_time})")

        notes = extract_memory_notes(
            client, batch,
            episode_context=ep_ctx,
            memopedia_context=memopedia_context,
            existing_notes=existing_contents,
            persona_id=args.persona_id,
        )

        if notes:
            for note in notes:
                print(f"  - {note}")

            if not args.dry_run:
                stored = add_memory_notes(
                    conn, thread_id="main", notes=notes,
                    source_time=end_time,
                )
                print(f"  -> Saved {len(stored)} notes")

            total_notes += len(notes)
        else:
            print("  (no notes extracted)")

    print(f"\nDone: {total_notes} notes extracted from {batch_count} batches")
    if args.dry_run:
        print("(dry-run mode, nothing saved)")

    conn.close()


if __name__ == "__main__":
    main()
