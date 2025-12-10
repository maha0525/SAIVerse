#!/usr/bin/env python3
"""Re-embed SAIMemory messages whose vectors have an unexpected dimension."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from sai_memory.config import load_settings
from sai_memory.memory.chunking import chunk_text
from sai_memory.memory.recall import Embedder
from sai_memory.memory.storage import (
    get_message,
    init_db,
    replace_message_embeddings,
)


def _normalize_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return str(Path(value).expanduser().resolve())


def _build_embedder(
    *,
    model_override: Optional[str],
    model_path_override: Optional[str],
    model_dim_override: Optional[int],
) -> Tuple[Embedder, int]:
    settings = load_settings()
    model_name = (model_override or settings.embed_model or "").strip()
    model_path = _normalize_path(model_path_override or settings.embed_model_path)
    model_dim = model_dim_override or settings.embed_model_dim

    embedder = Embedder(
        model=model_name,
        local_model_path=model_path,
        model_dim=model_dim,
    )
    expected_dim = embedder.model.embedding_size
    return embedder, expected_dim


def _reembed_persona(
    persona_id: str,
    *,
    embedder: Embedder,
    expected_dim: int,
    chunk_min: int,
    chunk_max: int,
    force: bool = False,
) -> None:
    db_path = Path.home() / ".saiverse" / "personas" / persona_id / "memory.db"
    if not db_path.exists():
        print(f"[skip] memory.db not found for {persona_id}: {db_path}", file=sys.stderr)
        return

    try:
        conn = init_db(str(db_path), check_same_thread=False)
    except Exception as exc:  # pragma: no cover - defensive guard
        print(
            f"[error] {persona_id}: failed to open {db_path} ({exc}). "
            "Ensure no other process is locking the database and that you have write permissions.",
            file=sys.stderr,
        )
        return

    try:
        # Collect target message IDs
        if force:
            # Force mode: re-embed all messages
            all_ids: set[str] = set()
            for (mid,) in conn.execute("SELECT DISTINCT id FROM messages"):
                all_ids.add(mid)
            target_ids = all_ids
            if not target_ids:
                print(f"[ok] {persona_id}: no messages found.")
                return
            print(f"[force] {persona_id}: re-embedding all {len(target_ids)} messages...")
        else:
            # Normal mode: only re-embed mismatched dimensions
            bad_ids: set[str] = set()
            highest_dim = 0
            for mid, _, vec_json in conn.execute(
                "SELECT message_id, chunk_index, vector FROM message_embeddings"
            ):
                try:
                    vec = json.loads(vec_json)
                except json.JSONDecodeError:
                    bad_ids.add(mid)
                    continue
                vec_len = len(vec)
                if vec_len > highest_dim:
                    highest_dim = vec_len
                if vec_len != expected_dim:
                    bad_ids.add(mid)

            if bad_ids and highest_dim > expected_dim:
                print(
                    f"[error] {persona_id}: existing embeddings up to dimension {highest_dim}, "
                    f"but the selected model provides dimension {expected_dim}. "
                    "Refusing to re-embed to a smaller dimension.",
                    file=sys.stderr,
                )
                return

            if not bad_ids:
                print(f"[ok] {persona_id}: no mismatched embeddings detected.")
                return
            target_ids = bad_ids

        fixed = 0
        for mid in target_ids:
            msg = get_message(conn, mid)
            if msg is None or not msg.content:
                continue
            chunks = chunk_text(
                msg.content,
                min_chars=chunk_min,
                max_chars=chunk_max,
            )
            payload: List[str] = [c.strip() for c in chunks if c and c.strip()]
            if not payload:
                payload = [msg.content.strip()]
            if not payload:
                continue
            vectors = embedder.embed(payload, is_query=False)
            replace_message_embeddings(conn, mid, vectors)
            fixed += 1

        print(f"[done] {persona_id}: re-embedded {fixed} messages (expected dim {expected_dim}).")
    finally:
        conn.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-embed SAIMemory messages with mismatched vector dimensions."
    )
    parser.add_argument(
        "--model",
        help="Override embedding model name (e.g., intfloat/multilingual-e5-large). "
        "Defaults to SAIMEMORY_EMBED_MODEL or config.",
    )
    parser.add_argument(
        "--model-path",
        help="Override local model directory. Defaults to SAIMEMORY_EMBED_MODEL_PATH or config.",
    )
    parser.add_argument(
        "--model-dim",
        type=int,
        help="Explicit embedding dimension override if the model metadata is unavailable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-embed all messages regardless of dimension match. "
        "Use this when changing embedding prefixes or model settings.",
    )
    parser.add_argument(
        "personas",
        nargs="+",
        help="Persona IDs whose memory.db should be scanned and re-embedded.",
    )
    args = parser.parse_args(argv)

    embedder, expected_dim = _build_embedder(
        model_override=args.model,
        model_path_override=args.model_path,
        model_dim_override=args.model_dim,
    )
    settings = load_settings()

    for persona_id in args.personas:
        _reembed_persona(
            persona_id,
            embedder=embedder,
            expected_dim=expected_dim,
            chunk_min=settings.chunk_min_chars,
            chunk_max=settings.chunk_max_chars,
            force=args.force,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
