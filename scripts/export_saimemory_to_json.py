#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from dotenv import load_dotenv

load_dotenv()

DEFAULT_OUTPUT = "-"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export SAIMemory persona messages to JSON.")
    parser.add_argument("persona", help="Persona ID (e.g. air_city_a)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output file path (default: stdout)")
    parser.add_argument("--start", help="Start ISO timestamp (inclusive)")
    parser.add_argument("--end", help="End ISO timestamp (inclusive)")
    parser.add_argument(
        "--thread",
        action="append",
        dest="threads",
        help="Thread suffix or full thread ID to export. Repeatable. If omitted, export all threads for the persona.",
    )
    return parser.parse_args()


def iso_to_epoch(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    return int(datetime.fromisoformat(ts).timestamp())


def _resolve_thread_ids(conn, persona: str, threads: Optional[Iterable[str]]) -> list[str]:
    if threads:
        resolved: list[str] = []
        for item in threads:
            if not item:
                continue
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                resolved.append(item)
            else:
                resolved.append(f"{persona}:{item}")
        return resolved

    cur = conn.execute(
        "SELECT DISTINCT thread_id FROM messages WHERE thread_id LIKE ? ORDER BY thread_id",
        (f"{persona}:%",),
    )
    return [row[0] for row in cur.fetchall()]


def export_messages(persona: str, threads: Optional[Iterable[str]], start_ts: Optional[str], end_ts: Optional[str]) -> list[dict]:
    db_path = Path.home() / ".saiverse" / "personas" / persona / "memory.db"
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona}: {db_path}")

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        start = iso_to_epoch(start_ts)
        end = iso_to_epoch(end_ts)
        has_message_embeddings = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_embeddings'"
        ).fetchone()
        thread_ids = _resolve_thread_ids(conn, persona, threads)

        out: list[dict] = []
        for thread_id in thread_ids:
            if has_message_embeddings:
                query = [
                    "SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at,",
                    "       COALESCE(e.embedding_count, 0) AS embedding_count",
                    "  FROM messages m",
                    "  LEFT JOIN (",
                    "      SELECT message_id, COUNT(*) AS embedding_count",
                    "      FROM message_embeddings",
                    "      GROUP BY message_id",
                    "  ) e ON m.id = e.message_id",
                    " WHERE m.thread_id=?",
                ]
            else:
                query = [
                    "SELECT m.id, m.thread_id, m.role, m.content, m.resource_id, m.created_at, 0 AS embedding_count",
                    "  FROM messages m",
                    " WHERE m.thread_id=?",
                ]
            params: list = [thread_id]
            if start is not None:
                query.append("AND created_at >= ?")
                params.append(start)
            if end is not None:
                query.append("AND created_at <= ?")
                params.append(end)
            query.append("ORDER BY created_at ASC")
            sql = " ".join(query)

            rows = conn.execute(sql, params).fetchall()

            for mid, tid, role, content, resource_id, created_at, embed_count in rows:
                out.append(
                    {
                        "message_id": mid,
                        "thread_id": tid,
                        "role": role,
                        "content": content,
                        "resource_id": resource_id,
                        "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
                        "embedding_chunks": embed_count,
                    }
                )
        return out
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    messages = export_messages(args.persona, args.threads, args.start, args.end)

    if args.output == DEFAULT_OUTPUT:
        json.dump(messages, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
