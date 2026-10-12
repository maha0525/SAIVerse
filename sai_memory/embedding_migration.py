"""Embedding model migration: find stale vectors and rebuild them.

docs/intent/embedding_gemma2_migration.md §2. A vector is *stale* when it was
made by another embedding model — in practice, when its length differs from
the dimension the current model stores (``Embedder.embedding_size``). Stale
vectors raise no error: every search path silently skips them, so the memory
quietly disappears from recall. This module is shared by the startup check
(saiverse_memory/adapter.py), the re-embed API (api/routes/people/reembed.py)
and the CLI (scripts/reembed_memory.py) so that all three agree on what
"stale" means.

Only vectors are touched here. The texts the vectors are made from (messages,
Chronicle, Memopedia pages, fragments) are never modified (invariant 1).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

#: The four tables that hold recall vectors (one per source system).
VECTOR_TABLES = (
    "message_embeddings",
    "arasuji_embeddings",
    "memopedia_embeddings",
    "memopedia_fragment_embeddings",
)

#: Rows sampled from each end (oldest / newest by rowid) of each table at startup.
STARTUP_SAMPLE_PER_END = 8

#: The three recall sources rebuilt by :func:`rebuild_recall_source_embeddings`,
#: in processing order.
RECALL_SOURCES = ("chronicle", "memopedia", "fragment")

ProgressCallback = Callable[[str, int, int], None]


def is_stale_vector(vec_json: Any, expected_dim: int) -> bool:
    """True when a stored vector cannot be compared with current query vectors.

    Unparseable JSON and non-list values count as stale (the re-embed job has
    always rebuilt those). The legacy "list of vectors" message format also
    counts as stale: its outer length is the chunk count, not the dimension,
    and re-embedding rewrites it as one row per chunk.
    """
    try:
        vec = json.loads(vec_json)
    except (json.JSONDecodeError, TypeError):
        return True
    return not isinstance(vec, list) or len(vec) != expected_dim


def _relation_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def has_any_vectors(conn: sqlite3.Connection) -> bool:
    """True when at least one of the four vector tables has a row."""
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        row = conn.execute(f"SELECT EXISTS(SELECT 1 FROM {table} LIMIT 1)").fetchone()
        if row and row[0]:
            return True
    return False


def sample_stale_vector_tables(
    conn: sqlite3.Connection,
    expected_dim: int,
    *,
    per_end: int = STARTUP_SAMPLE_PER_END,
) -> List[str]:
    """Return the vector tables in which a sampled row is stale.

    This is a sample, not a full scan: from each table only the ``per_end``
    oldest and ``per_end`` newest rows (by rowid) are parsed, so the check
    stays cheap at startup even for memory.db files with tens of thousands of
    chunks. A migration replaces every row of a table, so a table that is
    only partly migrated shows stale rows at the old end; a table that is
    fully stale shows them everywhere. A stale row in the middle of an
    otherwise fresh table is not guaranteed to be found.
    """
    stale: List[str] = []
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        rows = conn.execute(
            f"SELECT vector FROM {table} ORDER BY rowid ASC LIMIT ?", (per_end,)
        ).fetchall()
        rows += conn.execute(
            f"SELECT vector FROM {table} ORDER BY rowid DESC LIMIT ?", (per_end,)
        ).fetchall()
        if any(is_stale_vector(r[0], expected_dim) for r in rows):
            stale.append(table)
    return stale


def scan_stale_vector_tables(conn: sqlite3.Connection, expected_dim: int) -> List[str]:
    """Return the vector tables that hold at least one stale row (full scan).

    Unlike :func:`sample_stale_vector_tables`, every row of the four tables is
    parsed. The final check of a re-embed (:func:`finish_reembed`) runs only
    once per job and decides whether the startup warning is lifted, so it
    must not miss a stale row in the middle of a table; the cost of one full
    pass is acceptable there, unlike at every startup.
    """
    stale: List[str] = []
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        for (vec_json,) in conn.execute(f"SELECT vector FROM {table}"):
            if is_stale_vector(vec_json, expected_dim):
                stale.append(table)
                break
    return stale


def purge_stale_legacy_embeddings(conn: sqlite3.Connection, expected_dim: int) -> int:
    """Delete stale rows from the legacy ``embeddings`` table.

    ``init_db`` copies that table into ``message_embeddings`` (as chunk 0,
    ``INSERT OR IGNORE``) on every open. A message whose current rows were
    dropped (no text left, or the message itself is gone) would otherwise get
    its old-model vector copied back on the next open and keep tripping the
    startup check.
    """
    if not _relation_exists(conn, "embeddings"):
        return 0
    stale_ids = [
        mid
        for mid, vec_json in conn.execute("SELECT message_id, vector FROM embeddings")
        if is_stale_vector(vec_json, expected_dim)
    ]
    if stale_ids:
        conn.executemany("DELETE FROM embeddings WHERE message_id = ?", [(m,) for m in stale_ids])
        conn.commit()
    return len(stale_ids)


def highest_vector_dim(conn: sqlite3.Connection) -> int:
    """Largest stored vector length across the four vector tables (full scan).

    The CLI uses it to refuse an accidental re-embed to a smaller dimension.
    """
    highest = 0
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        for (vec_json,) in conn.execute(f"SELECT vector FROM {table}"):
            try:
                vec = json.loads(vec_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(vec, list) and len(vec) > highest:
                highest = len(vec)
    return highest


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def message_reembed_targets(
    conn: sqlite3.Connection,
    expected_dim: int,
    *,
    force: bool,
    include_missing: bool,
) -> set:
    """Message ids whose vectors :func:`reembed_messages` should rebuild.

    - always: ids with a stale vector row (see :func:`is_stale_vector`)
    - ``include_missing``: also messages with no vector row at all
    - ``force``: every message, plus every id that has vector rows (so rows
      left behind by deleted messages are cleared too)
    """
    if force:
        ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
        ids |= {r[0] for r in conn.execute("SELECT DISTINCT message_id FROM message_embeddings")}
        return ids
    targets = set()
    embedded = set()
    for mid, vec_json in conn.execute("SELECT message_id, vector FROM message_embeddings"):
        embedded.add(mid)
        if is_stale_vector(vec_json, expected_dim):
            targets.add(mid)
    if include_missing:
        targets |= {r[0] for r in conn.execute("SELECT id FROM messages")} - embedded
    return targets


def reembed_messages(
    conn: sqlite3.Connection,
    embedder: Any,
    message_ids,
    *,
    chunk_min: int,
    chunk_max: int,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, int]:
    """Re-embed the given messages chunk by chunk (same chunking as on write).

    A target with no text left, or whose message row is gone, has its vector
    rows deleted instead: they can only be stale or unreachable, and leaving
    them would keep the startup check firing.

    The text is read again after embedding and the vectors are stored only
    when it is unchanged: a message edited while its vectors were being
    computed is skipped (counted in ``changed``) instead of getting vectors
    of its old text. Its current rows stay as they were, so a later run (or
    the startup check) still sees it.

    ``progress(done, total)`` is called every 10 targets and at the end.
    Returns ``{"embedded": n, "dropped": n, "changed": n, "total": n}``.
    """
    from sai_memory.memory.chunking import chunk_text
    from sai_memory.memory.storage import get_message, replace_message_embeddings

    targets = list(message_ids)
    total = len(targets)
    embedded = 0
    dropped = 0
    changed = 0
    for i, mid in enumerate(targets, start=1):
        msg = get_message(conn, mid)
        content = (msg.content or "").strip() if msg is not None else ""
        if not content:
            cur = conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (mid,))
            conn.commit()
            if cur.rowcount:
                dropped += 1
        else:
            chunks = chunk_text(msg.content, min_chars=chunk_min, max_chars=chunk_max)
            payload = [c.strip() for c in chunks if c and c.strip()] or [content]
            vectors = embedder.embed(payload, is_query=False)
            latest = get_message(conn, mid)
            if latest is None or latest.content != msg.content:
                changed += 1
                LOGGER.info("Re-embed: message %s changed while embedding; skipped", mid)
            else:
                replace_message_embeddings(conn, mid, vectors)
                embedded += 1
        if progress is not None and (i % 10 == 0 or i == total):
            progress(i, total)
    return {"embedded": embedded, "dropped": dropped, "changed": changed, "total": total}


def finish_reembed(conn: sqlite3.Connection, embedder: Any, model_id: str) -> List[str]:
    """Record the model in ``embed_metadata`` and check every vector once more.

    Writes only the ``embed_model`` key (the table also holds other
    subsystems' markers). Returns the tables that still hold a stale vector
    — empty means the startup warning for this persona can be lifted.

    The final check is a full scan (:func:`scan_stale_vector_tables`), not
    the startup sample: it runs only once per re-embed, and lifting the
    warning while a stale row is left in the middle of a table would hide
    that memory from recall with nothing left to report it.
    """
    from sai_memory.memory.storage import set_embed_metadata

    expected_dim = int(embedder.embedding_size)
    purge_stale_legacy_embeddings(conn, expected_dim)
    set_embed_metadata(conn, "embed_model", model_id)
    return scan_stale_vector_tables(conn, expected_dim)


# ---------------------------------------------------------------------------
# Chronicle / Memopedia page / Fragment
# ---------------------------------------------------------------------------

# Per source: (vector table, id column, relations the regenerator reads,
# SQL selecting the ids the regenerator re-creates). The last item must stay
# in step with the "without embeddings" getters in sai_memory/unified_recall.py
# (get_chronicle_entries_without_embeddings with level=1,
# get_memopedia_pages_without_embeddings, get_fragments_without_embeddings):
# force mode deletes only rows that those getters will hand back for
# regeneration, so it never drops a vector that would not come back.
_SOURCES: Dict[str, tuple] = {
    "chronicle": (
        "arasuji_embeddings",
        "entry_id",
        ("arasuji_entries",),
        "SELECT id FROM arasuji_entries WHERE level = 1",
    ),
    "memopedia": (
        "memopedia_embeddings",
        "page_id",
        ("memopedia_pages",),
        "SELECT id FROM memopedia_pages"
        " WHERE id NOT LIKE 'root_%' AND is_trunk = 0"
        " AND (category IS NULL OR category != 'chronicle')",
    ),
    "fragment": (
        "memopedia_fragment_embeddings",
        "fragment_id",
        ("memopedia_fragments", "memopedia_pages"),
        "SELECT id FROM memopedia_fragments",
    ),
}


def _delete_source_vectors(
    conn: sqlite3.Connection,
    source: str,
    expected_dim: int,
    *,
    force: bool,
) -> int:
    table, id_col, _, regenerable_sql = _SOURCES[source]
    targets = {
        row_id
        for row_id, vec_json in conn.execute(f"SELECT {id_col}, vector FROM {table}")
        if is_stale_vector(vec_json, expected_dim)
    }
    if force:
        regenerable = {r[0] for r in conn.execute(regenerable_sql)}
        existing = {r[0] for r in conn.execute(f"SELECT {id_col} FROM {table}")}
        targets |= regenerable & existing
    if targets:
        conn.executemany(
            f"DELETE FROM {table} WHERE {id_col} = ?", [(t,) for t in targets]
        )
        conn.commit()
    return len(targets)


def rebuild_recall_source_embeddings(
    conn: sqlite3.Connection,
    embedder: Any,
    *,
    force: bool = False,
    db_lock: Optional[Any] = None,
    progress: Optional[ProgressCallback] = None,
) -> Dict[str, Dict[str, int]]:
    """Delete stale Chronicle / page / fragment vectors and regenerate them now.

    For each of :data:`RECALL_SOURCES`: rows whose vector is stale (see
    :func:`is_stale_vector`, compared with ``embedder.embedding_size``) are
    deleted — with ``force=True`` every row the regenerator re-creates is
    deleted as well — and the existing "fill in rows without a vector"
    generators of sai_memory/unified_recall.py are run immediately (Chronicle
    at level 1, the only level that is ever embedded). Rows that had no vector
    at all are filled in by the same pass.

    Stale rows of entries the generators never embed (Chronicle level 2+,
    root / trunk pages, rows left behind by deleted entries) are deleted and
    not re-created: they were unreachable by recall already.

    ``progress(source, done, total)`` is called when a source starts
    (``done=0``) and after every stored batch.

    Returns ``{source: {"deleted": n, "total": n, "done": n, "skipped": n}}``
    (``skipped``: rows left unembedded because their text changed during the
    run). A source whose tables do not exist in this memory.db is reported
    with zeros.
    """
    from sai_memory.unified_recall import (
        embed_chronicle_entries,
        embed_memopedia_fragments,
        embed_memopedia_pages,
        get_chronicle_entries_without_embeddings,
        get_fragments_without_embeddings,
        get_memopedia_pages_without_embeddings,
    )

    expected_dim = int(embedder.embedding_size)
    pending = {
        "chronicle": lambda: get_chronicle_entries_without_embeddings(conn, level=1),
        "memopedia": lambda: get_memopedia_pages_without_embeddings(conn),
        "fragment": lambda: get_fragments_without_embeddings(conn),
    }
    generate = {
        "chronicle": lambda cb: embed_chronicle_entries(
            conn, embedder, level=1, db_lock=db_lock, progress_callback=cb
        ),
        "memopedia": lambda cb: embed_memopedia_pages(
            conn, embedder, db_lock=db_lock, progress_callback=cb
        ),
        "fragment": lambda cb: embed_memopedia_fragments(
            conn, embedder, db_lock=db_lock, progress_callback=cb
        ),
    }

    result: Dict[str, Dict[str, int]] = {}
    for source in RECALL_SOURCES:
        table, _, readers, _ = _SOURCES[source]
        if not all(_relation_exists(conn, name) for name in (table, *readers)):
            result[source] = {"deleted": 0, "total": 0, "done": 0, "skipped": 0}
            continue
        deleted = _delete_source_vectors(conn, source, expected_dim, force=force)
        total = len(pending[source]())
        if progress is not None:
            progress(source, 0, total)

        def _cb(done: int, _source: str = source, _total: int = total) -> None:
            if progress is not None:
                progress(_source, done, _total)

        done = generate[source](_cb) if total else 0
        # Rows the generator skipped because their text changed (or the row
        # went away) while it was embedding them; they keep no vector and the
        # next generation pass embeds the new text.
        skipped = max(total - done, 0)
        result[source] = {"deleted": deleted, "total": total, "done": done, "skipped": skipped}
        LOGGER.info(
            "Re-embed %s: deleted %d stale/forced vectors, embedded %d/%d (skipped %d changed)",
            source, deleted, done, total, skipped,
        )
    return result
