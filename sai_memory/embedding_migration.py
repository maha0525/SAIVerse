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

Rules on top of the dimension check:

- **A changed model label means a full rebuild, whatever the dimensions.**
  When ``embed_metadata.embed_model`` differs from the current model id (or
  is missing while vectors exist), a re-embed rebuilds every vector as if
  ``force`` had been given (:func:`model_label_changed`). Comparing lengths
  alone would treat an old model of the same dimension (another
  quantisation, say) as already migrated.
- **A full rebuild empties the vector tables.** With ``force`` (including the
  promotions above), every row of the Chronicle / page / fragment vector
  tables is deleted before regenerating. The rows the generators re-create
  come back; the rows they never create (Chronicle level 2+, root / trunk
  pages, rows of deleted entries) are rows that should hold no vector, so
  they are right to go. Search does not filter those rows out, so leaving
  them would keep old-model vectors in recall.
- **A re-embed marks the memory.db until every vector that should exist is
  there.** :func:`begin_reembed` writes
  ``embed_metadata.embed_migration_in_progress`` before anything is deleted.
  :func:`finish_reembed` removes it (and records the model) only when no
  stale vector is left, no Chronicle / page / fragment row is waiting for a
  vector, and the job skipped nothing because of an edit. Otherwise the mark
  stays: the startup check warns on it, and the next re-embed sees it and
  rebuilds everything (``force``), which is how an unfinished job converges.
- **Background embedding of imported messages leaves a mark of its own.**
  :func:`begin_backfill` writes ``embed_metadata.embed_backfill_pending``
  when the import hands its messages to the background thread, and
  :func:`finish_backfill` removes it when that work ended without a
  failure. A process that dies in between leaves the mark; the startup check
  warns, and the banner's re-embed (which also fills messages without a
  vector) removes it.

Every "read the text again, then store the vector" step runs inside
``sai_memory.db_locks.immediate_transaction``, so no other connection can
edit the row between the comparison and the INSERT.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sai_memory.db_locks import immediate_transaction

LOGGER = logging.getLogger(__name__)

#: ``embed_metadata`` key holding the current model identity.
EMBED_MODEL_KEY = "embed_model"

#: ``embed_metadata`` key present while a re-embed job is between its first
#: deletion and its final check. Its value is ``"<model id> <ISO time>"``.
MIGRATION_IN_PROGRESS_KEY = "embed_migration_in_progress"

#: ``embed_metadata`` key present while messages stored by a chat-log import
#: are waiting for (or being given) their vectors in the background. Its value
#: is ``"<ISO time of the oldest unfinished import> <message count>"``.
BACKFILL_PENDING_KEY = "embed_backfill_pending"

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

    Only the length is looked at, so a vector of another model with the same
    dimension is not stale here; a re-embed catches that case through the
    model label (:func:`model_label_changed`), which rebuilds everything.
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


def model_label_changed(conn: sqlite3.Connection, model_id: str) -> bool:
    """True when the recorded model is not ``model_id`` and vectors exist.

    A re-embed treats this as ``force``: the label is the only thing that
    tells two models of the same dimension apart, so a changed label rebuilds
    every vector regardless of their lengths. A missing label counts as
    changed. A memory.db with no vectors at all has nothing to rebuild, so
    the label alone does not promote it.
    """
    from sai_memory.memory.storage import get_embed_metadata

    if get_embed_metadata(conn, EMBED_MODEL_KEY) == model_id:
        return False
    return has_any_vectors(conn)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def begin_reembed(conn: sqlite3.Connection, model_id: str) -> None:
    """Mark the memory.db as being re-embedded (call before deleting anything).

    The mark stays until :func:`finish_reembed` finds the job complete, so a
    job that dies in between, or ends with vectors still missing, is reported
    at the next startup and rebuilt in full by the next re-embed. Only the
    :data:`MIGRATION_IN_PROGRESS_KEY` key is written.
    """
    from sai_memory.memory.storage import set_embed_metadata

    set_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY, f"{model_id} {_now_iso()}")


