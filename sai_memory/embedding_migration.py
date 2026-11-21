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
- **A re-embed marks the memory.db until no vector of another space is
  left.** :func:`begin_reembed` writes
  ``embed_metadata.embed_migration_in_progress`` (with a token naming the
  job) before anything is deleted. :func:`finish_reembed` removes it (and
  records the model) only when a full scan finds no stale vector, no
  message kept its old vector because it was edited during the job, no
  empty vector row is left (counted inside the closing transaction), and
  only if the mark is still this job's. Otherwise the mark stays: the
  startup check warns on it, and the next re-embed sees it and rebuilds
  everything (``force``), which is how an unfinished job converges.
- **Rows without a vector are not the migration's business.** A Chronicle /
  page / fragment row left without a vector (skipped because it was edited
  while being embedded, or written after the job's pass) is embedded with
  the current model by the next Metabolism (``ensure_recall_embeddings``).
  The migration only has to leave no vector of another space behind.
- **Deferred embeddings are stale vectors.** A chat-log import stores each
  message with an empty vector row (``storage.DEFERRED_VECTOR``) and embeds
  in the background; every other message write or edit whose vectors could
  not be made leaves the same row (saiverse_memory/adapter.py,
  ``EMBEDDING_DEFERRED``). The empty row is caught by the same stale-vector
  checks as an old model's vector, so a background job that never finishes
  is warned about at startup and finished by the banner's re-embed, with no
  bookkeeping of its own.

The module also holds :func:`convert_json_vectors`, which rewrites stored
JSON-format rows in the byte format (docs/intent/vector_binary_storage.md
§4). It runs before the startup check and at the start of every re-embed, so
that rows it can convert are not counted as stale and rebuilt.

Every "read the text again, then store the vector" step runs inside
``sai_memory.db_locks.immediate_transaction``, so no other connection can
edit the row between the comparison and the INSERT.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sai_memory.db_locks import immediate_transaction
from sai_memory.vector_codec import VECTOR_FOR_DIM_SQL as _VEC
from sai_memory.vector_codec import stale_vector_sql, stored_vector_dim

LOGGER = logging.getLogger(__name__)

#: ``embed_metadata`` key holding the current model identity.
EMBED_MODEL_KEY = "embed_model"

#: ``embed_metadata`` key present while a re-embed job is between its first
#: deletion and its final check. Its value is
#: ``"<model id> <ISO time> <job token>"``.
MIGRATION_IN_PROGRESS_KEY = "embed_migration_in_progress"

#: The four tables that hold recall vectors (one per source system).
VECTOR_TABLES = (
    "message_embeddings",
    "arasuji_embeddings",
    "memopedia_embeddings",
    "memopedia_fragment_embeddings",
)

#: The three recall sources rebuilt by :func:`rebuild_recall_source_embeddings`,
#: in processing order.
RECALL_SOURCES = ("chronicle", "memopedia", "fragment")

ProgressCallback = Callable[[str, int, int], None]


def is_stale_vector(stored: Any, expected_dim: int) -> bool:
    """True when a stored vector cannot be compared with current query vectors.

    Only a byte-format value (``sai_memory.vector_codec``) of ``expected_dim``
    is fresh. Everything else is stale:

    - a byte-format value of another dimension;
    - a JSON-format row, whatever its length (docs/intent/vector_binary_storage.md
      §5; search skips JSON-format rows, so they cannot be compared). This
      includes the legacy "list of vectors" message format;
    - the empty vector row of a deferred embedding (``storage.DEFERRED_VECTOR``),
      as before;
    - an unreadable value.

    Only the dimension is looked at, so a vector of another model with the
    same dimension is not stale here; a re-embed catches that case through
    the model label (:func:`model_label_changed`), which rebuilds everything.
    """
    return stored_vector_dim(stored) != expected_dim


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


def begin_reembed(conn: sqlite3.Connection, model_id: str) -> str:
    """Mark the memory.db as being re-embedded (call before deleting anything).

    Returns the job's token, to be given to :func:`finish_reembed`. The mark
    stays until that job's :func:`finish_reembed` finds it complete, so a job
    that dies in between, or ends with work left, is reported at the next
    startup and rebuilt in full by the next re-embed. A second job started
    meanwhile (the API and the CLI are separate processes) overwrites the
    mark with its own token, so the first job's finish does not remove the
    mark of a job that may still be deleting vectors. Only the
    :data:`MIGRATION_IN_PROGRESS_KEY` key is written.
    """
    from sai_memory.memory.storage import set_embed_metadata

    token = uuid.uuid4().hex
    set_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY, f"{model_id} {_now_iso()} {token}")
    return token


