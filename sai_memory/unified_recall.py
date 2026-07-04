"""Unified recall: embedding-based search across Chronicle, Memopedia, and Fragments.

Provides:
- Embedding generation and storage for Chronicle Lv1, Memopedia pages, and Fragments
- Unified search that returns ranked results from all sources
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

LOGGER = logging.getLogger(__name__)


@dataclass
class RecallHit:
    """A single hit from unified recall search."""
    source_type: str  # "chronicle", "memopedia", or "fragment"
    source_id: str    # entry_id, page_id, or fragment_id
    title: str        # Chronicle: time range, Memopedia: page title, Fragment: entity title
    content: str      # Chronicle: summary text, Memopedia: summary, Fragment: fragment text
    score: float      # final fused (RRF) score after ranking; NOT raw cosine similarity
    uri: str          # saiverse:// URI for navigation
    # Raw cosine similarity from the embedding search (before RRF fusion overwrites
    # ``score``). ``None`` for hits that surfaced via keyword match only (no embedding
    # comparison was performed). Auto-recall (sea/auto_recall.py) needs this to apply a
    # cosine threshold — the fused ``score`` is on a ~1/60 RRF scale and cannot be
    # thresholded against a 0.78 cutoff.
    embed_score: Optional[float] = None
    # Extra metadata
    level: Optional[int] = None       # Chronicle level
    category: Optional[str] = None    # Memopedia category
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    message_count: Optional[int] = None
    entity_id: Optional[str] = None             # Fragment: parent entity page ID
    chronicle_entry_id: Optional[str] = None    # Fragment: linked Chronicle entry
    source_date: Optional[str] = None           # Fragment: extraction date (YYYY-MM-DD)
    # message ヒットのみ: 表示 content がどのチャンクから抜粋されたか (rev4)。
    # None は「先頭200字フォールバック」(整合性ガード落ち、または非 message ヒット)。
    chunk_index: Optional[int] = None


# ---------------------------------------------------------------------------
# Embedding storage: Chronicle
# ---------------------------------------------------------------------------

def store_chronicle_embedding(
    conn: sqlite3.Connection,
    entry_id: str,
    vector: List[float],
) -> None:
    """Store or replace an embedding for a Chronicle entry."""
    conn.execute(
        "INSERT OR REPLACE INTO arasuji_embeddings (entry_id, vector) VALUES (?, ?)",
        (entry_id, json.dumps(vector)),
    )
    conn.commit()


def get_chronicle_embeddings(
    conn: sqlite3.Connection,
    level: Optional[int] = None,
) -> List[tuple]:
    """Get all Chronicle embeddings, optionally filtered by level.

    Returns:
        List of (entry_id, vector, level, content, start_time, end_time, message_count)
    """
    if level is not None:
        cur = conn.execute(
            """
            SELECT e.entry_id, e.vector, a.level, a.content, a.start_time, a.end_time, a.message_count
            FROM arasuji_embeddings e
            JOIN arasuji_entries a ON e.entry_id = a.id
            WHERE a.level = ?
            """,
            (level,),
        )
    else:
        cur = conn.execute(
            """
            SELECT e.entry_id, e.vector, a.level, a.content, a.start_time, a.end_time, a.message_count
            FROM arasuji_embeddings e
            JOIN arasuji_entries a ON e.entry_id = a.id
            """,
        )
    result = []
    for row in cur.fetchall():
        try:
            vec = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        result.append((row[0], vec, row[2], row[3], row[4], row[5], row[6]))
    return result


def count_chronicle_embeddings(conn: sqlite3.Connection) -> int:
    """Count Chronicle entries that have embeddings."""
    cur = conn.execute("SELECT COUNT(*) FROM arasuji_embeddings")
    return cur.fetchone()[0]


def get_chronicle_entries_without_embeddings(
    conn: sqlite3.Connection,
    level: int = 1,
) -> List[tuple]:
    """Get Chronicle entries at given level that don't have embeddings yet.

    Returns:
        List of (entry_id, content)
    """
    cur = conn.execute(
        """
        SELECT a.id, a.content
        FROM arasuji_entries a
        LEFT JOIN arasuji_embeddings e ON a.id = e.entry_id
        WHERE a.level = ? AND e.entry_id IS NULL
        """,
        (level,),
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Embedding storage: Memopedia
# ---------------------------------------------------------------------------

def store_memopedia_embedding(
    conn: sqlite3.Connection,
    page_id: str,
    vector: List[float],
) -> None:
    """Store or replace an embedding for a Memopedia page."""
    conn.execute(
        "INSERT OR REPLACE INTO memopedia_embeddings (page_id, vector) VALUES (?, ?)",
        (page_id, json.dumps(vector)),
    )
    conn.commit()


def get_memopedia_embeddings(conn: sqlite3.Connection) -> List[tuple]:
    """Get all Memopedia embeddings.

    Returns:
        List of (page_id, vector, title, summary, category)
    """
    cur = conn.execute(
        """
        SELECT e.page_id, e.vector, p.title, p.summary, p.category
        FROM memopedia_embeddings e
        JOIN memopedia_pages p ON e.page_id = p.id
        WHERE (p.is_deleted = 0 OR p.is_deleted IS NULL)
        """,
    )
    result = []
    for row in cur.fetchall():
        try:
            vec = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        result.append((row[0], vec, row[2], row[3], row[4]))
    return result


def count_memopedia_embeddings(conn: sqlite3.Connection) -> int:
    """Count Memopedia pages that have embeddings."""
    cur = conn.execute("SELECT COUNT(*) FROM memopedia_embeddings")
    return cur.fetchone()[0]


def get_memopedia_pages_without_embeddings(conn: sqlite3.Connection) -> List[tuple]:
    """Get Memopedia pages that don't have embeddings yet (excluding root/trunk pages).

    Returns:
        List of (page_id, title, summary)
    """
    cur = conn.execute(
        """
        SELECT p.id, p.title, p.summary
        FROM memopedia_pages p
        LEFT JOIN memopedia_embeddings e ON p.id = e.page_id
        WHERE e.page_id IS NULL
          AND p.id NOT LIKE 'root_%'
          AND p.is_trunk = 0
        """,
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Embedding storage: Memopedia Fragments
# ---------------------------------------------------------------------------

def store_fragment_embedding(
    conn: sqlite3.Connection,
    fragment_id: str,
    vector: List[float],
) -> None:
    """Store or replace an embedding for a Memopedia fragment."""
    conn.execute(
        "INSERT OR REPLACE INTO memopedia_fragment_embeddings (fragment_id, vector) VALUES (?, ?)",
        (fragment_id, json.dumps(vector)),
    )
    conn.commit()


def get_fragment_embeddings(conn: sqlite3.Connection) -> List[tuple]:
    """Get all fragment embeddings with metadata.

    Returns:
        List of (fragment_id, vector, content, entity_id, chronicle_entry_id,
                 source_date, entity_title, entity_category)
    """
    cur = conn.execute(
        """
        SELECT e.fragment_id, e.vector, f.content, f.entity_id,
               f.chronicle_entry_id, f.source_date, p.title, p.category
        FROM memopedia_fragment_embeddings e
        JOIN memopedia_fragments f ON e.fragment_id = f.id
        LEFT JOIN memopedia_pages p ON f.entity_id = p.id
        """,
    )
    result = []
    for row in cur.fetchall():
        try:
            vec = json.loads(row[1])
        except (json.JSONDecodeError, TypeError):
            continue
        result.append((row[0], vec, row[2], row[3], row[4], row[5], row[6], row[7]))
    return result


def count_fragment_embeddings(conn: sqlite3.Connection) -> int:
    """Count Memopedia fragments that have embeddings."""
    cur = conn.execute("SELECT COUNT(*) FROM memopedia_fragment_embeddings")
    return cur.fetchone()[0]


def get_fragments_without_embeddings(conn: sqlite3.Connection) -> List[tuple]:
    """Get Memopedia fragments that don't have embeddings yet.

    Returns:
        List of (fragment_id, content, entity_title)
    """
    cur = conn.execute(
        """
        SELECT f.id, f.content, p.title
        FROM memopedia_fragments f
        LEFT JOIN memopedia_fragment_embeddings e ON f.id = e.fragment_id
        LEFT JOIN memopedia_pages p ON f.entity_id = p.id
        WHERE e.fragment_id IS NULL
        """,
    )
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Embedding retrieval: Messages (read-only, embeddings created elsewhere)
# ---------------------------------------------------------------------------

def get_message_embeddings(conn: sqlite3.Connection) -> List[tuple]:
    """Get all message embeddings for unified search (excluding system messages).

    Returns best-scoring chunk per message later; here we return all chunks.

    Returns:
        List of (message_id, vector, chunk_index, content, role, created_at)
    """
    cur = conn.execute(
        """
        SELECT e.message_id, e.vector, e.chunk_index, m.content, m.role, m.created_at
        FROM message_embeddings e
        JOIN messages m ON e.message_id = m.id
        WHERE m.role != 'system'
        """,
    )
    result = []
    for row in cur.fetchall():
        try:
            vec = json.loads(row[1])
            if isinstance(vec, list) and vec and isinstance(vec[0], list):
                for idx, entry in enumerate(vec):
                    result.append((row[0], [float(v) for v in entry], idx, row[3], row[4], row[5]))
            else:
                result.append((row[0], [float(v) for v in vec], int(row[2]), row[3], row[4], row[5]))
        except (json.JSONDecodeError, TypeError):
            continue
    return result


# ---------------------------------------------------------------------------
# Batch embedding generation
# ---------------------------------------------------------------------------

def embed_chronicle_entries(
    conn: sqlite3.Connection,
    embedder,
    *,
    level: int = 1,
    batch_size: int = 64,
) -> int:
    """Generate and store embeddings for Chronicle entries that don't have them.

    Args:
        conn: Database connection.
        embedder: Embedder instance with embed() method.
        level: Chronicle level to embed (default: 1 = あらすじ).
        batch_size: Batch size for embedding generation.

    Returns:
        Number of entries embedded.
    """
    entries = get_chronicle_entries_without_embeddings(conn, level=level)
    if not entries:
        LOGGER.info("No Chronicle Lv%d entries need embedding", level)
        return 0

    LOGGER.info("Embedding %d Chronicle Lv%d entries", len(entries), level)
    total = 0

    for i in range(0, len(entries), batch_size):
        batch = entries[i:i + batch_size]
        texts = [content for _, content in batch]
        vectors = embedder.embed(texts, is_query=False)

        for (entry_id, _), vec in zip(batch, vectors):
            store_chronicle_embedding(conn, entry_id, list(vec))
            total += 1

    LOGGER.info("Embedded %d Chronicle entries", total)
    return total


def embed_memopedia_pages(
    conn: sqlite3.Connection,
    embedder,
    *,
    batch_size: int = 64,
) -> int:
    """Generate and store embeddings for Memopedia pages that don't have them.

    Embeds "title: {title}. {summary}" for each page.

    Args:
        conn: Database connection.
        embedder: Embedder instance with embed() method.
        batch_size: Batch size for embedding generation.

    Returns:
        Number of pages embedded.
    """
    pages = get_memopedia_pages_without_embeddings(conn)
    if not pages:
        LOGGER.info("No Memopedia pages need embedding")
        return 0

    LOGGER.info("Embedding %d Memopedia pages", len(pages))
    total = 0

    for i in range(0, len(pages), batch_size):
        batch = pages[i:i + batch_size]
        texts = [f"{title}: {summary}" if summary else title
                 for _, title, summary in batch]
        vectors = embedder.embed(texts, is_query=False)

        for (page_id, _, _), vec in zip(batch, vectors):
            store_memopedia_embedding(conn, page_id, list(vec))
            total += 1

    LOGGER.info("Embedded %d Memopedia pages", total)
    return total


def embed_memopedia_fragments(
    conn: sqlite3.Connection,
    embedder,
    *,
    batch_size: int = 64,
) -> int:
    """Generate and store embeddings for Memopedia fragments that don't have them.

    Args:
        conn: Database connection.
        embedder: Embedder instance with embed() method.
        batch_size: Batch size for embedding generation.

    Returns:
        Number of fragments embedded.
    """
    fragments = get_fragments_without_embeddings(conn)
    if not fragments:
        LOGGER.info("No Memopedia fragments need embedding")
        return 0

    LOGGER.info("Embedding %d Memopedia fragments", len(fragments))
    total = 0

    for i in range(0, len(fragments), batch_size):
        batch = fragments[i:i + batch_size]
        # entity_title は親ページ消失 (孤児 Fragment) で None になりうる。
        # 孤児でも Fragment 本文単体で検索可能にする (LEFT JOIN 化, 2026-07-04)。
        texts = [
            f"{entity_title}: {content}" if entity_title else content
            for _, content, entity_title in batch
        ]
        vectors = embedder.embed(texts, is_query=False)

        for (frag_id, _, _), vec in zip(batch, vectors):
            store_fragment_embedding(conn, frag_id, list(vec))
            total += 1

    LOGGER.info("Embedded %d Memopedia fragments", total)
    return total


# ---------------------------------------------------------------------------
# Unified search
# ---------------------------------------------------------------------------

def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


# ---------------------------------------------------------------------------
# マッチ箇所抜粋 (rev4)
#
# メッセージ本文を先頭から機械的に切るのをやめ、実際にマッチしたチャンク/位置の
# 周辺だけを抜粋する。書き込み時 (saiverse_memory/adapter.py) の chunk_text() 呼び
# 出しと同じ min_chars/max_chars で再分割し、chunk_index で引く。パラメータが過去
# に変わっていた等でチャンク数が食い違う場合は位置がズレるため、整合性ガードで
# 検出しフォールバックする。
# ---------------------------------------------------------------------------

_EXCERPT_TRIM = 200  # 従来の先頭トリム長と揃える
_KEYWORD_WINDOW = 100  # キーワードマッチ抜粋の前後窓幅


def _get_chunk_params() -> tuple[int, int]:
    """メッセージ書き込み時と同じチャンク分割パラメータを env から読む。

    sai_memory.config.load_settings() はファイルシステム探索や無関係な設定
    (LLM provider など) まで読み込み重いので、ここでは同じ2つの env 変数を
    直接読む軽量版にする (ロジックは sai_memory/config.py の _get_int と同一)。
    """
    def _get_int(name: str, default: int) -> int:
        v = __import__("os").environ.get(name)
        try:
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    min_chars = _get_int("SAIMEMORY_MEMORY_CHUNK_MIN_CHARS", 120)
    max_chars = _get_int("SAIMEMORY_MEMORY_CHUNK_MAX_CHARS", 480)
    if min_chars < 0:
        min_chars = 0
    if max_chars <= 0:
        max_chars = 1
    if min_chars > max_chars:
        min_chars = max_chars
    return min_chars, max_chars


def _count_message_embedding_rows(conn: sqlite3.Connection, message_id: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM message_embeddings WHERE message_id = ?",
        (message_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def reconstruct_message_excerpt(
    conn: sqlite3.Connection,
    message_id: str,
    chunk_index: int,
    full_content: str,
) -> tuple[str, Optional[int]]:
    """メッセージ本文をベストマッチ chunk_index 周辺で抜粋する (200字トリム済み)。

    書き込み時と同じパラメータで chunk_text() を再実行し、再分割したチャンク数と
    その message_id の embedding 行数が一致する場合のみ chunk_index で引く
    (整合性ガード: 過去にチャンク分割パラメータが変わっていた等で位置がズレる
    可能性があるため)。不一致・チャンク取得失敗時は先頭200字にフォールバックする。

    Returns:
        (抜粋テキスト, 実際に使った chunk_index。フォールバック時は None)
    """
    chunk, used_chunk_index = reconstruct_message_chunk(conn, message_id, chunk_index, full_content)
    if used_chunk_index is None:
        # フォールバック: reconstruct_message_chunk は full_content をそのまま返す。
        return chunk[:_EXCERPT_TRIM], None
    excerpt = chunk[:_EXCERPT_TRIM] + ("…" if len(chunk) > _EXCERPT_TRIM else "")
    return excerpt, used_chunk_index


def reconstruct_message_chunk(
    conn: sqlite3.Connection,
    message_id: str,
    chunk_index: int,
    full_content: str,
) -> tuple[str, Optional[int]]:
    """メッセージ本文からベストマッチ chunk_index のチャンク全文 (トリムなし) を復元する。

    reconstruct_message_excerpt() の下請け。呼び出し側がチャンク全文に対して独自の
    ウィンドウ処理 (例: auto_recall のタイトル出現位置 ±60字トリム) をしたい場合は
    こちらを直接使う。整合性ガードのロジックは reconstruct_message_excerpt と同一。

    Returns:
        (チャンク全文 or full_content, 実際に使った chunk_index。フォールバック時は
        (full_content, None))。
    """
    if not full_content:
        return "", None

    try:
        from sai_memory.memory.chunking import chunk_text
        min_chars, max_chars = _get_chunk_params()
        chunks = chunk_text(full_content.strip(), min_chars=min_chars, max_chars=max_chars)
        chunks = [c for c in chunks if c and c.strip()]
        expected_rows = _count_message_embedding_rows(conn, message_id)
        if not chunks or len(chunks) != expected_rows:
            LOGGER.debug(
                "[unified_recall] chunk reconstruction mismatch for message_id=%s: "
                "re-chunked=%d rows=%d; falling back to full content",
                message_id, len(chunks), expected_rows,
            )
            return full_content, None
        if chunk_index < 0 or chunk_index >= len(chunks):
            LOGGER.debug(
                "[unified_recall] chunk_index %d out of range (%d chunks) for message_id=%s; "
                "falling back to full content",
                chunk_index, len(chunks), message_id,
            )
            return full_content, None
        return chunks[chunk_index].strip(), chunk_index
    except Exception:
        LOGGER.debug(
            "[unified_recall] chunk reconstruction raised for message_id=%s; falling back",
            message_id, exc_info=True,
        )
        return full_content, None


def _keyword_excerpt(content: str, keywords: List[str]) -> str:
    """最初にマッチしたキーワードの出現位置を中心に前後 ±100 字を抜粋する。

    出現が見つからなければ先頭200字にフォールバックする。
    """
    if not content:
        return ""
    lowered = content.lower()
    best_pos = -1
    for kw in keywords:
        kw_stripped = kw.strip()
        if not kw_stripped:
            continue
        pos = lowered.find(kw_stripped.lower())
        if pos != -1:
            best_pos = pos
            break
    if best_pos == -1:
        return content[:_EXCERPT_TRIM]
    start = max(0, best_pos - _KEYWORD_WINDOW)
    end = min(len(content), best_pos + _KEYWORD_WINDOW)
    excerpt = content[start:end].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{excerpt}{suffix}"


# Per-source base allocation weights.
# These reflect the inherent characteristics of each source type:
# - fragment: short texts, many available, high precision → most slots
# - chronicle: medium-length summaries, moderate count → decent allocation
# - memopedia: few pages, broader summaries → minimal to avoid irrelevant fills
# - message: variable length, already covered by Chronicle → minimal
SOURCE_ALLOCATIONS = {
    "chronicle": 3,
    "memopedia": 1,
    "fragment": 5,
    "message": 1,
}

FOCUS_MULTIPLIER = 4


def _format_timestamp(ts: Optional[int]) -> str:
    if not ts:
        return "?"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _format_time_range(start: Optional[int], end: Optional[int]) -> str:
    from datetime import datetime
    parts = []
    if start:
        parts.append(datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M"))
    if end:
        parts.append(datetime.fromtimestamp(end).strftime("%Y-%m-%d %H:%M"))
    return " ~ ".join(parts) if parts else "?"


def unified_recall(
    conn: sqlite3.Connection,
    embedder,
    query: str,
    *,
    topk: Optional[int] = None,
    focus: Optional[str] = None,
    search_chronicle: bool = True,
    search_memopedia: bool = True,
    search_fragments: bool = True,
    search_messages: bool = True,
    chronicle_level: int = 1,
    persona_id: Optional[str] = None,
) -> List[RecallHit]:
    """Search across Chronicle, Memopedia, Fragments, and Messages using hybrid search.

    Per-source allocation determines how many results each source type contributes,
    based on SOURCE_ALLOCATIONS weights. The ``focus`` parameter gives one source
    type FOCUS_MULTIPLIER× its normal allocation for deeper search.

    Args:
        conn: Database connection (memory.db with arasuji + memopedia tables).
        embedder: Embedder instance.
        query: Search query text.
        topk: Hard cap on total results (None = use sum of allocations).
        focus: Source type to give FOCUS_MULTIPLIER× allocation (e.g., "fragment").
        search_chronicle: Include Chronicle entries in search.
        search_memopedia: Include Memopedia pages in search.
        search_fragments: Include Memopedia fragments in search.
        search_messages: Include raw chat messages in search (default: off).
        chronicle_level: Chronicle level to search (default: 1).
        persona_id: Persona ID for URI generation.

    Returns:
        List of RecallHit sorted by fused score descending.
    """
    # --- Compute per-source allocations ---
    source_caps: dict[str, int] = {}
    if search_chronicle:
        base = SOURCE_ALLOCATIONS.get("chronicle", 3)
        source_caps["chronicle"] = base * (FOCUS_MULTIPLIER if focus == "chronicle" else 1)
    if search_memopedia:
        base = SOURCE_ALLOCATIONS.get("memopedia", 1)
        source_caps["memopedia"] = base * (FOCUS_MULTIPLIER if focus == "memopedia" else 1)
    if search_fragments:
        base = SOURCE_ALLOCATIONS.get("fragment", 5)
        source_caps["fragment"] = base * (FOCUS_MULTIPLIER if focus == "fragment" else 1)
    if search_messages:
        base = SOURCE_ALLOCATIONS.get("message", 1)
        source_caps["message"] = base * (FOCUS_MULTIPLIER if focus == "message" else 1)

    total_cap = topk if topk is not None else sum(source_caps.values())

    # --- Keyword search ---
    # Search per-keyword and count matches per entry, avoiding OR+limit issues.
    keyword_hits: dict[str, RecallHit] = {}  # source_id → hit
    keyword_match_count: dict[str, int] = {}  # source_id → number of keywords matched

    query_keywords = query.split()

    if search_chronicle:
        # For each keyword, find matching Chronicle entry IDs
        kw_id_sets: list[set[str]] = []
        for kw in query_keywords:
            cur = conn.execute(
                "SELECT id FROM arasuji_entries WHERE content LIKE ? AND level = ?",
                (f"%{kw}%", chronicle_level),
            )
            kw_id_sets.append({row[0] for row in cur.fetchall()})

        # Collect all matched IDs and count per-entry matches
        all_chronicle_ids: set[str] = set()
        for ids in kw_id_sets:
            all_chronicle_ids |= ids

        for entry_id in all_chronicle_ids:
            count = sum(1 for ids in kw_id_sets if entry_id in ids)
            keyword_match_count[entry_id] = count

        # Fetch entry details only for matched IDs (sorted by match count desc)
        if all_chronicle_ids:
            from sai_memory.arasuji.storage import get_entry
            for entry_id in all_chronicle_ids:
                entry = get_entry(conn, entry_id)
                if entry:
                    time_range = _format_time_range(entry.start_time, entry.end_time)
                    keyword_hits[entry.id] = RecallHit(
                        source_type="chronicle",
                        source_id=entry.id,
                        title=f"Chronicle Lv{entry.level}: {time_range}",
                        content=entry.content[:200] if entry.content else "",
                        score=0.0,
                        uri=f"saiverse://self/chronicle/entry/{entry.id}",
                        level=entry.level,
                        start_time=entry.start_time,
                        end_time=entry.end_time,
                        message_count=entry.message_count,
                    )

    if search_memopedia:
        # For each keyword, find matching Memopedia page IDs
        kw_id_sets = []
        for kw in query_keywords:
            cur = conn.execute(
                "SELECT id FROM memopedia_pages WHERE "
                "(title LIKE ? OR summary LIKE ? OR content LIKE ?) "
                "AND id NOT LIKE 'root_%' "
                "AND (is_deleted = 0 OR is_deleted IS NULL)",
                (f"%{kw}%", f"%{kw}%", f"%{kw}%"),
            )
            kw_id_sets.append({row[0] for row in cur.fetchall()})

        all_memopedia_ids: set[str] = set()
        for ids in kw_id_sets:
            all_memopedia_ids |= ids

        for page_id in all_memopedia_ids:
            count = sum(1 for ids in kw_id_sets if page_id in ids)
            keyword_match_count[page_id] = count

        if all_memopedia_ids:
            from sai_memory.memopedia.storage import get_page
            for page_id in all_memopedia_ids:
                page = get_page(conn, page_id)
                if page:
                    keyword_hits[page.id] = RecallHit(
                        source_type="memopedia",
                        source_id=page.id,
                        title=page.title,
                        content=page.summary[:200] if page.summary else "",
                        score=0.0,
                        uri=f"saiverse://self/memopedia/page/{page.id}",
                        category=page.category,
                    )

    if search_fragments:
        kw_id_sets = []
        for kw in query_keywords:
            cur = conn.execute(
                "SELECT id FROM memopedia_fragments WHERE content LIKE ?",
                (f"%{kw}%",),
            )
            kw_id_sets.append({row[0] for row in cur.fetchall()})

        all_fragment_ids: set[str] = set()
        for ids in kw_id_sets:
            all_fragment_ids |= ids

        for frag_id in all_fragment_ids:
            count = sum(1 for ids in kw_id_sets if frag_id in ids)
            keyword_match_count[frag_id] = count

        if all_fragment_ids:
            placeholders = ",".join("?" for _ in all_fragment_ids)
            cur = conn.execute(
                f"SELECT f.id, f.content, f.entity_id, f.chronicle_entry_id, "
                f"f.source_date, p.title, p.category "
                f"FROM memopedia_fragments f "
                f"LEFT JOIN memopedia_pages p ON f.entity_id = p.id "
                f"WHERE f.id IN ({placeholders})",
                list(all_fragment_ids),
            )
            for row in cur.fetchall():
                fid, fcontent, eid, ceid, sdate, etitle, ecat = row
                keyword_hits[fid] = RecallHit(
                    source_type="fragment",
                    source_id=fid,
                    title=etitle,
                    content=fcontent[:200] if fcontent else "",
                    score=0.0,
                    uri=f"saiverse://self/memopedia/page/{eid}",
                    category=ecat,
                    entity_id=eid,
                    chronicle_entry_id=ceid,
                    source_date=sdate,
                )

    if search_messages:
        kw_id_sets = []
        for kw in query_keywords:
            cur = conn.execute(
                "SELECT id FROM messages WHERE content LIKE ? AND role != 'system'",
                (f"%{kw}%",),
            )
            kw_id_sets.append({row[0] for row in cur.fetchall()})

        all_message_ids: set[str] = set()
        for ids in kw_id_sets:
            all_message_ids |= ids

        for mid in all_message_ids:
            count = sum(1 for ids in kw_id_sets if mid in ids)
            keyword_match_count[mid] = count

        if all_message_ids:
            placeholders = ",".join("?" for _ in all_message_ids)
            cur = conn.execute(
                f"SELECT id, content, role, created_at FROM messages WHERE id IN ({placeholders})",
                list(all_message_ids),
            )
            for row in cur.fetchall():
                mid, mcontent, mrole, mcreated = row
                title = f"{mrole} @ {_format_timestamp(mcreated)}"
                excerpt = _keyword_excerpt(mcontent, query_keywords) if mcontent else ""
                keyword_hits[mid] = RecallHit(
                    source_type="message",
                    source_id=mid,
                    title=title,
                    content=excerpt,
                    score=0.0,
                    uri=f"saiverse://self/message/{mid}",
                    start_time=mcreated,
                )

    # --- Embedding search ---
    embedding_hits: dict[str, RecallHit] = {}

    vectors = embedder.embed([query], is_query=True)
    q = np.array(vectors[0], dtype=np.float32)
    vector_dim = q.shape[0]

    if search_chronicle:
        corpus = get_chronicle_embeddings(conn, level=chronicle_level)
        scored: list[tuple[str, float, RecallHit]] = []
        for entry_id, vec, level, content, start_time, end_time, msg_count in corpus:
            if len(vec) != vector_dim:
                continue
            v = np.array(vec, dtype=np.float32)
            score = _cosine_sim(q, v)
            time_range = _format_time_range(start_time, end_time)
            scored.append((entry_id, score, RecallHit(
                source_type="chronicle",
                source_id=entry_id,
                title=f"Chronicle Lv{level}: {time_range}",
                content=content[:200] if content else "",
                score=score,
                embed_score=score,
                uri=f"saiverse://self/chronicle/entry/{entry_id}",
                level=level,
                start_time=start_time,
                end_time=end_time,
                message_count=msg_count,
            )))
        scored.sort(key=lambda x: x[1], reverse=True)
        for sid, _, hit in scored[:total_cap * 2]:
            embedding_hits[sid] = hit

    if search_memopedia:
        corpus = get_memopedia_embeddings(conn)
        scored = []
        for page_id, vec, title, summary, category in corpus:
            if len(vec) != vector_dim:
                continue
            v = np.array(vec, dtype=np.float32)
            score = _cosine_sim(q, v)
            scored.append((page_id, score, RecallHit(
                source_type="memopedia",
                source_id=page_id,
                title=title,
                content=summary[:200] if summary else "",
                score=score,
                embed_score=score,
                uri=f"saiverse://self/memopedia/page/{page_id}",
                category=category,
            )))
        scored.sort(key=lambda x: x[1], reverse=True)
        for sid, _, hit in scored[:total_cap * 2]:
            embedding_hits[sid] = hit

    if search_fragments:
        corpus = get_fragment_embeddings(conn)
        scored = []
        for frag_id, vec, content, entity_id, chronicle_entry_id, source_date, etitle, ecat in corpus:
            if len(vec) != vector_dim:
                continue
            v = np.array(vec, dtype=np.float32)
            score = _cosine_sim(q, v)
            scored.append((frag_id, score, RecallHit(
                source_type="fragment",
                source_id=frag_id,
                title=etitle,
                content=content[:200] if content else "",
                score=score,
                embed_score=score,
                uri=f"saiverse://self/memopedia/page/{entity_id}",
                category=ecat,
                entity_id=entity_id,
                chronicle_entry_id=chronicle_entry_id,
                source_date=source_date,
            )))
        scored.sort(key=lambda x: x[1], reverse=True)
        for sid, _, hit in scored[:total_cap * 2]:
            embedding_hits[sid] = hit

    if search_messages:
        corpus = get_message_embeddings(conn)
        # message_id ごとに best (score, chunk_index, full_content, role, created_at) を
        # 保持する。表示 content は最後にまとめて再構築する (rev4: 先頭200字固定をやめ、
        # ベストマッチ chunk_index の周辺を抜粋する)。
        best_by_message: dict[str, tuple[float, int, str, str, int]] = {}
        for msg_id, vec, chunk_index, content, role, created_at in corpus:
            if len(vec) != vector_dim:
                continue
            v = np.array(vec, dtype=np.float32)
            score = _cosine_sim(q, v)
            prev = best_by_message.get(msg_id)
            if prev is None or score > prev[0]:
                best_by_message[msg_id] = (score, chunk_index, content, role, created_at)

        scored_list = sorted(best_by_message.items(), key=lambda kv: kv[1][0], reverse=True)
        for msg_id, (score, chunk_index, content, role, created_at) in scored_list[:total_cap * 2]:
            title = f"{role} @ {_format_timestamp(created_at)}"
            excerpt, used_chunk_index = reconstruct_message_excerpt(
                conn, msg_id, chunk_index, content or "",
            )
            embedding_hits[msg_id] = RecallHit(
                source_type="message",
                source_id=msg_id,
                title=title,
                content=excerpt,
                score=score,
                embed_score=score,
                uri=f"saiverse://self/message/{msg_id}",
                start_time=created_at,
                chunk_index=used_chunk_index,
            )

    # --- RRF fusion ---
    RRF_K = 60  # Standard RRF constant

    # Build rank maps
    keyword_rank: dict[str, int] = {}
    for rank, sid in enumerate(keyword_hits.keys()):
        keyword_rank[sid] = rank + 1

    embedding_rank: dict[str, int] = {}
    # Sort embedding hits by score for ranking
    sorted_embed = sorted(embedding_hits.items(), key=lambda x: x[1].score, reverse=True)
    for rank, (sid, _) in enumerate(sorted_embed):
        embedding_rank[sid] = rank + 1

    # Collect all source IDs
    all_ids = set(keyword_rank.keys()) | set(embedding_rank.keys())

    # Calculate RRF scores, layered by keyword match count.
    # Entries matching more keywords always rank above those matching fewer,
    # regardless of embedding similarity. Within the same match-count layer,
    # RRF score determines the order.
    rrf_scored: list[tuple[str, int, float]] = []  # (source_id, match_count, rrf_score)
    for sid in all_ids:
        score = 0.0
        if sid in keyword_rank:
            score += 1.0 / (RRF_K + keyword_rank[sid])
        if sid in embedding_rank:
            score += 1.0 / (RRF_K + embedding_rank[sid])
        match_count = keyword_match_count.get(sid, 0)
        rrf_scored.append((sid, match_count, score))

    # Sort by match_count DESC first, then RRF score DESC
    rrf_scored.sort(key=lambda x: (x[1], x[2]), reverse=True)

    # Build final result using per-source caps computed at the top.
    source_counts: dict[str, int] = {}
    results: List[RecallHit] = []
    for sid, match_count, rrf_score in rrf_scored:
        if len(results) >= total_cap:
            break
        hit = keyword_hits.get(sid) or embedding_hits.get(sid)
        if not hit:
            continue
        st = hit.source_type
        cap = source_caps.get(st, 0)
        if source_counts.get(st, 0) >= cap:
            continue
        hit.score = rrf_score
        # Carry the raw cosine similarity onto the returned hit even when the
        # keyword variant won the ``or`` above (keyword hits have embed_score=None).
        # A hit present in both maps was compared against the query embedding, so
        # its cosine lives on the embedding-map twin.
        if hit.embed_score is None:
            embed_twin = embedding_hits.get(sid)
            if embed_twin is not None:
                hit.embed_score = embed_twin.embed_score
        results.append(hit)
        source_counts[st] = source_counts.get(st, 0) + 1

    results.sort(key=lambda h: h.score, reverse=True)
    return results