def reembed_in_progress(conn: sqlite3.Connection) -> Optional[str]:
    """The mark left by :func:`begin_reembed`, or None when there is none."""
    from sai_memory.memory.storage import get_embed_metadata

    return get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY)


# ---------------------------------------------------------------------------
# Background embedding of imported messages
# ---------------------------------------------------------------------------

def begin_backfill(
    conn: sqlite3.Connection, count: int, *, db_lock: Optional[Any] = None,
) -> bool:
    """Mark that ``count`` imported messages are waiting for their vectors.

    Called when a chat-log import hands its messages to the background
    embedding thread, before the import reports success. When a mark is
    already there (an earlier import still running, or one that did not
    finish), its time is kept and the count is added, so the mark keeps
    pointing at the oldest unfinished work.

    Returns True when a new mark was written, False when an existing one was
    extended.
    """
    from sai_memory.memory.storage import get_embed_metadata, set_embed_metadata

    with immediate_transaction(conn, db_lock):
        existing = get_embed_metadata(conn, BACKFILL_PENDING_KEY)
        if existing is None:
            set_embed_metadata(conn, BACKFILL_PENDING_KEY, f"{_now_iso()} {int(count)}", commit=False)
            return True
        since, _, old_count = existing.rpartition(" ")
        try:
            total = int(old_count) + int(count)
        except ValueError:
            since, total = existing, int(count)
        set_embed_metadata(conn, BACKFILL_PENDING_KEY, f"{since or existing} {total}", commit=False)
        return False


def backfill_pending(conn: sqlite3.Connection) -> Optional[str]:
    """The mark written by :func:`begin_backfill`, or None when there is none."""
    from sai_memory.memory.storage import get_embed_metadata

    return get_embed_metadata(conn, BACKFILL_PENDING_KEY)


def finish_backfill(conn: sqlite3.Connection, *, db_lock: Optional[Any] = None) -> None:
    """Remove the mark of :func:`begin_backfill`.

    The caller decides when: only after the background embedding ended with
    nothing left behind (no failure, nothing skipped) and no later import is
    queued behind it (api/routes/people/import_chatlog.py).
    """
    with immediate_transaction(conn, db_lock):
        conn.execute("DELETE FROM embed_metadata WHERE key = ?", (BACKFILL_PENDING_KEY,))


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


