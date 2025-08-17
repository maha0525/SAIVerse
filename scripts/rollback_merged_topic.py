#!/usr/bin/env python3
from __future__ import annotations

"""
指定トピック（マージで作成された統合トピック想定）から、各エントリを
entry.previous_topics に記録されている元トピックへ戻すロールバックツール。

使い方:
  python scripts/rollback_merged_topic.py --persona-id <ID> --topic-id <TOPIC_ID>

注意:
  - ペルソナ単位のQdrant DBを直接更新します。
  - previous_topics が空のエントリはスキップします。
  - 元トピックは disabled=True の可能性があります。復元時に disabled=False に戻します。
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from memory_core.config import Config
from memory_core.pipeline import MemoryCore


def force_qdrant(cfg: Config, persona_id: str, location_base: str | None, collection_prefix: str | None) -> None:
    cfg.storage_backend = "qdrant"  # type: ignore
    base = location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    cfg.qdrant_location = str(Path(base) / "persona" / persona_id)
    pref = collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{persona_id}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Rollback entries from a merged topic to their previous topics")
    ap.add_argument("--persona-id", required=True)
    ap.add_argument("--topic-id", required=True)
    ap.add_argument("--location-base", default=None)
    ap.add_argument("--collection-prefix", default=None)
    args = ap.parse_args()

    cfg = Config.from_env()
    force_qdrant(cfg, args.persona_id, args.location_base, args.collection_prefix)
    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=False)

    merged = mc.storage.get_topic(args.topic_id)
    if not merged:
        raise SystemExit(f"Topic not found: {args.topic_id}")

    restored = 0
    per_topic_restored: dict[str, list[str]] = {}
    for eid in list(merged.entry_ids):
        e = mc.storage.get_entry(eid)
        if not e:
            continue
        prevs = list(getattr(e, "previous_topics", []) or [])
        if not prevs:
            continue
        # Remove merged topic link
        if merged.id in e.linked_topics:
            e.linked_topics = [tid for tid in e.linked_topics if tid != merged.id]
        # Re-link to previous topics
        for ptid in prevs:
            if ptid not in e.linked_topics:
                e.linked_topics.append(ptid)
            pt = mc.storage.get_topic(ptid)
            if pt:
                if e.id not in pt.entry_ids:
                    pt.entry_ids.append(e.id)
                if getattr(pt, "disabled", False):
                    pt.disabled = False
                mc.storage.update_topic(pt)
                per_topic_restored.setdefault(ptid, []).append(e.id)
        mc.storage.upsert_entry(e)
        restored += 1

    # Ensure all child/source topics are re-enabled, even if no entries restored (safety)
    for ptid in list(getattr(merged, "children", []) or []):
        pt = mc.storage.get_topic(ptid)
        if not pt:
            continue
        # If we collected restored entries per topic, reconcile entry_ids deterministically
        if ptid in per_topic_restored:
            ids = list(dict.fromkeys(per_topic_restored[ptid]))
            pt.entry_ids = ids
        # Re-enable
        if getattr(pt, "disabled", False):
            pt.disabled = False
        mc.storage.update_topic(pt)

    # Empty and disable the merged topic
    merged.entry_ids = []
    merged.disabled = True
    mc.storage.update_topic(merged)

    print(f"Restored {restored} entries back to their previous topics. Disabled {merged.id}.")


if __name__ == "__main__":
    main()