def _mark_token(mark: Optional[str]) -> Optional[str]:
    if not mark:
        return None
    return mark.rsplit(" ", 1)[-1]


def reembed_in_progress(conn: sqlite3.Connection) -> Optional[str]:
    """The mark left by :func:`begin_reembed`, or None when there is none."""
    from sai_memory.memory.storage import get_embed_metadata

    return get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY)


def scan_stale_vector_tables(
    conn: sqlite3.Connection,
    expected_dim: int,
    *,
    skip_deferred: bool = False,
) -> List[str]:
    """Return the vector tables that hold at least one stale row (every row).

    Every row of the four tables is checked, inside SQLite, with
    ``vector_codec.stale_vector_sql`` (the same rule as :func:`is_stale_vector`:
    only a byte-format value of ``expected_dim`` is fresh; ``NULL``, JSON-format
    rows, the empty vector row, unreadable values and other dimensions are
    stale). Only ``typeof`` and ``length`` of each value are looked at, so no
    value is parsed in Python, and the search of a table stops at its first
    stale row.

    Used by the startup check (saiverse_memory/adapter.py) and the final check
    of a re-embed (:func:`finish_reembed`); neither may miss a stale row in
    the middle of a table, since search skips such a row without a warning.

    ``skip_deferred=True`` does not count the empty vector row of a deferred
    embedding (``storage.DEFERRED_VECTOR``) in ``message_embeddings``. The
    startup check counts and reports those rows on their own
    (``count_deferred_message_embeddings``), so it passes this to keep them
    out of the "length differs" report. Only ``message_embeddings`` holds
    deferred embeddings; ``'[]'`` in any other table is always counted.
    """
    from sai_memory.memory.storage import DEFERRED_VECTOR

    stale_condition = stale_vector_sql(expected_dim)
    stale: List[str] = []
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        condition = stale_condition
        params: tuple = ()
        # The empty vector row is a deferred embedding only in
        # message_embeddings, the one table count_deferred_message_embeddings
        # counts. In the other tables '[]' is a stale value nothing else
        # reports, so it is never skipped there.
        if skip_deferred and table == "message_embeddings":
            condition += " AND NOT (typeof(vector) = 'text' AND vector = ?)"
            params = (DEFERRED_VECTOR,)
        row = conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE {condition})", params
        ).fetchone()
        if row and row[0]:
            stale.append(table)
    return stale


def purge_stale_legacy_embeddings(
    conn: sqlite3.Connection,
    expected_dim: int,
    *,
    everything: bool = False,
    db_lock: Optional[Any] = None,
) -> int:
    """Delete stale rows from the legacy ``embeddings`` table.

    ``init_db`` copies that table into ``message_embeddings`` (as chunk 0,
    ``INSERT OR IGNORE``) on every open. A message whose current rows were
    dropped (no text left, or the message itself is gone) would otherwise get
    its old-model vector copied back on the next open and keep tripping the
    startup check.

    Every value of this table is in the JSON format (nothing writes to it any
    more), and a JSON-format row is stale (:func:`is_stale_vector`), so
    without ``everything`` every row with a vector is deleted too.

    ``everything=True`` empties the table. Two callers use it:

    - a ``force``-grade re-embed (which includes a changed model label): a
      same-dimension model change cannot be told apart by length, and after a
      full rebuild ``message_embeddings`` is authoritative — rows left here
      would only be copied back as vectors of the old model;
    - :func:`convert_json_vectors`, once every table has been converted: the
      rows here were already copied into ``message_embeddings`` by ``init_db``
      and converted there, and rows left here would be copied back in the
      JSON format on the next open (docs/intent/vector_binary_storage.md §4).

    The deletion runs inside ``immediate_transaction``.
    """
    if not _relation_exists(conn, "embeddings"):
        return 0
    if everything:
        with immediate_transaction(conn, db_lock):
            cur = conn.execute("DELETE FROM embeddings")
        return cur.rowcount
    with immediate_transaction(conn, db_lock):
        stale_ids = [
            mid
            for mid, stored in conn.execute(f"SELECT message_id, {_VEC} FROM embeddings")
            if is_stale_vector(stored, expected_dim)
        ]
        if stale_ids:
            conn.executemany(
                "DELETE FROM embeddings WHERE message_id = ?", [(m,) for m in stale_ids]
            )
    return len(stale_ids)


