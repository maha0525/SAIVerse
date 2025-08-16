#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Dict, Tuple

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from memory_core import MemoryCore
from memory_core.config import Config
from memory_core.topic_assigner import _is_generic_title, _derive_fallback_title


def derive_title_and_summary(mc: MemoryCore, entry_ids: List[str]) -> Tuple[str, str | None]:
    # Build a recent_dialog view from the topic's entry ids (order by timestamp if possible)
    entries = []
    for eid in entry_ids:
        e = mc.storage.get_entry(eid)
        if e is not None:
            entries.append(e)
    # sort by timestamp/turn if available
    entries.sort(key=lambda x: (getattr(x, "timestamp", None) or 0, getattr(x, "turn_index", 0)))

    recent_dialog: List[Dict[str, str]] = [
        {"speaker": (e.speaker or ""), "text": (e.raw_text or "")}
        for e in entries[-12:]
    ]
    title = _derive_fallback_title(recent_dialog)
    # summary: prefer latest user text
    last_user = ""
    for e in reversed(entries):
        if (e.speaker or "").lower() in ("user", "human"):
            last_user = (e.raw_text or "").strip()
            if last_user:
                break
    summary = last_user[:160] if last_user else None
    return title, summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Rename generic/empty topic titles in a per-persona memory DB (Qdrant).")
    ap.add_argument("persona_id", help="Persona ID suffix used during ingestion")
    ap.add_argument("--location-base", default=None, help="Base dir for per-persona DB (default: QDRANT_LOCATION or ~/.saiverse/qdrant)")
    ap.add_argument("--collection-prefix", default=None, help="Collection prefix (will be suffixed with persona id)")
    ap.add_argument("--dry-run", action="store_true", help="Preview changes without writing to DB")
    ap.add_argument("--limit", type=int, default=None, help="Limit the number of topics to rename")
    args = ap.parse_args()

    cfg = Config.from_env()
    cfg.storage_backend = "qdrant"  # force Qdrant

    base = args.location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    per_loc = str(Path(base) / "persona" / args.persona_id)
    cfg.qdrant_location = per_loc
    pref = args.collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{args.persona_id}"

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True)
    print(f"DB location: {cfg.qdrant_location}")
    print(f"Collection prefix: {cfg.qdrant_collection_prefix}")

    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    if not topics:
        print("No topics found.")
        return

    # Filter topics with generic/empty titles
    targets = [t for t in topics if _is_generic_title((t.title or None))]
    print(f"Topics total: {len(topics)}  |  generic/empty: {len(targets)}")

    changed = 0
    for i, t in enumerate(targets, 1):
        if args.limit is not None and changed >= args.limit:
            break
        new_title, new_summary = derive_title_and_summary(mc, t.entry_ids or [])
        print(f"[{i}] {t.id}\n  old: '{t.title}'\n  new: '{new_title}'")
        if (t.summary or None) is None and new_summary:
            print(f"  summary set: '{new_summary[:60]}'{'…' if new_summary and len(new_summary)>60 else ''}")
        if not args.dry_run:
            t.title = new_title
            if new_summary:
                t.summary = new_summary
            mc.storage.update_topic(t)  # type: ignore[attr-defined]
        changed += 1

    print(f"Done. Renamed {changed} topic(s){' (dry-run)' if args.dry_run else ''}.")


if __name__ == "__main__":
    main()

