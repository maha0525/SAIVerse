"""The one place that knows how recall vectors are stored in ``memory.db``.

docs/intent/vector_binary_storage.md §1-§2. The ``vector`` column of the four
vector tables (``message_embeddings``, ``arasuji_embeddings``,
``memopedia_embeddings``, ``memopedia_fragment_embeddings``) holds a vector in
the byte format: float32 little-endian values, back to back (768 dims =
3,072 bytes). The column is declared ``TEXT``, but SQLite keeps a type per
value, so the bytes are stored as a ``BLOB``.

Other values can still be found in the column:

- the empty vector row of a deferred embedding (``storage.DEFERRED_VECTOR``,
  the string ``'[]'``), kept as a string on purpose;
- rows still in the JSON format (written before this change);
- broken values.

None of these is a vector to compare with: :func:`decode_vector` returns
``None`` for them and :func:`stored_vector_dim` returns ``None``. Nothing in
this module raises on a stored value.

Code elsewhere must not build or parse stored vector values itself; it calls
the functions here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Tuple

import numpy as np

_DTYPE = np.dtype("<f4")
_ITEM_SIZE = _DTYPE.itemsize  # 4


#: SQL expression that reads the ``vector`` column for code that only needs
#: :func:`stored_vector_dim` (the stale-vector checks). A text value is never a
#: byte-format vector, so it is read as ``NULL``: the result of
#: :func:`stored_vector_dim` is the same (``None``), and a text value that is
#: not valid UTF-8 cannot make the read raise (Python decodes TEXT values on
#: fetch). Code that must tell the empty vector row apart reads the column
#: itself.
VECTOR_FOR_DIM_SQL = "CASE WHEN typeof(vector) = 'text' THEN NULL ELSE vector END"


def vector_read_sql(column: str) -> str:
    """SQL expression that reads the vector column ``column`` for search.

    Unlike :data:`VECTOR_FOR_DIM_SQL`, the search must tell the empty vector
    row of a deferred embedding (``storage.DEFERRED_VECTOR``, ``'[]'``) apart
    (:func:`score_vector_rows` counts it in ``deferred``), so that one text
    value is kept. Every other text value (JSON-format rows, text that is not
    valid UTF-8) is read as ``NULL``, which :func:`score_vector_rows` counts in
    ``unreadable``: Python decodes TEXT values on fetch, and one value that is
    not valid UTF-8 would otherwise make the whole read raise. Byte-format
    values are read as they are.

    ``column`` is the column as written in the query (``"vector"``,
    ``"e.vector"``). The ``'[]'`` below must stay equal to
    ``storage.DEFERRED_VECTOR`` (checked by a test).
    """
    return (
        f"CASE WHEN typeof({column}) = 'text' AND {column} != '[]' "
        f"THEN NULL ELSE {column} END"
    )


def encode_vector(vector: Any) -> bytes:
    """Return the stored value (byte format) of ``vector`` (list / tuple / ndarray)."""
    return np.asarray(vector, dtype=_DTYPE).reshape(-1).tobytes()


def _as_bytes(value: Any) -> Optional[bytes]:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    return None


def stored_vector_dim(value: Any) -> Optional[int]:
    """Dimension of a byte-format stored value; ``None`` for any other value.

    ``None`` covers the empty vector row (``'[]'``), JSON-format rows,
    ``None``, and byte values whose length is 0 or not a multiple of 4.
    """
    if isinstance(value, (bytes, bytearray)):
        n = len(value)
    elif isinstance(value, memoryview):
        n = value.nbytes
    else:
        return None
    if n == 0 or n % _ITEM_SIZE:
        return None
    return n // _ITEM_SIZE


def decode_vector(value: Any) -> Optional[np.ndarray]:
    """Turn a stored value back into a 1-D float32 array; ``None`` when it is not one."""
    if stored_vector_dim(value) is None:
        return None
    raw = _as_bytes(value)
    if raw is None:
        return None
    return np.frombuffer(raw, dtype=_DTYPE).astype(np.float32, copy=True)


#: Default number of rows put into one block by :func:`score_vector_rows`
#: (768 dims: 4,096 rows = about 12.6 MB).
DEFAULT_BLOCK_ROWS = 4096


@dataclass
class ScoredVectorRows:
    """Rows read by :func:`score_vector_rows`.

    ``rows[i]`` holds the other columns of the row whose cosine similarity
    with the query is ``scores[i]``, in cursor order. Rows that are not
    scored are only counted (or, for ``mismatched``, listed by their other
    columns).
    """

    rows: List[Tuple[Any, ...]]
    scores: np.ndarray  # float32, shape (len(rows),)
    # (other columns, stored dim) of byte-format rows whose dim is not the
    # query's.
    mismatched: List[Tuple[Tuple[Any, ...], int]] = field(default_factory=list)
    # Other columns of the rows holding the empty vector row of a deferred
    # embedding ('[]').
    deferred: List[Tuple[Any, ...]] = field(default_factory=list)
    # Rows whose value is neither the byte format nor '[]' (JSON-format rows,
    # broken values).
    unreadable: int = 0

    def __len__(self) -> int:
        return len(self.rows)


def score_vector_rows(
    cursor: Iterable[Sequence[Any]],
    query: Any,
    *,
    block_rows: int = DEFAULT_BLOCK_ROWS,
) -> ScoredVectorRows:
    """Read rows ``(vector, *other columns)`` and score each against ``query``.

    Only byte-format rows whose dimension is the length of ``query`` are
    scored (:func:`cosine_scores`). The cursor is consumed row by row; the
    vectors are put into one float32 array of ``block_rows`` rows, which is
    scored and reused for the next rows when it is full. So the vectors of
    every row are never held at once: besides that one array, only the other
    columns and one float32 score per row are kept, whatever the number of
    rows.
    """
    # storage imports this module at load time, so its constant is read here.
    from sai_memory.memory.storage import DEFERRED_VECTOR

    q = np.asarray(query, dtype=np.float32).reshape(-1)
    dim = int(q.shape[0])
    block_rows = max(1, int(block_rows))
    block = np.empty((block_rows, dim), dtype=np.float32)
    score_parts: List[np.ndarray] = []
    rows: List[Tuple[Any, ...]] = []
    mismatched: List[Tuple[Tuple[Any, ...], int]] = []
    deferred: List[Tuple[Any, ...]] = []
    unreadable = 0
    n = 0  # rows in ``block``
    for row in cursor:
        value = row[0]
        other = tuple(row[1:])
        stored_dim = stored_vector_dim(value)
        if stored_dim is None:
            if isinstance(value, str) and value == DEFERRED_VECTOR:
                deferred.append(other)
            else:
                unreadable += 1
            continue
        if stored_dim != dim:
            mismatched.append((other, stored_dim))
            continue
        block[n] = np.frombuffer(_as_bytes(value), dtype=_DTYPE)
        rows.append(other)
        n += 1
        if n == block_rows:
            score_parts.append(cosine_scores(block, q))
            n = 0
    if n:
        score_parts.append(cosine_scores(block[:n], q))
    scores = (
        np.concatenate(score_parts) if score_parts else np.zeros(0, dtype=np.float32)
    )
    return ScoredVectorRows(
        rows=rows,
        scores=scores,
        mismatched=mismatched,
        deferred=deferred,
        unreadable=unreadable,
    )


def cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine similarity of every row of ``matrix`` with ``query`` (float32).

    Each row is scored on its own, with the operations the search used before
    the byte format (``np.dot`` and ``np.linalg.norm`` on two 1-D float32
    arrays). So a row's score is the value that computation gave, and it does
    not depend on the other rows or on where the row sits in the matrix: a
    matrix product over all rows rounds differently (differences around 1e-8),
    which is enough to swap two nearly tied memories.

    A row or query whose norm is 0 scores 0.0.
    """
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    out = np.zeros(matrix.shape[0], dtype=np.float32)
    q_norm = np.linalg.norm(q)
    for i in range(matrix.shape[0]):
        row = matrix[i]
        denom = float(q_norm * np.linalg.norm(row))
        if denom == 0:
            continue
        out[i] = float(np.dot(q, row) / denom)
    return out