# ---------------------------------------------------------------------------
# JSON format -> byte format (docs/intent/vector_binary_storage.md §4)
# ---------------------------------------------------------------------------

#: Rows converted per transaction by :func:`convert_json_vectors`.
CONVERSION_BATCH_SIZE = 1000


@dataclass
class VectorConversionResult:
    """What :func:`convert_json_vectors` did.

    ``tables`` maps each vector table present in the memory.db to
    ``{"converted": n, "kept": n}``: ``converted`` rows were rewritten in the
    byte format; ``kept`` rows were JSON-format rows that are not one vector
    (broken values, the legacy "list of vectors" message format) and were
    left as they are. ``legacy_deleted`` is the number of rows deleted from
    the legacy ``embeddings`` table.
    """

    tables: Dict[str, Dict[str, int]] = field(default_factory=dict)
    legacy_deleted: int = 0

    @property
    def converted(self) -> int:
        return sum(c["converted"] for c in self.tables.values())

    @property
    def kept(self) -> int:
        return sum(c["kept"] for c in self.tables.values())

    @property
    def did_anything(self) -> bool:
        return bool(self.converted or self.kept or self.legacy_deleted)


def _json_vector_or_none(raw: Any) -> Optional[List[float]]:
    """The vector a JSON-format value holds, or None when it is not one vector.

    ``raw`` is the value read as bytes (``CAST(vector AS BLOB)``), so a text
    value that is not valid UTF-8 is left as it is instead of making the read
    raise. One vector is a non-empty flat list of numbers. Booleans are not
    numbers here (``json.loads`` gives ``True`` for ``true``).
    """
    try:
        text = bytes(raw).decode("utf-8")
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
    return parsed


_JSON_ROW_CONDITION = "typeof(vector) = 'text' AND vector != ?"

#: Exceptions that a stored value itself can cause while it is parsed or
#: encoded by :func:`convert_json_vectors`. Only these make a row "kept";
#: every other exception propagates.
_VALUE_ERRORS = (TypeError, ValueError, RecursionError, OverflowError)


