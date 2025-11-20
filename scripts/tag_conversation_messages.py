#!/usr/bin/env python3
"""Add missing 'conversation' tags to SAIMemory messages."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def ensure_conversation_tags(db_path: Path, apply: bool) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, metadata FROM messages")
    updates = []
    for msg_id, raw_meta in cur.fetchall():
        if not raw_meta:
            metadata = {}
        else:
            try:
                metadata = json.loads(raw_meta)
            except json.JSONDecodeError:
                metadata = {}
        tags = metadata.get("tags")
        if isinstance(tags, list):
            tag_list = [str(tag) for tag in tags if tag]
        elif tags is None:
            tag_list = []
        else:
            tag_list = [str(tags)]
        if "conversation" in (tag.lower() if isinstance(tag, str) else tag for tag in tag_list):
            continue
        tag_list.append("conversation")
        metadata["tags"] = tag_list
        updates.append((json.dumps(metadata, ensure_ascii=False), msg_id))
    if apply and updates:
        cur.executemany("UPDATE messages SET metadata=? WHERE id=?", updates)
        conn.commit()
    conn.close()
    return len(updates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Add conversation tags to SAIMemory entries.")
    parser.add_argument("persona", help="Persona ID (maps to ~/.saiverse/personas/<persona>/memory.db)")
    parser.add_argument("--apply", action="store_true", help="Write changes to the database (default: dry-run)")
    parser.add_argument("--db", help="Override path to memory.db")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Path.home() / ".saiverse" / "personas" / args.persona / "memory.db"
    if not db_path.exists():
        parser.error(f"memory.db not found at {db_path}")

    count = ensure_conversation_tags(db_path, args.apply)
    if args.apply:
        print(f"Updated {count} messages in {db_path}")
    else:
        print(f"{count} messages would be updated. Run with --apply to write changes.")


if __name__ == "__main__":
    main()
