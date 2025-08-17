#!/usr/bin/env python3
from __future__ import annotations

"""
指定した persona_id の永続DB（ペルソナ単位のQdrantローカルDB）に対して、
トピックマージプロトコルを直接実行するテストスクリプト。

ログの読み込みは行わず、既存のDB内容に対してのみ操作します。

使い方:
  python scripts/merge_persona_db.py --persona-id <ID> [--min-topics 30] [--block-source-threshold 10] [--force]

必要条件:
  - Qdrantのローカル（embedded）DBをペルソナごとに用意している構成。
  - 位置・接頭辞は ingest_persona_log.py と同様のルールで解決します。
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure project root is on sys.path (same approach as ingest_persona_log.py)
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

from memory_core.config import Config
from memory_core.pipeline import MemoryCore
from memory_core.organizer import run_topic_merge
from memory_core.llm import DummyLLM
try:
    # Optional backends
    from memory_core.llm import OllamaHTTPAssignLLM, OllamaLLM, GeminiAssignLLM
except Exception:
    OllamaHTTPAssignLLM = None  # type: ignore
    OllamaLLM = None  # type: ignore
    GeminiAssignLLM = None  # type: ignore


def list_topics(core: MemoryCore, limit: int = 25) -> list[dict]:
    topics = core.storage.list_topics()
    topics.sort(key=lambda t: len(t.entry_ids), reverse=True)
    out = []
    for t in topics[:limit]:
        out.append({
            "id": t.id,
            "title": t.title,
            "entries": len(t.entry_ids or []),
            "disabled": bool(getattr(t, "disabled", False)),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Run topic merge on a persona's persisted DB")
    ap.add_argument("--persona-id", required=True)
    ap.add_argument("--location-base", default=None, help="DB base dir (default: QDRANT_LOCATION or ~/.saiverse/qdrant)")
    ap.add_argument("--collection-prefix", default=None, help="Prefix base (default: QDRANT_COLLECTION_PREFIX or 'saiverse')")
    ap.add_argument("--min-topics", type=int, default=30)
    ap.add_argument("--block-source-threshold", type=int, default=10)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--assign-llm",
        default=None,
        choices=["gemini", "ollama_http", "ollama_cli", "dummy", "none"],
        help="Use LLM to propose merge (optional).",
    )
    args = ap.parse_args()

    # ベース設定をENVから読み込み
    cfg = Config.from_env()
    # ストレージはQdrantを強制（ペルソナDBへ直アクセス）
    cfg.storage_backend = "qdrant"  # type: ignore

    # ペルソナ専用のDBロケーションに切替（ingest_persona_log.py と同様の規約）
    base = args.location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    cfg.qdrant_location = str(Path(base) / "persona" / args.persona_id)

    # コレクション接頭辞もペルソナごとに分離
    pref = args.collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{args.persona_id}"

    # MemoryCore を立ち上げ（埋め込み/検索のみ使う）
    core = MemoryCore.create_default(config=cfg, with_dummy_llm=False)

    # Optional: build LLM backend for merge proposal
    llm = None
    if args.assign_llm and args.assign_llm != "none":
        if args.assign_llm == "gemini" and GeminiAssignLLM is not None:
            # Model from env if set
            import os as _os
            model = _os.getenv("SAIVERSE_ASSIGN_GEMINI_MODEL") or _os.getenv("SAIVERSE_ASSIGN_LLM_MODEL") or "gemini-2.0-flash"
            llm = GeminiAssignLLM(model=model)  # type: ignore
        elif args.assign_llm == "ollama_http" and OllamaHTTPAssignLLM is not None:
            llm = OllamaHTTPAssignLLM()  # type: ignore
        elif args.assign_llm == "ollama_cli" and OllamaLLM is not None:
            llm = OllamaLLM()  # type: ignore
        elif args.assign_llm == "dummy":
            llm = DummyLLM()

    before = list_topics(core)
    print(json.dumps({"before": before, "count": len(core.storage.list_topics())}, ensure_ascii=False))

    res = run_topic_merge(
        storage=core.storage,
        embedder=core.embedder,
        min_topics=args.min_topics,
        block_source_threshold=args.block_source_threshold,
        force=args.force,
        llm=llm,
    )
    print(json.dumps({"merge_result": res}, ensure_ascii=False))

    after = list_topics(core)
    print(json.dumps({"after": after, "count": len(core.storage.list_topics())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
