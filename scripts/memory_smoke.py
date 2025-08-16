#!/usr/bin/env python3
"""
Quick smoke test for MemoryCore with Qdrant + E5 embeddings.

Usage:
  python scripts/memory_smoke.py

Relies on .env for configuration. You can override the collection prefix by
setting QDRANT_COLLECTION_PREFIX or passing SMOKE_PREFIX in the environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from pprint import pprint
from datetime import datetime

# Ensure project root is importable when running as `python scripts/memory_smoke.py`
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

from memory_core import MemoryCore
from memory_core.config import Config


def main() -> None:
    # Ensure we don't collide with existing collections unless explicitly desired
    smoke_prefix = os.getenv("SMOKE_PREFIX") or "saiverse768_smoke"
    if not os.getenv("QDRANT_COLLECTION_PREFIX"):
        os.environ["QDRANT_COLLECTION_PREFIX"] = smoke_prefix

    cfg = Config.from_env()
    print("Config:", {
        "storage_backend": cfg.storage_backend,
        "embed_provider": cfg.embedding_provider,
        "embed_model": cfg.embedding_model,
        "embed_device": cfg.embedding_device,
        "embed_dim": cfg.embedding_dim,
        "qdrant_url": cfg.qdrant_url,
        "qdrant_location": cfg.qdrant_location,
        "qdrant_prefix": cfg.qdrant_collection_prefix,
    })

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=False)
    print("Backend:", type(mc.storage).__name__)
    print("Embedder:", type(mc.embedder).__name__)

    conv_id = f"smoke_conv_{datetime.now().strftime('%H%M%S')}"

    turns = [
        ("user", "那須塩原の吊り橋の写真、送ったよ。めっちゃ揺れたね…"),
        ("ai",   "ほんとに高かったよね。スリル満点だった。"),
        ("user", "来月また旅行行こう。今度は温泉に入りたい。"),
        ("ai",   "温泉いいね。那須塩原なら秘湯もあるよ。"),
    ]

    print("\nIngesting turns…")
    for spk, text in turns:
        e = mc.remember(text, conv_id=conv_id, speaker=spk)
        print(f"  + {spk}: {text[:40]}… -> {e.id}")

    print("\nRecalling…")
    query = "あの旅行、また行きたいな。"
    bundle = mc.recall(query, k=5)
    texts = bundle.get("texts", [])
    topics = bundle.get("topics", [])
    print("Query:", query)
    print("Top texts:")
    for t in texts:
        print(" -", t)
    print("Topics:")
    for t in topics:
        print(" -", getattr(t, "title", ""))

    print("\nDone.")


if __name__ == "__main__":
    main()
