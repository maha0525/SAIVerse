"""Mark (観測点) ストア — 「意味の予約」のペルソナ別永続化。

life_concept_map.md §8 (b)「観測点＝意味の予約」/ §9.1 層 1 の受け皿。mark は
SAIMemory メッセージに引用アンカー ``(message_id, 逐語引用)`` で付く注釈で、
**目的ノードではない** (§3.1: 種が違う。収穫されて初めて候補が生まれる)。

保存先の選定 — **memory.db 相乗り** (別ファイル marks.db にしない) の理由:

1. mark のアンカーは SAIMemory メッセージ (``message_id`` + 逐語引用) を指す。
   注釈は注釈対象と同じファイルに置くのがアクセス・バックアップ (rdiff-backup の
   既存対象) ・整合の全てで自然。
2. 生きている前例が memopedia (sai_memory/memopedia/storage.py): 注釈・編纂層の
   テーブル群は adapter.conn (= memory.db) に相乗りし、``init_*_tables(conn)`` +
   「CREATE TABLE IF NOT EXISTS + try/except ALTER」の migration 流儀を持つ。
   本モジュールはこれに倣う。
3. もう一方の既存例だった per-persona tasks.db (persona/tasks/storage.py) は
   統合 persona_task テーブル (main DB) へ一本化されて廃止済み — 「ペルソナ別の
   独立 SQLite ファイルを増やす」流儀はもう生きていない。

時刻は epoch 秒 int で、必ず ``saiverse.clock.now()`` 経由で刻む (一日シミュレータ
の仮想クロック尊重。autonomous_behavior_v2.md §12)。sai_memory → saiverse の
import は unified_recall (saiverse.references) 等に既存の前例がある。

P1 (DB 基盤) の範囲はテーブルと API のみ — ``==語句==`` マーカーのパースや
収穫 (mark → 候補) の配線は P3 以降 (life_concept_map.md §14)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional

from saiverse import clock


@dataclass(frozen=True)
class Mark:
    """1 つの観測点。``quote`` は message 本文からの逐語引用 (アンカー §9.1)。"""

    mark_id: str
    message_id: str
    quote: str
    purpose_ref: Optional[str]         # 目的ノード参照 (``task:N`` 等)。素の予約は None
    created_at: int                    # epoch 秒 (clock 経由)
    harvested_to: Optional[str]        # 収穫先の来歴 ref (未収穫は None)
    origin_episode_ref: Optional[str]  # 打たれた時に開いていた出来事 (``episode:N``)


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def init_marks_tables(conn: sqlite3.Connection) -> None:
    """marks テーブルを初期化する (冪等)。memopedia の init 流儀に倣う。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS marks (
            mark_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            quote TEXT NOT NULL,
            purpose_ref TEXT,
            created_at INTEGER NOT NULL,
            harvested_to TEXT,
            origin_episode_ref TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_marks_message ON marks(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_marks_created ON marks(created_at)"
    )
    # 未収穫フィルタ用 (list_marks(unharvested_only=True) が常用クエリ)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_marks_harvested ON marks(harvested_to)"
    )
    conn.commit()


_MARK_COLS = (
    "mark_id, message_id, quote, purpose_ref, created_at, harvested_to, origin_episode_ref"
)


def _row_to_mark(row: tuple) -> Mark:
    return Mark(
        mark_id=row[0],
        message_id=row[1],
        quote=row[2],
        purpose_ref=row[3],
        created_at=int(row[4]),
        harvested_to=row[5],
        origin_episode_ref=row[6],
    )


def add_mark(
    conn: sqlite3.Connection,
    *,
    message_id: str,
    quote: str,
    purpose_ref: Optional[str] = None,
    origin_episode_ref: Optional[str] = None,
) -> Mark:
    """観測点を打つ (§8: 意味ではなく意味の予約。行動の最中にしか打てない)。

    ``quote`` は対象メッセージ本文からの逐語引用。実在検証 (引用が本当に
    message 本文に含まれるか) は呼び出し側の責務 — 本レイヤーは永続化のみ。
    """
    if not message_id:
        raise ValueError("message_id is required")
    if not quote:
        raise ValueError("quote is required")
    mark = Mark(
        mark_id=str(uuid.uuid4()),
        message_id=message_id,
        quote=quote,
        purpose_ref=purpose_ref,
        created_at=_now_epoch(),
        harvested_to=None,
        origin_episode_ref=origin_episode_ref,
    )
    conn.execute(
        f"INSERT INTO marks ({_MARK_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            mark.mark_id, mark.message_id, mark.quote, mark.purpose_ref,
            mark.created_at, mark.harvested_to, mark.origin_episode_ref,
        ),
    )
    conn.commit()
    return mark


def get_mark(conn: sqlite3.Connection, mark_id: str) -> Optional[Mark]:
    """mark_id で 1 件引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_MARK_COLS} FROM marks WHERE mark_id = ?", (mark_id,)
    )
    row = cur.fetchone()
    return _row_to_mark(row) if row else None


def list_marks(
    conn: sqlite3.Connection,
    *,
    unharvested_only: bool = False,
    since: Optional[int] = None,
    message_id: Optional[str] = None,
) -> List[Mark]:
    """観測点の一覧 (created_at 昇順)。

    Args:
        unharvested_only: True なら未収穫 (harvested_to IS NULL) のみ —
            収穫 (mark → desire、起床判断等の前段操作 §5.1) の読み手が使う。
        since: epoch 秒。指定時は ``created_at >= since`` のみ。
        message_id: 指定時はそのメッセージに付いた mark のみ。
    """
    conditions: List[str] = []
    params: List = []
    if unharvested_only:
        conditions.append("harvested_to IS NULL")
    if since is not None:
        conditions.append("created_at >= ?")
        params.append(int(since))
    if message_id is not None:
        conditions.append("message_id = ?")
        params.append(message_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cur = conn.execute(
        f"SELECT {_MARK_COLS} FROM marks {where} ORDER BY created_at ASC, mark_id ASC",
        params,
    )
    return [_row_to_mark(row) for row in cur.fetchall()]


def mark_harvested(
    conn: sqlite3.Connection, mark_id: str, harvested_to: str
) -> Optional[Mark]:
    """収穫済みにする (harvested_to = 生まれた候補等への来歴 ref)。

    §3.1「収穫されて初めて候補が生まれる (来歴リンクで接地)」の mark 側の刻印。
    mark 自体は消さない (歴史として残す §5.1)。無ければ None。
    """
    if not harvested_to:
        raise ValueError("harvested_to is required")
    cur = conn.execute(
        "UPDATE marks SET harvested_to = ? WHERE mark_id = ?",
        (harvested_to, mark_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_mark(conn, mark_id)