def convert_json_vectors(
    conn: sqlite3.Connection,
    *,
    batch_size: int = CONVERSION_BATCH_SIZE,
    progress: Optional[ProgressCallback] = None,
    db_lock: Optional[Any] = None,
) -> VectorConversionResult:
    """Rewrite the JSON-format rows of the four vector tables in the byte format.

    docs/intent/vector_binary_storage.md §4. Run when a persona starts
    (saiverse_memory/adapter.py, ``convert_stored_vectors``) and at the start
    of every re-embed, before the targets are looked at.

    - Target rows: ``typeof(vector) = 'text'`` and not the empty vector row
      (``storage.DEFERRED_VECTOR``, which stays as it is).
    - A table with no target row is skipped after one read-only count, with
      no write lock taken.
    - Rows are processed ``batch_size`` at a time in rowid order; the next
      batch starts after the last rowid of the previous one, so rows that are
      left as they are are never read again. Each batch is read and written
      inside one ``immediate_transaction`` and committed on its own, so a
      stop half way keeps every committed batch, and the next run converts
      the rest. Each UPDATE also requires the row to still be in the JSON
      format.
    - A row is rewritten only when its value is one vector (a non-empty flat
      list of numbers), encoded with ``vector_codec.encode_vector``. Its
      dimension is not checked (an old dimension is the stale-vector check's
      business). Other rows (broken values, the legacy "list of vectors"
      format, values whose parse or encoding raises ``TypeError``,
      ``ValueError``, ``RecursionError`` or ``OverflowError``) are left as
      they are and counted in ``kept``. Any other exception raised while a
      row is processed (``MemoryError``, for one) propagates.
    - Only the ``vector`` column is written. ``embed_metadata`` (the model
      label, the in-progress mark) is neither read nor written.
    - When every table was processed without an exception, the legacy
      ``embeddings`` table is emptied (:func:`purge_stale_legacy_embeddings`
      with ``everything=True``). An exception propagates and leaves it as it is.

    ``progress(table, done, total)`` is called when a table with target rows
    starts (``done=0``) and after every committed batch; ``done`` counts the
    rows looked at (converted + kept), ``total`` the target rows counted at
    the start.
    """
    from sai_memory.memory.storage import DEFERRED_VECTOR
    from sai_memory.vector_codec import encode_vector

    batch_size = max(1, int(batch_size))
    result = VectorConversionResult()
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        counts = {"converted": 0, "kept": 0}
        result.tables[table] = counts
        total = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {_JSON_ROW_CONDITION}",
            (DEFERRED_VECTOR,),
        ).fetchone()[0]
        if not total:
            continue
        if progress is not None:
            progress(table, 0, total)
        last_rowid = None
        while True:
            with immediate_transaction(conn, db_lock):
                if last_rowid is None:
                    rows = conn.execute(
                        f"SELECT rowid, CAST(vector AS BLOB) FROM {table} "
                        f"WHERE {_JSON_ROW_CONDITION} ORDER BY rowid LIMIT ?",
                        (DEFERRED_VECTOR, batch_size),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT rowid, CAST(vector AS BLOB) FROM {table} "
                        f"WHERE rowid > ? AND {_JSON_ROW_CONDITION} "
                        "ORDER BY rowid LIMIT ?",
                        (last_rowid, DEFERRED_VECTOR, batch_size),
                    ).fetchall()
                updates = []
                for rowid, value in rows:
                    # A value can make the parse or the encoding raise (deeply
                    # nested JSON: RecursionError; an integer too large for
                    # float32: OverflowError; text that is not JSON or not
                    # UTF-8: ValueError; a value of an unexpected type:
                    # TypeError). Such a row is kept like any other row that
                    # is not one vector, so that it cannot stop every later
                    # run (and the re-embed, which runs this first). Any other
                    # exception (MemoryError included) is a failure of the run,
                    # not of the row's value: it propagates, the batch is
                    # rolled back, and the legacy table is left as it is.
                    try:
                        vector = _json_vector_or_none(value)
                        encoded = None if vector is None else encode_vector(vector)
                    except _VALUE_ERRORS:
                        encoded = None
                    if encoded is None:
                        counts["kept"] += 1
                    else:
                        updates.append((encoded, rowid))
                if updates:
                    conn.executemany(
                        f"UPDATE {table} SET vector = ? "
                        "WHERE rowid = ? AND typeof(vector) = 'text'",
                        updates,
                    )
                    counts["converted"] += len(updates)
            if not rows:
                break
            last_rowid = rows[-1][0]
            if progress is not None:
                progress(table, counts["converted"] + counts["kept"], total)
            if len(rows) < batch_size:
                break
    result.legacy_deleted = purge_stale_legacy_embeddings(
        conn, 0, everything=True, db_lock=db_lock,
    )
    return result


