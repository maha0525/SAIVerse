#!/usr/bin/env python3
"""Rebuild SAIMemory recall vectors made by another embedding model.

Covers all four vector systems (messages, Chronicle, Memopedia pages,
fragments; docs/intent/embedding_gemma2_migration.md §2) and records the
current model in ``embed_metadata.embed_model`` at the end, so the next
startup does not warn again.

A recorded model label that differs from the current one rebuilds every
vector, whatever the dimensions (the label is what tells two models of the
same dimension apart). The memory.db is marked while the job runs, so a run
that dies half way is warned about at the next startup.
"""

from __future__ import annotations

import os

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from sai_memory.config import load_settings
from sai_memory.embedding_migration import (
    begin_reembed,
    finish_reembed,
    highest_vector_dim,
    message_reembed_targets,
    model_label_changed,
    reembed_in_progress,
    rebuild_recall_source_embeddings,
    reembed_messages,
)
from sai_memory.memory.recall import Embedder
from sai_memory.memory.storage import init_db


def _build_embedder() -> Tuple[Embedder, int]:
    settings = load_settings()
    embedder = Embedder(local_model_path=settings.embed_model_path)
    expected_dim = embedder.embedding_size
    return embedder, expected_dim


def _reembed_persona(
    persona_id: str,
    *,
    embedder: Embedder,
    expected_dim: int,
    chunk_min: int,
    chunk_max: int,
    model_id: str,
    force: bool = False,
) -> None:
    db_path = Path(os.getenv("SAIVERSE_HOME") or Path.home() / ".saiverse") / "personas" / persona_id / "memory.db"
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
        if not force:
            highest_dim = highest_vector_dim(conn)
            if highest_dim > expected_dim:
                print(
                    f"[error] {persona_id}: existing embeddings up to dimension {highest_dim}, "
                    f"but the current model provides dimension {expected_dim}. "
                    "Refusing to re-embed to a smaller dimension (use --force to override).",
                    file=sys.stderr,
                )
                return
        else:
            print(f"[force] {persona_id}: rebuilding every recall vector...")

        # A changed model label rebuilds everything regardless of dimensions
        # (checked after the smaller-dimension guard above). So does an
        # in-progress mark left by a run that died half way.
        if not force and model_label_changed(conn, model_id):
            print(
                f"[force] {persona_id}: recorded embed_model differs from {model_id}; "
                "rebuilding every recall vector..."
            )
            force = True
        if not force and reembed_in_progress(conn):
            print(
                f"[force] {persona_id}: a previous re-embed did not finish; "
                "rebuilding every recall vector..."
            )
            force = True
        # Mark the memory.db before the first deletion; finish_reembed removes it.
        begin_reembed(conn, model_id)

        sources = rebuild_recall_source_embeddings(conn, embedder, force=force)
        for source, counts in sources.items():
            print(
                f"[{source}] {persona_id}: deleted {counts['deleted']}, "
                f"embedded {counts['done']}/{counts['total']}"
                f" (skipped {counts['skipped']} changed while embedding)"
            )

        target_ids = message_reembed_targets(
            conn, expected_dim, force=force, include_missing=False,
        )
        result = reembed_messages(
            conn, embedder, target_ids, chunk_min=chunk_min, chunk_max=chunk_max,
        )
        print(
            f"[messages] {persona_id}: re-embedded {result['embedded']} messages, "
            f"dropped vectors of {result['dropped']} without text, "
            f"skipped {result['changed']} changed while embedding (expected dim {expected_dim})."
        )

        still_stale = finish_reembed(conn, embedder, model_id, force=force)
        if still_stale:
            print(
                f"[warn] {persona_id}: stale vectors remain in {', '.join(still_stale)}.",
                file=sys.stderr,
            )
        else:
            print(f"[done] {persona_id}: recorded embed_model={model_id}.")
    finally:
        conn.close()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild SAIMemory recall vectors (messages, Chronicle, Memopedia pages, "
        "fragments) whose dimension does not match the current embedding model. "
        "If the recorded model differs from the current one, every vector is rebuilt."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild every recall vector regardless of dimension match. "
        "Use this when changing embedding prompts or model settings.",
    )
    parser.add_argument(
        "personas",
        nargs="+",
        help="Persona IDs whose memory.db should be scanned and re-embedded.",
    )
    args = parser.parse_args(argv)

    embedder, expected_dim = _build_embedder()
    settings = load_settings()

    for persona_id in args.personas:
        _reembed_persona(
            persona_id,
            embedder=embedder,
            expected_dim=expected_dim,
            chunk_min=settings.chunk_min_chars,
            chunk_max=settings.chunk_max_chars,
            model_id=settings.embed_model,
            force=args.force,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
