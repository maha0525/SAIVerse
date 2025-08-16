#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

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
    ap = argparse.ArgumentParser(description="Recall from a per-persona memory DB created by ingest_persona_log.")
    ap.add_argument("persona_id", help="Persona ID used for ingestion (collection suffix)")
    ap.add_argument("query", help="Query text to recall against the memory")
    ap.add_argument("--topk", type=int, default=5, help="Number of items to recall (default: 5)")
    ap.add_argument("--location-base", default=None, help="Base dir for per-persona DB (default: QDRANT_LOCATION or ~/.saiverse/qdrant)")
    ap.add_argument("--collection-prefix", default=None, help="Collection prefix used during ingestion")
    ap.add_argument("--json", action="store_true", help="Output JSON for programmatic inspection")
    args = ap.parse_args()

    cfg = Config.from_env()
    # Force Qdrant backend for recall against the ingested DB
    cfg.storage_backend = "qdrant"  # type: ignore

    base = args.location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    per_loc = str(Path(base) / "persona" / args.persona_id)
    cfg.qdrant_location = per_loc

    pref = args.collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{args.persona_id}"

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True)
    print(f"Per-persona DB: location={cfg.qdrant_location} prefix={cfg.qdrant_collection_prefix}")
    print(f"Backend: {type(mc.storage).__name__}, Embedder: {type(mc.embedder).__name__}")

    bundle = mc.recall(args.query, k=args.topk)
    texts: List[str] = bundle.get("texts", [])
    topics = bundle.get("topics", [])

    if args.json:
        out = {
            "query": args.query,
            "top_texts": texts,
            "topics": [
                {"id": getattr(t, "id", None), "title": getattr(t, "title", None), "summary": getattr(t, "summary", None)}
                for t in topics
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print("\nQuery:", args.query)
    print("Top texts:")
    for t in texts:
        print(" -", t)
    print("Topics:")
    for t in topics:
        title = getattr(t, "title", "")
        print(" -", title)


if __name__ == "__main__":
    main()