def highest_vector_dim(conn: sqlite3.Connection) -> int:
    """Largest stored vector dimension across the four vector tables (full scan).

    The CLI uses it to refuse an accidental re-embed to a smaller dimension.
    Only byte-format values count (``sai_memory.vector_codec.stored_vector_dim``);
    JSON-format rows, the empty vector row and unreadable values are ignored.
    """
    highest = 0
    for table in VECTOR_TABLES:
        if not _relation_exists(conn, table):
            continue
        for (stored,) in conn.execute(f"SELECT {_VEC} FROM {table}"):
            dim = stored_vector_dim(stored)
            if dim is not None and dim > highest:
                highest = dim
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
    for mid, stored in conn.execute(f"SELECT message_id, {_VEC} FROM message_embeddings"):
        embedded.add(mid)
        if is_stale_vector(stored, expected_dim):
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
    of its old text. Its current rows stay as they were; a re-embed passes
    the count to :func:`finish_reembed`, which then keeps the in-progress
    mark so the next run rebuilds it (for the background embedding of an
    import, the row left is the empty one, which stays stale). A target that gained text while being
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

    ``complete`` is True only when the memory.db holds no vector of another
    space and the job's mark was closed: no stale vector in any of the four
    tables (``stale_tables``), no message that kept its old vector because
    it was edited while being embedded (``skipped``), no empty vector row
    written after the scan (``deferred``, counted inside the closing
    transaction), and the in-progress mark not taken over by another
    re-embed (``other_job``).
    """

    stale_tables: List[str] = field(default_factory=list)
    skipped: int = 0
    other_job: bool = False
    deferred: int = 0

    @property
    def complete(self) -> bool:
        return (
            not self.stale_tables
            and self.skipped == 0
            and self.deferred == 0
            and not self.other_job
        )

    def describe(self) -> str:
        """One line naming why the job is not complete ("" when it is)."""
        parts = []
        if self.stale_tables:
            parts.append(f"Stale vectors remain in: {', '.join(self.stale_tables)}.")
        if self.skipped:
            parts.append(f"Skipped {self.skipped} messages edited during the re-embed.")
        if self.deferred:
            parts.append(
                f"{self.deferred} messages written during the re-embed still wait "
                "for their embeddings."
            )
        if self.other_job:
            parts.append("Another re-embed started during this one; its mark is kept.")
        return " ".join(parts)


def finish_reembed(
    conn: sqlite3.Connection,
    embedder: Any,
    model_id: str,
    *,
    token: Optional[str],
    force: bool = False,
    skipped: int = 0,
    db_lock: Optional[Any] = None,
) -> ReembedOutcome:
    """Close the job when no vector of another space is left.

    ``force=True`` (including the escalations: changed model label, leftover
    in-progress mark) first empties the legacy ``embeddings`` table; see
    :func:`purge_stale_legacy_embeddings`.

    The job is complete when (:class:`ReembedOutcome`):

    - a check of every row (:func:`scan_stale_vector_tables`) finds no stale
      vector in the four tables (the empty row of a deferred embedding is
      stale too);
    - ``skipped``, the messages the job left with their old vector because
      their text was edited while being embedded (``changed`` of
      :func:`reembed_messages`), is zero. After a ``force`` rebuild those old
      vectors may have the current length, so only this count tells they are
      there;
    - no message holds an empty vector row (``storage.DEFERRED_VECTOR``),
      counted inside the write transaction (``deferred``);
    - the in-progress mark is still the one this job wrote (``token`` from
      :func:`begin_reembed`), checked inside the write transaction.

    The full scan runs outside the write transaction, so it can miss rows
    written after it. A non-empty vector written then comes from the
    current model at the current dimension, so missing it is harmless; the
    only row that breaks completion is an empty one (a message stored or
    edited without its vectors), and that is what the count inside the
    lock is for (it uses the partial index, so it is instant).

    Rows without a vector (Chronicle / page / fragment rows the generators
    skipped because of an edit, rows written after the job's pass) do not
    hold completion back: the next Metabolism embeds them with the current
    model.

    When complete, the model label is recorded and the mark removed in one
    ``BEGIN IMMEDIATE`` transaction. When the mark carries another job's
    token, neither is touched and ``other_job`` is set (that job's own finish
    closes the memory.db; if it died, its mark makes the next run rebuild in
    full). A mark that is already gone counts as closed. When not complete,
    neither the label nor the mark is touched. Only the keys named here are
    written (the table also holds other subsystems' markers).
    """
    from sai_memory.memory.storage import (
        count_deferred_message_embeddings,
        get_embed_metadata,
        set_embed_metadata,
    )

    expected_dim = int(embedder.embedding_size)
    purge_stale_legacy_embeddings(conn, expected_dim, everything=force)
    # The scan runs before the transaction: a full scan holds no write lock.
    # Non-empty rows written after it come from the current model and are
    # not stale; empty rows written after it are counted inside the lock.
    outcome = ReembedOutcome(
        stale_tables=scan_stale_vector_tables(conn, expected_dim),
        skipped=int(skipped),
    )
    if not outcome.complete:
        return outcome
    with immediate_transaction(conn, db_lock):
        outcome.deferred = count_deferred_message_embeddings(conn)
        if outcome.deferred:
            return outcome
        mark = get_embed_metadata(conn, MIGRATION_IN_PROGRESS_KEY)
        if mark is not None and _mark_token(mark) != token:
            outcome.other_job = True
            return outcome
        set_embed_metadata(conn, EMBED_MODEL_KEY, model_id, commit=False)
        conn.execute("DELETE FROM embed_metadata WHERE key = ?", (MIGRATION_IN_PROGRESS_KEY,))
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
            for row_id, stored in conn.execute(f"SELECT {id_col}, {_VEC} FROM {table}")
            if is_stale_vector(stored, expected_dim)
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