def purge_stale_legacy_embeddings(
    conn: sqlite3.Connection,
    expected_dim: int,
    *,
    everything: bool = False,
) -> int:
    """Delete stale rows from the legacy ``embeddings`` table.

    ``init_db`` copies that table into ``message_embeddings`` (as chunk 0,
    ``INSERT OR IGNORE``) on every open. A message whose current rows were
    dropped (no text left, or the message itself is gone) would otherwise get
    its old-model vector copied back on the next open and keep tripping the
    startup check.

    ``everything=True`` (a ``force``-grade re-embed, which includes a changed
    model label) empties the table: a same-dimension model change cannot be
    told apart by length, and after a full rebuild ``message_embeddings`` is
    authoritative — rows left here would only be copied back as vectors of
    the old model.
    """
    if not _relation_exists(conn, "embeddings"):
        return 0
    if everything:
        cur = conn.execute("DELETE FROM embeddings")
        conn.commit()
        return cur.rowcount
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
    db_lock: Optional[Any] = None,
) -> Dict[str, int]:
    """Re-embed the given messages chunk by chunk (same chunking as on write).

    A target with no text left, or whose message row is gone, has its vector
    rows deleted instead: they can only be stale or unreachable, and leaving
    them would keep the startup check firing.

    The text is read again after embedding, inside one
    ``immediate_transaction`` with the store, and the vectors are stored only
    when it is unchanged: a message edited while its vectors were being
    computed is skipped (counted in ``changed``) instead of getting vectors
    of its old text. Its current rows stay as they were; the caller passes
    the count to :func:`finish_reembed`, which then keeps the in-progress
    mark so the next run rebuilds it. A target that gained text while being
    looked at is counted in ``changed`` the same way instead of losing its
    vectors.

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
            with immediate_transaction(conn, db_lock):
                latest = get_message(conn, mid)
                if latest is not None and (latest.content or "").strip():
                    changed += 1
                else:
                    cur = conn.execute(
                        "DELETE FROM message_embeddings WHERE message_id = ?", (mid,)
                    )
                    if cur.rowcount:
                        dropped += 1
        else:
            chunks = chunk_text(msg.content, min_chars=chunk_min, max_chars=chunk_max)
            payload = [c.strip() for c in chunks if c and c.strip()] or [content]
            vectors = embedder.embed(payload, is_query=False)
            with immediate_transaction(conn, db_lock):
                latest = get_message(conn, mid)
                if latest is None or latest.content != msg.content:
                    changed += 1
                    LOGGER.info("Re-embed: message %s changed while embedding; skipped", mid)
                else:
                    replace_message_embeddings(conn, mid, vectors, commit=False)
                    embedded += 1
        if progress is not None and (i % 10 == 0 or i == total):
            progress(i, total)
    return {"embedded": embedded, "dropped": dropped, "changed": changed, "total": total}


@dataclass
class ReembedOutcome:
    """What :func:`finish_reembed` found at the end of a re-embed.

    ``complete`` is True only when nothing is left to do: no stale vector in
    any of the four tables (``stale_tables``), no Chronicle / page /
    fragment row still waiting for a vector (``pending``, per source, only
    sources with a count above zero), and nothing skipped by the job because
    its text was edited while being embedded (``skipped``).
    """

    stale_tables: List[str] = field(default_factory=list)
    pending: Dict[str, int] = field(default_factory=dict)
    skipped: int = 0

    @property
    def complete(self) -> bool:
        return not self.stale_tables and not self.pending and self.skipped == 0

    def describe(self) -> str:
        """One line naming why the job is not complete ("" when it is)."""
        parts = []
        if self.stale_tables:
            parts.append(f"Stale vectors remain in: {', '.join(self.stale_tables)}.")
        if self.pending:
            counts = ", ".join(f"{source} {n}" for source, n in self.pending.items())
            parts.append(f"Entries still without a vector: {counts}.")
        if self.skipped:
            parts.append(f"Skipped {self.skipped} entries edited during the re-embed.")
        return " ".join(parts)


def _pending_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """Rows of the three recall sources that should hold a vector and have none."""
    from sai_memory.unified_recall import (
        get_chronicle_entries_without_embeddings,
        get_fragments_without_embeddings,
        get_memopedia_pages_without_embeddings,
    )

    getters = {
        "chronicle": lambda: get_chronicle_entries_without_embeddings(conn, level=1),
        "memopedia": lambda: get_memopedia_pages_without_embeddings(conn),
        "fragment": lambda: get_fragments_without_embeddings(conn),
    }
    pending: Dict[str, int] = {}
    for source in RECALL_SOURCES:
        table, _, readers = _SOURCES[source]
        if not all(_relation_exists(conn, name) for name in (table, *readers)):
            continue
        n = len(getters[source]())
        if n:
            pending[source] = n
    return pending


def finish_reembed(
    conn: sqlite3.Connection,
    embedder: Any,
    model_id: str,
    *,
    force: bool = False,
    skipped: int = 0,
    backfill_mark: Optional[str] = None,
    db_lock: Optional[Any] = None,
) -> ReembedOutcome:
    """Check that every vector that should exist is there, and only then close the job.

    ``force=True`` (including the escalations: changed model label, leftover
    in-progress mark) first empties the legacy ``embeddings`` table — see
    :func:`purge_stale_legacy_embeddings`.

    The job is complete when all of these hold (:class:`ReembedOutcome`):

    - a full scan (:func:`scan_stale_vector_tables`, not the startup sample)
      finds no stale vector in the four tables;
    - the "without embeddings" getters of the three recall sources
      (Chronicle level 1, pages, fragments) return nothing;
    - ``skipped`` — the rows the caller's job skipped because their text was
      edited while being embedded (generator skips plus the messages'
      ``changed``) — is zero.

    Messages without a vector are not part of the check: some are left
    without one on purpose (``embedding_chunks=0``), so they are judged by
    the stale scan only.

    When complete, the model label is recorded and the in-progress mark of
    :func:`begin_reembed` removed in one transaction. ``backfill_mark`` is the
    value of the import's backfill mark (:func:`backfill_pending`) read just
    before the job listed the messages to embed, given when that pass also
    filled messages without a vector; if the mark still has that value it is
    removed in the same transaction (the job has embedded those messages; a
    changed value means an import queued more work during the job, which
    keeps its mark).

    When not complete, neither the label nor the mark is touched: the
    startup check keeps warning, and the next re-embed finds the mark and
    rebuilds everything, which is how a job cut short converges. Only the
    keys named here are written (the table also holds other subsystems'
    markers).
    """
    from sai_memory.memory.storage import get_embed_metadata, set_embed_metadata

    expected_dim = int(embedder.embedding_size)
    purge_stale_legacy_embeddings(conn, expected_dim, everything=force)
    # The checks run before the transaction: a full scan holds no write lock.
    # Rows written after it come from the current model and are not stale.
    outcome = ReembedOutcome(
        stale_tables=scan_stale_vector_tables(conn, expected_dim),
        pending=_pending_counts(conn),
        skipped=int(skipped),
    )
    if not outcome.complete:
        return outcome
    with immediate_transaction(conn, db_lock):
        set_embed_metadata(conn, EMBED_MODEL_KEY, model_id, commit=False)
        conn.execute("DELETE FROM embed_metadata WHERE key = ?", (MIGRATION_IN_PROGRESS_KEY,))
        if backfill_mark is not None and get_embed_metadata(conn, BACKFILL_PENDING_KEY) == backfill_mark:
            conn.execute("DELETE FROM embed_metadata WHERE key = ?", (BACKFILL_PENDING_KEY,))
    return outcome


# ---------------------------------------------------------------------------
# Chronicle / Memopedia page / Fragment
# ---------------------------------------------------------------------------

# Per source: (vector table, id column, relations the regenerator reads).
_SOURCES: Dict[str, tuple] = {
    "chronicle": ("arasuji_embeddings", "entry_id", ("arasuji_entries",)),
    "memopedia": ("memopedia_embeddings", "page_id", ("memopedia_pages",)),
    "fragment": (
        "memopedia_fragment_embeddings",
        "fragment_id",
        ("memopedia_fragments", "memopedia_pages"),
    ),
}


def _delete_source_vectors(
    conn: sqlite3.Connection,
    source: str,
    expected_dim: int,
    *,
    force: bool,
    db_lock: Optional[Any] = None,
) -> int:
    """Delete the stale rows of one source's vector table; with ``force``, every row."""
    table, id_col, _ = _SOURCES[source]
    with immediate_transaction(conn, db_lock):
        if force:
            return conn.execute(f"DELETE FROM {table}").rowcount
        targets = [
            row_id
            for row_id, vec_json in conn.execute(f"SELECT {id_col}, vector FROM {table}")
            if is_stale_vector(vec_json, expected_dim)
        ]
        if targets:
            conn.executemany(
                f"DELETE FROM {table} WHERE {id_col} = ?", [(t,) for t in targets]
            )
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
    deleted — with ``force=True`` every row of the vector table is deleted —
    and the existing "fill in rows without a vector" generators of
    sai_memory/unified_recall.py are run immediately (Chronicle at level 1,
    the only level that is ever embedded). Rows that had no vector at all are
    filled in by the same pass.

    Rows of entries the generators never embed (Chronicle level 2+, root /
    trunk pages, rows left behind by deleted entries) are deleted when stale
    (and always with ``force``) and not re-created: they are rows that should
    hold no vector. Search does not filter them out, so a same-dimension
    old-model vector left there would still show up in recall.

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
        table, _, readers = _SOURCES[source]
        if not all(_relation_exists(conn, name) for name in (table, *readers)):
            result[source] = {"deleted": 0, "total": 0, "done": 0, "skipped": 0}
            continue
        deleted = _delete_source_vectors(
            conn, source, expected_dim, force=force, db_lock=db_lock,
        )
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
