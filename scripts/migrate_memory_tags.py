#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def migrate_memory_tags(persona_dir: Path) -> None:
    db_path = persona_dir / "memory.db"
    if not db_path.exists():
        print(f"[skip] {persona_dir}: memory.db not found")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT id, metadata FROM messages WHERE metadata IS NULL OR JSON_EXTRACT(metadata, '$.tags') IS NULL"
        ).fetchall()
        for row in rows:
            meta = row["metadata"]
            if meta:
                data = json.loads(meta)
            else:
                data = {}
            tags = data.get("tags") or []
            if "conversation" not in tags:
                tags.append("conversation")
            data["tags"] = tags
            conn.execute(
                "UPDATE messages SET metadata=? WHERE id=?",
                (json.dumps(data, ensure_ascii=False), row["id"])
            )
        conn.commit()
        print(f"[done] {persona_dir}: tagged {len(rows)} rows")
    finally:
        conn.close()


def main() -> None:
    base = Path.home() / ".saiverse" / "personas"
    if not base.exists():
        print(f"No personas found at {base}")
        return
    for persona_dir in base.iterdir():
        if persona_dir.is_dir():
            migrate_memory_tags(persona_dir)


if __name__ == "__main__":
    main()
