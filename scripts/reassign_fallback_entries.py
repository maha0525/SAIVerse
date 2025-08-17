#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import List, Tuple

try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from memory_core import MemoryCore
from memory_core.config import Config


def _force_qdrant(cfg: Config, persona_id: str, location_base: str | None, collection_prefix: str | None) -> None:
    cfg.storage_backend = "qdrant"  # type: ignore
    base = location_base or cfg.qdrant_location or str(Path.home() / ".saiverse" / "qdrant")
    base = os.path.expandvars(os.path.expanduser(base))
    per_loc = str(Path(base) / "persona" / persona_id)
    cfg.qdrant_location = per_loc
    pref = collection_prefix or (cfg.qdrant_collection_prefix or "saiverse")
    cfg.qdrant_collection_prefix = f"{pref}_{persona_id}"


def _list_all_entries(mc: MemoryCore) -> List[Tuple[str, dict]]:
    """Return list of (id, payload) for all entries. Qdrant backend only."""
    storage = mc.storage
    try:
        # Access Qdrant internals
        client = storage.client  # type: ignore[attr-defined]
        entries_col = storage.entries_col  # type: ignore[attr-defined]
        qmodels = storage._qmodels  # type: ignore[attr-defined]
    except Exception as e:
        raise RuntimeError("Qdrant backend is required for reassign script") from e

    out: List[Tuple[str, dict]] = []
    next_page = None
    while True:
        pts, next_page = client.scroll(collection_name=entries_col, with_payload=True, with_vectors=False, limit=256, offset=next_page)
        for p in pts:
            out.append((str(p.id), p.payload or {}))
        if next_page is None:
            break
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Reassign topics for entries created via DummyLLM (fallback)")
    ap.add_argument("persona_id", help="Persona ID whose per-persona memory will be scanned")
    ap.add_argument("--location-base", default=None, help="Base dir for per-persona DB (default: QDRANT_LOCATION or ~/.saiverse/qdrant)")
    ap.add_argument("--collection-prefix", default=None, help="Collection prefix used during ingestion")
    ap.add_argument("--conv-id", default=None, help="Restrict to a single conversation id")
    ap.add_argument("--limit", type=int, default=None, help="Max number of fallback entries to process")
    ap.add_argument("--dry-run", action="store_true", help="Print actions but do not modify the DB")
    ap.add_argument("--assign-llm", default=None, choices=["dummy", "ollama_http", "ollama_cli", "gemini"], help="Override assignment backend (recommended: gemini or ollama_*)")
    args = ap.parse_args()

    cfg = Config.from_env()
    _force_qdrant(cfg, args.persona_id, args.location_base, args.collection_prefix)

    # Choose backend for (re)assignment
    backend = args.assign_llm or cfg.assign_llm_backend
    if backend == "dummy":
        print("Warning: assign-llm is set to dummy; reassign will have no effect.")

    mc = MemoryCore.create_default(config=cfg, with_dummy_llm=True, llm_backend=backend)
    print(f"DB: location={cfg.qdrant_location} prefix={cfg.qdrant_collection_prefix}")
    print(f"Reassign backend: {backend} model={getattr(cfg, 'assign_gemini_model', None) or cfg.assign_llm_model}")

    items = _list_all_entries(mc)
    # Filter fallback_dummy entries
    targets: List[str] = []
    for pid, payload in items:
        meta = payload.get("meta") or {}
        status = (meta.get("assign_llm_status") or meta.get("assign_status") or "").lower()
        conv = payload.get("conversation_id")
        if status == "fallback_dummy" and (not args.conv_id or args.conv_id == conv):
            targets.append(pid)

    print(f"Found {len(targets)} fallback_dummy entries.")
    if not targets:
        return

    if args.limit is not None:
        targets = targets[: args.limit]

    processed = 0
    for pid in targets:
        e = mc.storage.get_entry(pid)  # type: ignore[arg-type]
        if not e:
            continue
        if args.dry_run:
            print(f"[DRY] Reassign conv_id={e.conversation_id} turn={e.turn_index} speaker={e.speaker} id={e.id}")
            continue

        # Re-ingest with proper assigner; link original -> new via meta
        new = mc.remember(e.raw_text, conv_id=e.conversation_id, speaker=e.speaker, meta={"reprocess_of": e.id})
        # Update original entry meta to mark superseded
        try:
            e.meta = e.meta or {}
            e.meta["superseded_by"] = new.id
            mc.storage.upsert_entry(e)
        except Exception:
            pass
        processed += 1
        print(f"Reassigned entry {e.id} -> {new.id}")

    print(f"Done. Reprocessed {processed} entries.")


if __name__ == "__main__":
    main()

