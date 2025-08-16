#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Dict

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


def read_persona_log(persona_id: str) -> List[Dict]:
    home = Path.home() / ".saiverse" / "personas" / persona_id / "log.json"
    if not home.exists():
        raise FileNotFoundError(f"Persona log not found: {home}")
    try:
        data = json.loads(home.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        # some formats may wrap under {log: [...]}
        if isinstance(data, dict) and isinstance(data.get("log"), list):
            return data["log"]
    except Exception as e:
        raise RuntimeError(f"Failed to parse persona log: {e}")
    raise RuntimeError("Unsupported log format")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Ingest persona's log.json into per-persona memory DB.")
    ap.add_argument("persona_id", help="Persona ID whose log.json to ingest")
    ap.add_argument("--location-base", default=None, help="Base dir for per-persona DB (default: QDRANT_LOCATION or ~/.saiverse/qdrant)")
    ap.add_argument("--collection-prefix", default=None, help="Collection prefix (will be suffixed with persona id)")
    ap.add_argument("--conv-id", default=None, help="Conversation id (default: persona:<id>)")
    ap.add_argument(
        "--assign-llm",
        default=None,
        choices=["dummy", "ollama_http", "ollama_cli", "none"],
        help="Topic assigner backend override; if omitted, use SAIVERSE_ASSIGN_LLM_BACKEND env or none",
    )
    ap.add_argument("--limit", type=int, default=None, help="Limit number of messages to ingest (for quick tests)")
    args = ap.parse_args()

    messages = read_persona_log(args.persona_id)
    cfg = Config.from_env()
    # Per-persona DB location
    base = args.location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    per_loc = str(Path(base) / "persona" / args.persona_id)
    cfg.qdrant_location = per_loc
    # Per-persona prefix to avoid cross-contamination
    pref = args.collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{args.persona_id}"
    # LLM assigner selection
    if args.assign_llm:
        cfg.assign_llm_backend = args.assign_llm  # type: ignore

    # Resolve effective LLM backend
    eff_backend = args.assign_llm
    if eff_backend is None:
        eff_backend = os.getenv("SAIVERSE_ASSIGN_LLM_BACKEND") or "none"
    eff_model = os.getenv("SAIVERSE_ASSIGN_LLM_MODEL") or (cfg.assign_llm_model or "")

    if eff_backend == "none" or not eff_backend:
        mc = MemoryCore.create_default(config=cfg, with_dummy_llm=False, llm_backend=None)
        print("Assign LLM: disabled (heuristic assignment)")
    else:
        cfg.assign_llm_backend = eff_backend  # type: ignore
        if eff_model:
            cfg.assign_llm_model = eff_model
        mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True, llm_backend=eff_backend)
        # Surface connection details for Ollama
        if eff_backend.startswith("ollama"):
            print(
                f"Assign LLM: {eff_backend} model={cfg.assign_llm_model or 'qwen2.5:3b'} base={os.getenv('OLLAMA_BASE_URL', 'http://localhost:11434')}"
            )
    conv_id = args.conv_id or f"persona:{args.persona_id}"

    print(f"Per-persona DB: location={cfg.qdrant_location} prefix={cfg.qdrant_collection_prefix}")
    print(f"Backend: {type(mc.storage).__name__}, Embedder: {type(mc.embedder).__name__}")

    turn = 0
    ingested = 0
    new_topics = 0
    matched = 0
    for idx, msg in enumerate(messages):
        if args.limit is not None and idx >= args.limit:
            break
        role = (msg.get("role") or "").lower()
        if role == "system":
            continue
        text = msg.get("content") or ""
        if not text.strip():
            continue
        speaker = "user" if role == "user" else "ai"
        prev_topics = len(mc.storage.list_topics())  # type: ignore[attr-defined]
        mc.ingest_turn(conv_id=conv_id, turn_index=turn, speaker=speaker, text=text, meta={"source":"persona_log"})
        turn += 1
        ingested += 1
        after_topics = len(mc.storage.list_topics())  # type: ignore[attr-defined]
        if after_topics > prev_topics:
            new_topics += 1
        else:
            matched += 1
    print(f"Ingested {ingested} messages from persona:{args.persona_id}")
    print(f"Assignment summary: NEW topics={new_topics}, matched={matched}")

    # Summarize topics
    topics = mc.storage.list_topics()  # type: ignore[attr-defined]
    topics.sort(key=lambda t: (-(t.strength or 0), t.title or ""))
    print(f"Topics: {len(topics)}")
    for i, t in enumerate(topics, 1):
        cnt = len(t.entry_ids) if t.entry_ids else 0
        print(f"[{i:02d}] {t.title}  (entries={cnt})")


if __name__ == "__main__":
    main()
