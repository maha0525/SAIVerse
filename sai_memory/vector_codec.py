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


@dataclass
class StoredVectorRows:
    """Rows read by :func:`read_vector_rows`.

    ``rows[i]`` holds the other columns of the row whose vector is
    ``matrix[i]``, in cursor order. Rows that are not part of the matrix are
    only counted (or, for ``mismatched``, listed by their other columns).
    """

    rows: List[Tuple[Any, ...]]
    matrix: np.ndarray  # float32, shape (len(rows), dim)
    # (other columns, stored dim) of byte-format rows whose dim is not ``dim``.
    mismatched: List[Tuple[Tuple[Any, ...], int]] = field(default_factory=list)
    # Other columns of the rows holding the empty vector row of a deferred
    # embedding ('[]').
    deferred: List[Tuple[Any, ...]] = field(default_factory=list)
    # Rows whose value is neither the byte format nor '[]' (JSON-format rows,
    # broken values).
    unreadable: int = 0

    def __len__(self) -> int:
        return len(self.rows)


def read_vector_rows(cursor: Iterable[Sequence[Any]], *, dim: int) -> StoredVectorRows:
    """Read rows ``(vector, *other columns)`` one at a time into a float32 matrix.

    Only byte-format rows of dimension ``dim`` enter the matrix. The cursor
    is consumed row by row; no list of every row's stored value is built.
    """
    # storage imports this module at load time, so its constant is read here.
    from sai_memory.memory.storage import DEFERRED_VECTOR

    dim = int(dim)
    capacity = 1024
    buf = np.empty((capacity, dim), dtype=np.float32)
    rows: List[Tuple[Any, ...]] = []
    mismatched: List[Tuple[Tuple[Any, ...], int]] = []
    deferred: List[Tuple[Any, ...]] = []
    unreadable = 0
    n = 0
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
        if n == capacity:
            capacity *= 2
            grown = np.empty((capacity, dim), dtype=np.float32)
            grown[:n] = buf[:n]
            buf = grown
        buf[n] = np.frombuffer(_as_bytes(value), dtype=_DTYPE)
        rows.append(other)
        n += 1
    return StoredVectorRows(
        rows=rows,
        matrix=buf[:n],
        mismatched=mismatched,
        deferred=deferred,
        unreadable=unreadable,
    )


def cosine_scores(matrix: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Cosine similarity of every row of ``matrix`` with ``query`` (float32).

    A row or query whose norm is 0 scores 0.0, as the per-row computation did.
    """
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    if matrix.shape[0] == 0:
        return np.zeros(0, dtype=np.float32)
    dots = matrix @ q
    denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(q)
    out = np.zeros(matrix.shape[0], dtype=np.float32)
    nonzero = denom != 0
    out[nonzero] = dots[nonzero] / denom[nonzero]
    return out
