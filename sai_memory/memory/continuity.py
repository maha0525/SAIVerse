"""スレッドの前駆エッジ (thread_edges) — 記憶の連続性グラフの粗粒度の段。

正典: docs/issues/memory_continuity_graph.md の「決着」節 (2026-08-19 まはー裁定)。

- 子スレッドが「どのスレッドの続きか」を機械的に記帳する矢印。前駆は複数可
  (DAG)。エッジが無ければ一列として読む縮退。
- ``layer``: 'fact' = 生ログごと続いている / 'digest' = あらすじ・メモリ経由で
  知っているだけ。
- ``anchor_message_id``: 分岐点の messages.id。NULL = 親スレッド丸ごと継承。
- 記帳は LLM なしの機械操作。消費 (グラフをどう辿るか) は後続 wave の領分で、
  本モジュールは器と記帳・列挙だけを持つ。

storage.py の流儀に合わせ、conn を第一引数に取る素朴な関数群で構成する。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sai_memory.memory.pocketbook import validate_epoch

#: thread_edges.layer の閉語彙。
THREAD_EDGE_LAYERS = ("fact", "digest")


def init_continuity_tables(conn: sqlite3.Connection) -> None:
    """thread_edges テーブルを用意する。冪等。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_edges (
            edge_id TEXT PRIMARY KEY,
            child_thread_id TEXT NOT NULL,
            parent_thread_id TEXT NOT NULL,
            layer TEXT NOT NULL,
            anchor_message_id TEXT,
            origin TEXT,
            created_at INTEGER NOT NULL,
            meta TEXT,
            UNIQUE (child_thread_id, parent_thread_id, layer)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_edges_child ON thread_edges(child_thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_thread_edges_parent ON thread_edges(parent_thread_id)"
    )
    conn.commit()


@dataclass
class ThreadEdge:
    edge_id: str
    child_thread_id: str
    parent_thread_id: str
    layer: str  # 'fact' | 'digest'
    anchor_message_id: Optional[str]
    origin: Optional[str]  # 由来ヒント: 'branch' | 'import' | 'replant' | 'continue' 等
    created_at: int
    meta: Optional[Dict[str, Any]] = None


_EDGE_COLUMNS = (
    "edge_id, child_thread_id, parent_thread_id, layer, "
    "anchor_message_id, origin, created_at, meta"
)


def _row_to_edge(row) -> ThreadEdge:
    meta: Optional[Dict[str, Any]] = None
    if row[7]:
        try:
            decoded = json.loads(row[7])
            if isinstance(decoded, dict):
                meta = decoded
        except json.JSONDecodeError:
            meta = None
    return ThreadEdge(
        edge_id=row[0],
        child_thread_id=row[1],
        parent_thread_id=row[2],
        layer=row[3],
        anchor_message_id=row[4],
        origin=row[5],
        created_at=int(row[6]),
        meta=meta,
    )


def add_thread_edge(
    conn: sqlite3.Connection,
    child_thread_id: str,
    parent_thread_id: str,
    layer: str,
    *,
    anchor_message_id: Optional[str] = None,
    origin: Optional[str] = None,
    created_at: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> ThreadEdge:
    """エッジを記帳する。(child, parent, layer) が既にあれば既存エッジを返す (冪等)。

    DAG 保証: 辺の意味は「child が parent の続き」。自己辺と、parent の祖先に
    child が既にいる辺 (挿入すると循環になる) は ValueError で拒否する。
    循環検査は layer を区別せず全エッジを親向きに遡る。深さ上限は置かない —
    UNION の重複排除が thread_id 単独に効くので、有限個の ID で必ず自然終了
    する (既存データに万一循環があっても停止する)。

    原子性: 既存チェック → 循環検査 → INSERT は ``BEGIN IMMEDIATE`` の
    トランザクションで不可分にする (書き込み予約ロックを先に取り、他接続の
    書き込みは commit まで待たされる = 検査が必ず最新の確定状態を見る)。
    これ無しでは、検査と INSERT の間に他接続の書き込みが滑り込むと A→B と
    B→A が両方通って永続循環が成立する。呼び手が既にトランザクション中
    (``conn.in_transaction``) なら BEGIN も commit も発行せず呼び手に委ねる。
    ロック競合 (``sqlite3.OperationalError: database is locked``) はそのまま
    送出する (呼び手の再試行の領分)。
    """
    if layer not in THREAD_EDGE_LAYERS:
        raise ValueError(
            f"unknown thread edge layer: {layer!r} (expected one of {THREAD_EDGE_LAYERS})"
        )
    # ID は本システムでは常に文字列。非文字列は呼び手のバグなので、暗黙変換で
    # 救わず入口で拒否する — int の 1 と str の '1' が SQLite の TEXT affinity で
    # 同じ '1' に着地し、Python 側の自己辺判定 (1 == '1' は False) だけを
    # すり抜けて自己辺が永続する口を塞ぐ。
    if not isinstance(child_thread_id, str) or not child_thread_id.strip():
        raise ValueError(
            f"child_thread_id must be a non-empty string, got: {child_thread_id!r}"
        )
    if not isinstance(parent_thread_id, str) or not parent_thread_id.strip():
        raise ValueError(
            f"parent_thread_id must be a non-empty string, got: {parent_thread_id!r}"
        )
    if anchor_message_id is not None and not isinstance(anchor_message_id, str):
        raise ValueError(
            f"anchor_message_id must be a string or None, got: {anchor_message_id!r}"
        )
    if origin is not None and not isinstance(origin, str):
        raise ValueError(f"origin must be a string or None, got: {origin!r}")
    # meta は dict | None、キーは str 限定。非 dict も json.dumps は通るが、
    # 読み手 (_row_to_edge) が dict 以外を黙って捨てるため「書けたのに読めない」
    # 行になる。int / bool のキーも json.dumps が "1" / "true" へ黙って
    # 文字列化し、読み戻しが書いた姿と食い違う — どちらも入口で拒否する。
    if meta is not None:
        if not isinstance(meta, dict):
            raise ValueError(f"meta must be a dict or None, got: {meta!r}")
        for key in meta:
            if not isinstance(key, str):
                raise ValueError(f"meta keys must be strings, got: {key!r}")
    # SQLite は列型を強制しない — 文字列や bool の created_at が永続すると
    # created_at 順の列挙 (get_parent_edges 等) を毒するため入口で拒否する。
    validate_epoch("created_at", created_at)
    if child_thread_id == parent_thread_id:
        raise ValueError(
            f"self edge is not allowed: {child_thread_id!r} cannot be its own parent"
        )

    manage_txn = not conn.in_transaction
    if manage_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM thread_edges "
            "WHERE child_thread_id = ? AND parent_thread_id = ? AND layer = ?",
            (child_thread_id, parent_thread_id, layer),
        ).fetchone()
        if existing:
            if manage_txn:
                conn.rollback()  # 何も書いていない — ロックだけ手放す
            return _row_to_edge(existing)

        # 循環検査: parent から親向きエッジを遡った祖先集合に child がいたら、
        # この辺 (child は parent の続き) の追加で循環ができる。再帰の列は
        # thread_id 単独 — UNION の重複排除が ID 単位に効き、有限個の ID で
        # 必ず自然終了する (既存データに万一循環があっても停止する)。深さ上限は
        # 置かない — 上限より深い祖先が見えず循環を素通しする (fail-open) ため。
        reachable = conn.execute(
            """
            WITH RECURSIVE ancestors(thread_id) AS (
                SELECT ?
                UNION
                SELECT te.parent_thread_id
                FROM thread_edges te
                JOIN ancestors a ON te.child_thread_id = a.thread_id
            )
            SELECT 1 FROM ancestors WHERE thread_id = ? LIMIT 1
            """,
            (parent_thread_id, child_thread_id),
        ).fetchone()
        if reachable:
            raise ValueError(
                f"cycle detected: {child_thread_id!r} is an ancestor of "
                f"{parent_thread_id!r}; adding this edge would break the DAG"
            )

        edge_id = str(uuid.uuid4())
        ts = int(time.time()) if created_at is None else created_at
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        conn.execute(
            "INSERT INTO thread_edges("
            "edge_id, child_thread_id, parent_thread_id, layer, "
            "anchor_message_id, origin, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (edge_id, child_thread_id, parent_thread_id, layer,
             anchor_message_id, origin, ts, meta_json),
        )
    except sqlite3.IntegrityError:
        # 並行の同一辺挿入が UNIQUE (child, parent, layer) で弾かれた —
        # 巻き戻して既存行を読み直して返す (冪等)。
        if manage_txn:
            conn.rollback()
        existing = conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM thread_edges "
            "WHERE child_thread_id = ? AND parent_thread_id = ? AND layer = ?",
            (child_thread_id, parent_thread_id, layer),
        ).fetchone()
        if existing is None:
            raise
        return _row_to_edge(existing)
    except BaseException:
        if manage_txn:
            conn.rollback()
        raise
    if manage_txn:
        conn.commit()
    return ThreadEdge(
        edge_id=edge_id,
        child_thread_id=child_thread_id,
        parent_thread_id=parent_thread_id,
        layer=layer,
        anchor_message_id=anchor_message_id,
        origin=origin,
        created_at=ts,
        meta=meta if meta else None,
    )


