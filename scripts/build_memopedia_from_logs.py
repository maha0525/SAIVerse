"""Build Memopedia knowledge base from existing chat logs.

Processes messages from oldest to newest, extracting memory notes in batches.
When unresolved notes reach a threshold, organizes them into Memopedia pages.
Repeats until all messages are processed.

Usage:
    python scripts/build_memopedia_from_logs.py <persona_id> [options]

Examples:
    # Full run with defaults (organize every 30 notes)
    python scripts/build_memopedia_from_logs.py eris_city_a

    # Customize thresholds
    python scripts/build_memopedia_from_logs.py eris_city_a --organize-threshold 20

    # Dry run (extract + organize preview, no DB writes)
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


def _get_memopedia_context(memopedia) -> str:
    try:
        ctx = memopedia.get_tree_markdown(include_keywords=False, show_markers=False)
        if ctx == "(まだページはありません)":
            return ""
        return ctx
    except Exception:
        return ""


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


def _run_organize(client, conn, memopedia, persona_id: str, dry_run: bool) -> int:
    """Run organize_notes and return number of notes resolved."""
    from sai_memory.memory.storage import count_unplanned_notes

    n_unplanned = count_unplanned_notes(conn)
    if n_unplanned == 0:
        return 0

    if dry_run:
        from sai_memory.memory.note_organizer import plan_notes
        tree = memopedia.get_tree()
        groups = plan_notes(client, conn, tree, persona_id=persona_id)
        total = sum(len(g.get("note_ids", [])) for g in groups)
        print(f"    [organize/dry-run] {len(groups)} groups, {total} notes planned")
        for g in groups:
            target = g.get("target_page_id") or g.get("suggested_title") or "?"
            print(f"      [{g['group_label']}] {len(g['note_ids'])} notes → {g['action']} ({target})")
        return 0
    else:
        from sai_memory.memory.note_organizer import organize_notes
        results = organize_notes(client, conn, memopedia, persona_id=persona_id)
        total = sum(r.note_count for r in results)
        print(f"    [organize] {len(results)} groups, {total} notes → Memopedia")
        for r in results:
            print(f"      [{r.group_label}] {r.action} → {r.page_id[:12]}... ({r.note_count} notes)")
        return total


def main():
    parser = argparse.ArgumentParser(
        description="Build Memopedia knowledge base from chat logs",
    )
    parser.add_argument("persona_id", help="Persona ID (e.g., eris_city_a)")
    parser.add_argument("--limit", type=int, default=0, help="Max messages to process (0=all)")
    parser.add_argument("--batch-size", type=int, default=20, help="Messages per extraction batch (default: 20)")
    parser.add_argument("--organize-threshold", type=int, default=30, help="Organize when unresolved notes reach this count (default: 30)")
    parser.add_argument("--model", type=str, default=None, help="Model to use for extraction and organization")
    parser.add_argument("--start-after", type=float, default=0, help="Process messages after this timestamp (for resuming)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()

    conn = _init_db(args.persona_id)
    client = _init_llm(args.model)
    memopedia = _init_memopedia(conn)

    # Fetch all target messages
    messages = _fetch_messages(conn, limit=args.limit, start_after=args.start_after)
    if not messages:
        print("No messages found")
        conn.close()
        return

    print(f"Found {len(messages)} messages to process")
    print(f"  Oldest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[0].created_at))}")
    print(f"  Newest: {time.strftime('%Y-%m-%d %H:%M', time.localtime(messages[-1].created_at))}")
    print(f"  Batch size: {args.batch_size}, Organize threshold: {args.organize_threshold}")
    print()

    from sai_memory.memory.note_extractor import extract_memory_notes
    from sai_memory.memory.storage import add_memory_notes, get_unresolved_notes, count_unresolved_notes
    from sai_memory.arasuji.context import get_episode_context_for_timerange

    total_notes_extracted = 0
    total_notes_organized = 0
    batch_count = 0
    organize_count = 0
    thread_id = f"{args.persona_id}:__persona__"

    for i in range(0, len(messages), args.batch_size):
        batch = messages[i:i + args.batch_size]
        if len(batch) < args.batch_size // 2:
            print(f"  Skipping small final batch ({len(batch)} messages)")
            continue

        batch_count += 1
        start_time = min(m.created_at for m in batch)
        end_time = max(m.created_at for m in batch)
        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(start_time))

        # Episode context for this time range
        ep_ctx = ""
        try:
            ep_ctx = get_episode_context_for_timerange(
                conn, start_time=start_time, end_time=end_time, max_entries=10,
            )
        except Exception:
            pass

        # Memopedia context (refreshed after each organize)
        memopedia_context = _get_memopedia_context(memopedia)

        # Existing notes for dedup
        existing = get_unresolved_notes(conn, limit=200)
        existing_contents = [n.content for n in existing]

        print(f"[Batch {batch_count}] {time_str} | msgs {i+1}-{i+len(batch)}/{len(messages)}", end="")

        notes = extract_memory_notes(
            client, batch,
            episode_context=ep_ctx,
            memopedia_context=memopedia_context,
            existing_notes=existing_contents,
            persona_id=args.persona_id,
        )

        if notes:
            print(f" → {len(notes)} notes")
            for note in notes:
                print(f"    + {note}")

            if not args.dry_run:
                add_memory_notes(
                    conn, thread_id=thread_id, notes=notes,
                    source_time=int(end_time),
                )

            total_notes_extracted += len(notes)
        else:
            print(" → 0 notes")

        # Check if we should organize
        if args.dry_run:
            unresolved = total_notes_extracted - total_notes_organized
            if unresolved >= args.organize_threshold:
                print(f"\n  --- [dry-run] Organize would run here ({unresolved} notes) ---\n")
                total_notes_organized += unresolved
                organize_count += 1
        else:
            unresolved = count_unresolved_notes(conn)
            if unresolved >= args.organize_threshold:
                print(f"\n  --- Organizing ({unresolved} unresolved notes) ---")
                organized = _run_organize(client, conn, memopedia, args.persona_id, dry_run=False)
                total_notes_organized += organized
                organize_count += 1
                print()

    # Final organize for remaining notes
    if args.dry_run:
        final_unresolved = total_notes_extracted - total_notes_organized
        if final_unresolved > 0:
            print(f"\n  --- [dry-run] Final organize would run here ({final_unresolved} notes) ---")
            total_notes_organized += final_unresolved
            organize_count += 1
    else:
        final_unresolved = count_unresolved_notes(conn)
        if final_unresolved > 0:
            print(f"\n  --- Final organize ({final_unresolved} remaining notes) ---")
            organized = _run_organize(client, conn, memopedia, args.persona_id, dry_run=False)
            total_notes_organized += organized
            organize_count += 1

    # Summary
    print(f"\n{'='*60}")
    print("Done!")
    print(f"  Messages processed: {batch_count * args.batch_size} (in {batch_count} batches)")
    print(f"  Notes extracted: {total_notes_extracted}")
    print(f"  Notes organized: {total_notes_organized}")
    print(f"  Organize rounds: {organize_count}")
    if messages:
        print(f"  Last message timestamp: {messages[-1].created_at}")
        print(f"  (Use --start-after {messages[-1].created_at} to resume)")
    if args.dry_run:
        print("  (dry-run mode, nothing saved)")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
