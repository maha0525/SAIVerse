"""想起用タグの辺 (chunk_page_edges) — チャンクと実体ページの関与記録。

正典: docs/intent/autonomous_behavior_v3.md §13.6 の「B2 欄と辺の格納」。

- entity_extractor の同一コールに載る `involved_entities` (この範囲が関与した
  既存ページの列挙) の格納先。チャンク (Chronicle ページ) も実体も Memopedia
  ページなので、辺はページ ID 同士で張る。
- 役目は選別ではなく**遡りの材料** (§12.1) — 「このチャンクにどの実体が
  居合わせたか」「この実体はどのチャンクに現れたか」の両向きを引けること。
- 記帳は冪等 (同じ組は一度だけ)。

storage.py の流儀に合わせ、conn を第一引数に取る素朴な関数群で構成する。
"""

from __future__ import annotations

import sqlite3
import time
from typing import List, Optional

from sai_memory.memory.pocketbook import validate_epoch


def init_chunk_page_edge_tables(conn: sqlite3.Connection) -> None:
    """chunk_page_edges テーブルを用意する。冪等。

    (chronicle_page_id, entity_page_id) の複合 PRIMARY KEY が UNIQUE 制約と
    chronicle 起点のインデックスを兼ねる。逆向き (entity 起点) は専用
    インデックスで張る。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_page_edges (
            chronicle_page_id TEXT NOT NULL,
            entity_page_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (chronicle_page_id, entity_page_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunk_page_edges_entity "
        "ON chunk_page_edges(entity_page_id)"
    )
    conn.commit()


def add_chunk_page_edge(
    conn: sqlite3.Connection,
    chronicle_page_id: str,
    entity_page_id: str,
    *,
    created_at: Optional[int] = None,
) -> bool:
    """辺を記帳する。新規に張れたら True、既にあれば False (冪等)。"""
    # ページ ID は本システムでは常に文字列。非文字列は TEXT affinity の変換で
    # '123' の顔に着地して Memopedia ページ ID と照合できない辺が永続する —
    # 暗黙変換で救わず入口で拒否する (continuity.add_thread_edge と同族の口)。
    if not isinstance(chronicle_page_id, str) or not chronicle_page_id.strip():
        raise ValueError(
            f"chronicle_page_id must be a non-empty string, got: {chronicle_page_id!r}"
        )
    if not isinstance(entity_page_id, str) or not entity_page_id.strip():
        raise ValueError(
            f"entity_page_id must be a non-empty string, got: {entity_page_id!r}"
        )
    # SQLite は列型を強制しない — 文字列や bool の created_at が永続すると
    # created_at 順の列挙を毒するため入口で拒否する (int | None のみ)。
    validate_epoch("created_at", created_at)
    ts = int(time.time()) if created_at is None else created_at
    cur = conn.execute(
        "INSERT OR IGNORE INTO chunk_page_edges("
        "chronicle_page_id, entity_page_id, created_at) VALUES (?, ?, ?)",
        (chronicle_page_id, entity_page_id, ts),
    )
    conn.commit()
    return cur.rowcount > 0


def list_entity_pages_for_chronicle(
    conn: sqlite3.Connection, chronicle_page_id: str
) -> List[str]:
    """チャンク (Chronicle ページ) に居合わせた実体ページ ID を列挙する。

    ID は文字列限定 (add_chunk_page_edge と同族の口) — 非文字列は入口で拒否。
    """
    if not isinstance(chronicle_page_id, str):
        raise ValueError(
            f"chronicle_page_id must be a string, got: {chronicle_page_id!r}"
        )
    cur = conn.execute(
        "SELECT entity_page_id FROM chunk_page_edges "
        "WHERE chronicle_page_id = ? ORDER BY created_at ASC, rowid ASC",
        (chronicle_page_id,),
    )
    return [row[0] for row in cur.fetchall()]


def list_chronicle_pages_for_entity(
    conn: sqlite3.Connection, entity_page_id: str
) -> List[str]:
    """実体ページが現れたチャンク (Chronicle ページ) ID を列挙する (逆向き)。

    ID は文字列限定 (add_chunk_page_edge と同族の口) — 非文字列は入口で拒否。
    """
    if not isinstance(entity_page_id, str):
        raise ValueError(
            f"entity_page_id must be a string, got: {entity_page_id!r}"
        )
    cur = conn.execute(
        "SELECT chronicle_page_id FROM chunk_page_edges "
        "WHERE entity_page_id = ? ORDER BY created_at ASC, rowid ASC",
        (entity_page_id,),
    )
    return [row[0] for row in cur.fetchall()]
