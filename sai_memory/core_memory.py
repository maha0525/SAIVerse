"""コア記憶 (記憶アーキv2 ゾーン A) のストレージ層。

コア記憶＝ペルソナが**自分で選んで刻む恒常知識**。head (システムプロンプト部) に
常駐し、Metabolism 時のみ更新が反映される。編集主体はペルソナ自身 (専用スペル
core_memory_add / core_memory_update / core_memory_remove)。システムは容量目安を
超過しても絶対に切り詰めない (通知のみ)。

ペルソナへの提示時は ``c:{id}`` 形式で参照する (Memopedia の ``m:N`` と同じ操作感)。

テーブルはペルソナの memory.db に同居する (Memopedia / Chronicle と同じ conn)。
``init_core_memory_table`` は SAIMemoryAdapter の初期化時に冪等に呼ばれる。

詳細設計: docs/intent/memory_architecture_v2.md §5
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class CoreMemory:
    """コア記憶 1 件。

    ``kind`` は項目種別。今回実装するのは 'note' (書き下ろしテキスト) のみだが、
    直後の増分で 'scene' (実会話の切り抜き＝口調・性格のアンカー) が入る予定。
    ``metadata`` は scene の由来参照 (元 message_id 群・日付等) などが将来入る余白。
    """
    id: int
    content: str
    created_at: int
    updated_at: int
    kind: str = "note"
    metadata: Optional[str] = None

    @property
    def ref(self) -> str:
        """ペルソナ提示用の参照 (例: ``c:3``)。"""
        return f"c:{self.id}"


def init_core_memory_table(conn: sqlite3.Connection) -> None:
    """コア記憶テーブルを初期化する (冪等)。

    ``kind`` / ``metadata`` は将来拡張 (scene 種別・由来参照) 用に最初から DDL に
    含める。新設テーブルなので今入れておけば将来のマイグレーションが不要になる。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS core_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'note',
            metadata TEXT
        )
        """
    )
    conn.commit()


def add_core_memory(
    conn: sqlite3.Connection,
    content: str,
    *,
    kind: str = "note",
    metadata: Optional[str] = None,
) -> int:
    """コア記憶を1件追加し、採番された id を返す。

    ``kind`` / ``metadata`` は 'scene' (実会話の切り抜き) 用の追加パラメータ。
    'note' の既存呼び出し (kind/metadata 省略) は後方互換のまま動く。
    """
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO core_memories (content, created_at, updated_at, kind, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        (content, now, now, kind, metadata),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_core_memory(conn: sqlite3.Connection, memory_id: int, content: str) -> bool:
    """既存のコア記憶を書き換える。対象が存在すれば True。"""
    now = int(time.time())
    cur = conn.execute(
        "UPDATE core_memories SET content = ?, updated_at = ? WHERE id = ?",
        (content, now, memory_id),
    )
    conn.commit()
    return cur.rowcount > 0


def remove_core_memory(conn: sqlite3.Connection, memory_id: int) -> bool:
    """コア記憶を削除する。対象が存在すれば True。"""
    cur = conn.execute("DELETE FROM core_memories WHERE id = ?", (memory_id,))
    conn.commit()
    return cur.rowcount > 0


def list_core_memories(conn: sqlite3.Connection) -> List[CoreMemory]:
    """全コア記憶を id 昇順で返す。"""
    rows = conn.execute(
        "SELECT id, content, created_at, updated_at, kind, metadata "
        "FROM core_memories ORDER BY id ASC"
    ).fetchall()
    return [
        CoreMemory(
            id=int(row[0]),
            content=str(row[1]),
            created_at=int(row[2]),
            updated_at=int(row[3]),
            kind=str(row[4]) if row[4] is not None else "note",
            metadata=row[5] if row[5] is not None else None,
        )
        for row in rows
    ]


def total_core_memory_chars(conn: sqlite3.Connection) -> int:
    """全コア記憶の本文文字数の合計を返す (容量目安判定に使う)。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM core_memories"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0
