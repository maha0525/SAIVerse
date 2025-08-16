#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

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


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="List memory topics and counts.")
    ap.add_argument("--prefix", help="Qdrant collection prefix override", default=None)
    ap.add_argument("--location", help="Qdrant location override (:memory: or path)", default=None)
    ap.add_argument("--show-entries", action="store_true", help="Show entry texts under each topic (max 5)")
    args = ap.parse_args()

    cfg = Config.from_env()
    if args.prefix:
        cfg.qdrant_collection_prefix = args.prefix
    if args.location:
        cfg.qdrant_location = args.location

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True)
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    topics.sort(key=lambda t: (-(t.strength or 0), t.title or ""))
    print(f"Topics: {len(topics)} (prefix={cfg.qdrant_collection_prefix})")
    for i, t in enumerate(topics, 1):
        cnt = len(t.entry_ids) if t.entry_ids else 0
        print(f"[{i:02d}] {t.title}  (entries={cnt})")
        if args.show-entries and t.entry_ids:
            # Fetch a few entries by id
            shown = 0
            for eid in t.entry_ids:
                e = mc.storage.get_entry(eid)  # type: ignore[attr-defined]
                if e is None:
                    continue
                print("     -", e.raw_text)
                shown += 1
                if shown >= 5:
                    break


if __name__ == "__main__":
    main()

