#!/usr/bin/env python3
"""
Prune SAIMemory entries using timestamp or count based filters.

Usage example:
    python scripts/prune_sai_memory.py --persona eris_city_a --since "2025-10-08T00:00:00"
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence


def parse_timestamp(value: str) -> int:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("timestamp value must not be empty")
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"could not parse '{value}' as ISO-8601 or epoch seconds"
        ) from exc
    return int(dt.timestamp())


def normalise_thread_id(persona_id: str, name: str) -> str:
    if ":" in name:
        return name
    return f"{persona_id}:{name}"


def fetch_thread_ids(
    conn: sqlite3.Connection,
    persona_id: str,
    suffixes: Sequence[str] | None,
    explicit_threads: Sequence[str] | None,
) -> List[str]:
    ids: List[str] = []
    if explicit_threads:
        ids.extend(normalise_thread_id(persona_id, t) for t in explicit_threads)
    if suffixes:
        ids.extend(normalise_thread_id(persona_id, s) for s in suffixes)
    if ids:
        return ids
    cur = conn.execute("SELECT id FROM threads WHERE id LIKE ?", (f"{persona_id}:%",))
    return [row[0] for row in cur.fetchall()]


def chunked(seq: Sequence[str], size: int = 500) -> Iterable[List[str]]:
    for idx in range(0, len(seq), size):
        yield seq[idx : idx + size]


def delete_messages(conn: sqlite3.Connection, message_ids: Sequence[str]) -> int:
    total = 0
    for chunk in chunked(message_ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM message_embeddings WHERE message_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            f"DELETE FROM embeddings WHERE message_id IN ({placeholders})",
            chunk,
        )
        conn.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",
            chunk,
        )
        total += len(chunk)
    conn.commit()
    return total


def gather_message_ids(
    conn: sqlite3.Connection,
    thread_ids: Sequence[str],
    cutoff: int | None,
    limit_per_thread: int | None,
) -> List[str]:
    ids: List[str] = []
    for thread_id in thread_ids:
        params: List[int | str] = [thread_id]
        query = "SELECT id FROM messages WHERE thread_id=?"
        if cutoff is not None:
            query += " AND created_at>=?"
            params.append(cutoff)
        query += " ORDER BY created_at DESC"
        if limit_per_thread is not None:
            query += " LIMIT ?"
            params.append(limit_per_thread)
        cur = conn.execute(query, tuple(params))
        ids.extend(row[0] for row in cur.fetchall())
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove SAIMemory messages matching the given filters."
    )
    parser.add_argument("--persona", required=True, help="Persona ID (e.g. eris_city_a)")
    parser.add_argument(
        "--since",
        help="Timestamp boundary (ISO-8601 like 2025-10-08T00:00:00 or epoch seconds). "
        "Messages created before this value are kept.",
    )
    parser.add_argument(
        "--db",
        help="Override path to memory.db. Defaults to ~/.saiverse/personas/<persona>/memory.db",
    )
    parser.add_argument(
        "--suffix",
        action="append",
        help="Optional thread suffix to limit pruning (can be supplied multiple times). "
        "If omitted, all threads for the persona are pruned. Example suffix: __persona__ or user_room_city_a.",
    )
    parser.add_argument(
        "--thread",
        action="append",
        help="Explicit thread identifier to prune. Provide either the full thread ID (persona:suffix) "
        "or just the suffix. Can be repeated.",
    )
    parser.add_argument(
        "--count",
        type=int,
        help="Maximum number of newest messages to delete per thread (after applying '--since' if provided).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show how many entries would be deleted without modifying the database.",
    )
    args = parser.parse_args()

    if args.since is None and args.count is None:
        raise SystemExit("Specify --since, --count, or both to choose what to delete.")

    cutoff = parse_timestamp(args.since) if args.since is not None else None
    persona_id = args.persona
    db_path = Path(args.db) if args.db else Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"

    if not db_path.exists():
        raise SystemExit(f"memory database not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        thread_ids = fetch_thread_ids(conn, persona_id, args.suffix, args.thread)
        if not thread_ids:
            raise SystemExit("No matching threads found; nothing to prune.")

        limit = None
        if args.count is not None:
            if args.count <= 0:
                raise SystemExit("--count must be a positive integer")
            limit = args.count

        message_ids = gather_message_ids(conn, thread_ids, cutoff, limit)
        if args.dry_run:
            scope_desc = []
            if cutoff is not None:
                scope_desc.append(f"created_at≥{cutoff}")
            if limit is not None:
                scope_desc.append(f"top {limit} per thread")
            scope = ", ".join(scope_desc) if scope_desc else "no filters"
            print(f"[dry-run] Found {len(message_ids)} message(s) ({scope}) for persona '{persona_id}'.")
            return

        deleted = delete_messages(conn, message_ids)
        print(f"Deleted {deleted} message(s) from {len(thread_ids)} thread(s) for persona '{persona_id}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
