#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

DEFAULT_OUTPUT = "-"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export SAIMemory persona messages to JSON.")
    parser.add_argument("persona", help="Persona ID (e.g. air_city_a)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output file path (default: stdout)")
    parser.add_argument("--start", help="Start ISO timestamp (inclusive)")
    parser.add_argument("--end", help="End ISO timestamp (inclusive)")
    parser.add_argument("--thread", default="__persona__", help="Thread suffix (default: __persona__)" )
    return parser.parse_args()


def iso_to_epoch(ts: Optional[str]) -> Optional[int]:
    if not ts:
        return None
    return int(datetime.fromisoformat(ts).timestamp())


def export_messages(persona: str, thread_suffix: str, start_ts: Optional[str], end_ts: Optional[str]) -> list[dict]:
    db_path = Path.home() / ".saiverse" / "personas" / persona / "memory.db"
    if not db_path.exists():
        raise FileNotFoundError(f"memory.db not found for persona {persona}: {db_path}")

    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        start = iso_to_epoch(start_ts)
        end = iso_to_epoch(end_ts)
        thread_id = f"{persona}:{thread_suffix}"
        query = [
            "SELECT thread_id, role, content, resource_id, created_at FROM messages WHERE thread_id=?",
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

        out: list[dict] = []
        for thread_id, role, content, resource_id, created_at in rows:
            out.append(
                {
                    "thread_id": thread_id,
                    "role": role,
                    "content": content,
                    "resource_id": resource_id,
                    "created_at": datetime.fromtimestamp(created_at, timezone.utc).isoformat(),
                }
            )
        return out
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    messages = export_messages(args.persona, args.thread, args.start, args.end)

    if args.output == DEFAULT_OUTPUT:
        json.dump(messages, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