def get_parent_edges(conn: sqlite3.Connection, child_thread_id: str) -> List[ThreadEdge]:
    """子スレッドから前駆 (親) 向きのエッジを列挙する。

    ID は文字列限定 (add_thread_edge と同族の口)。int を渡すと TEXT affinity で
    一致せず「エッジ無し」の嘘が返るため、入口で拒否する。
    """
    if not isinstance(child_thread_id, str):
        raise ValueError(
            f"child_thread_id must be a string, got: {child_thread_id!r}"
        )
    cur = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM thread_edges "
        "WHERE child_thread_id = ? ORDER BY created_at ASC, rowid ASC",
        (child_thread_id,),
    )
    return [_row_to_edge(row) for row in cur.fetchall()]


def get_child_edges(conn: sqlite3.Connection, parent_thread_id: str) -> List[ThreadEdge]:
    """親スレッドから後続 (子) 向きのエッジを列挙する。

    ID は文字列限定 (add_thread_edge と同族の口)。int を渡すと TEXT affinity で
    一致せず「エッジ無し」の嘘が返るため、入口で拒否する。
    """
    if not isinstance(parent_thread_id, str):
        raise ValueError(
            f"parent_thread_id must be a string, got: {parent_thread_id!r}"
        )
    cur = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM thread_edges "
        "WHERE parent_thread_id = ? ORDER BY created_at ASC, rowid ASC",
        (parent_thread_id,),
    )
    return [_row_to_edge(row) for row in cur.fetchall()]
