"""Recover spell messages lost due to _store_memory bug (2026-05-26 ~ 2026-05-31).

Bug: sea/runtime.py の _store_memory で spell_seq ブロック内に
len(thought_signature) が誤配置され、spell 経由の全メッセージが
TypeError → except Exception で握り潰されていた。

Recovery source: pulse_logs テーブル（memory.db 内）。
spell 発動 (assistant) と spell 結果 (system) が node_id='spell_round_*' で残っている。

Usage:
    python scripts/recover_spell_messages.py --dry-run   # プレビュー
    python scripts/recover_spell_messages.py              # 実行
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Optional


SAIVERSE_HOME = Path.home() / ".saiverse"
AFFECTED_PERSONAS = ["air_city_a", "eris_city_a"]
BUG_START_EPOCH = 1748267620  # 2026-05-26 22:53:40 JST (commit 99566e5)
PERSONA_THREAD_SUFFIX = "__persona__"


def get_memory_db(persona_id: str) -> Path:
    return SAIVERSE_HOME / "personas" / persona_id / "memory.db"


def recover_persona(persona_id: str, dry_run: bool) -> int:
    db_path = get_memory_db(persona_id)
    if not db_path.exists():
        print(f"  [SKIP] {db_path} not found")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # pulse_logs から spell 関連エントリを取得
    cur.execute("""
        SELECT pulse_id, role, content, node_id, playbook_name, created_at
        FROM pulse_logs
        WHERE node_id LIKE 'spell_round_%'
          AND created_at >= ?
        ORDER BY created_at ASC
    """, (BUG_START_EPOCH,))
    candidates = cur.fetchall()

    if not candidates:
        print(f"  [INFO] No spell entries in pulse_logs after bug start")
        conn.close()
        return 0

    # messages テーブルの default thread を特定
    default_thread = f"{persona_id}:{PERSONA_THREAD_SUFFIX}"

    # 既に復元済みか確認するため、既存 messages の (pulse_id, role, created_at) を取得
    cur.execute("""
        SELECT pulse_id, role, created_at FROM messages
        WHERE created_at >= ?
    """, (BUG_START_EPOCH,))
    existing = {(r["pulse_id"], r["role"], r["created_at"]) for r in cur.fetchall()}

    # 隣接メッセージから origin_track_id / line_role を推定するヘルパー
    def find_adjacent_metadata(pulse_id: str) -> dict:
        cur.execute("""
            SELECT origin_track_id, line_role, line_id, scope
            FROM messages
            WHERE pulse_id = ? AND origin_track_id IS NOT NULL
            LIMIT 1
        """, (pulse_id,))
        row = cur.fetchone()
        if row:
            return {
                "origin_track_id": row["origin_track_id"],
                "line_role": row["line_role"],
                "line_id": row["line_id"],
                "scope": row["scope"],
            }
        return {"origin_track_id": None, "line_role": "main_line", "line_id": "main", "scope": "committed"}

    recovered = 0
    # pulse_id ごとにグループ化して spell_origin_id / spell_seq を設定
    from collections import defaultdict
    by_pulse: dict[str, list] = defaultdict(list)
    for row in candidates:
        by_pulse[row["pulse_id"]].append(row)

    for pulse_id, entries in by_pulse.items():
        meta = find_adjacent_metadata(pulse_id)
        spell_origin_id: Optional[str] = None

        for i, entry in enumerate(entries):
            key = (entry["pulse_id"], entry["role"], entry["created_at"])
            if key in existing:
                print(f"  [SKIP] Already exists: {entry['role']} at {entry['created_at']} pulse={pulse_id}")
                continue

            msg_id = str(uuid.uuid4())
            role = entry["role"]
            content = entry["content"]

            # spell_seq: round 番号を node_id から抽出
            node_id = entry["node_id"] or ""
            try:
                spell_seq = int(node_id.replace("spell_round_", ""))
            except ValueError:
                spell_seq = i + 1

            # assistant が spell_origin, system が結果
            if role == "assistant":
                spell_origin_id = msg_id

            # tags
            tags = ["conversation"]
            if role == "system":
                tags.append("spell")

            metadata_json = json.dumps({"tags": tags})

            print(f"  [{'DRY' if dry_run else 'INSERT'}] {role} | seq={spell_seq} | "
                  f"ts={entry['created_at']} | pulse={pulse_id[:12]}... | "
                  f"content={content[:60]}...")

            if not dry_run:
                cur.execute("""
                    INSERT INTO messages (
                        id, thread_id, role, content, created_at, metadata,
                        origin_track_id, line_role, line_id, scope,
                        pulse_id, spell_origin_id, spell_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg_id,
                    default_thread,
                    role,
                    content,
                    entry["created_at"],
                    metadata_json,
                    meta["origin_track_id"],
                    meta["line_role"],
                    meta["line_id"],
                    meta["scope"],
                    pulse_id,
                    spell_origin_id if role == "system" else None,
                    spell_seq,
                ))
            recovered += 1

    if not dry_run and recovered > 0:
        conn.commit()
        print(f"  [DONE] {recovered} messages recovered")
    elif dry_run and recovered > 0:
        print(f"  [DRY-RUN] {recovered} messages would be recovered")

    conn.close()
    return recovered


def main():
    parser = argparse.ArgumentParser(description="Recover lost spell messages from pulse_logs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    args = parser.parse_args()

    print(f"Recovery mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")
    print(f"Bug window: epoch >= {BUG_START_EPOCH}")
    print()

    total = 0
    for persona_id in AFFECTED_PERSONAS:
        print(f"[{persona_id}]")
        n = recover_persona(persona_id, args.dry_run)
        total += n
        print()

    print(f"Total: {total} messages {'would be' if args.dry_run else ''} recovered")


if __name__ == "__main__":
    main()
