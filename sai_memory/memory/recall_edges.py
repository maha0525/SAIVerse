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
from typing import Iterable, List, Optional

from sai_memory.memory.pocketbook import validate_epoch

#: 1 文 (``IN (...)``) に載せるページ id の上限。SQLite の変数上限
#: (古いビルドでは 999) に当たらない範囲へ刻む。
_ID_CHUNK = 400


def init_chunk_page_edge_tables(
    conn: sqlite3.Connection, *, commit: bool = True
) -> None:
    """chunk_page_edges テーブルを用意する。冪等。

    (chronicle_page_id, entity_page_id) の複合 PRIMARY KEY が UNIQUE 制約と
    chronicle 起点のインデックスを兼ねる。逆向き (entity 起点) は専用
    インデックスで張る。

    Args:
        commit: False にすると確定しない。呼び出し元が既にトランザクションを
            開いている場合に渡す —— ここで commit すると他人の書き込みを
            巻き込んで確定してしまう。
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
    if commit:
        conn.commit()


def add_chunk_page_edge(
    conn: sqlite3.Connection,
    chronicle_page_id: str,
    entity_page_id: str,
    *,
    created_at: Optional[int] = None,
    commit: bool = True,
) -> bool:
    """辺を記帳する。新規に張れたら True、既にあれば False (冪等)。

    Args:
        commit: False にすると確定しない。「書いてよい状態か」の検査と INSERT を
            同じトランザクションに収める呼び出し元
            (:func:`~sai_memory.memory.entity_extractor._record_involvement_edges`)
            が渡す —— 1 本ずつ確定すると、検査を通った後に失効した実行の辺が
            確定済みで残る。
    """
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
    if commit:
        conn.commit()
    return cur.rowcount > 0


def delete_chunk_page_edges(
    conn: sqlite3.Connection, page_ids: Iterable[str],
) -> int:
    """指定したページに繋がる辺を**両向きとも**消す。消した本数を返す。

    ページを**物理削除**する側が、その削除と同じトランザクションで呼ぶ。
    ``chunk_page_edges`` には外部キーも削除連鎖も無いため、呼ばなければ消えた
    ページを指す辺が残り続ける —— 実際に起きる経路が両側にある:

    - チャンク側: Chronicle の再生成 (``arasuji.storage.regenerate_entry``) は
      旧エントリを別 id の新エントリへ差し替える。旧 id を指す辺は新エントリの
      再抽出でも消えず、存在しないページを指したまま遡りに現れる。
      解体 (``dismantle_entry``) と各種明示削除も同じ。
    - 実体側: ``memopedia.storage.delete_page`` の物理削除
      (``Memopedia.clear_all_pages`` / ``import_json(clear_existing=True)``)。

    両向きを 1 関数で消すのは、削除する側が「このページはチャンクか実体か」を
    知らなくてよくするため —— 判定を呼び出し元へ配ると、片側だけ忘れた経路が
    必ず出る。

    **commit しない。** 確定は呼び出し元の削除と同じ tx に委ねる (辺だけ先に
    確定すると、削除が rollback された回に辺だけが失われる)。テーブルがまだ
    無い DB (抽出が一度も走っていない) では 0 を返す。
    """
    ids = [pid for pid in page_ids if isinstance(pid, str) and pid]
    if not ids:
        return 0
    deleted = 0
    for start in range(0, len(ids), _ID_CHUNK):
        block = ids[start:start + _ID_CHUNK]
        placeholders = ",".join("?" for _ in block)
        try:
            cur = conn.execute(
                f"DELETE FROM chunk_page_edges "
                f"WHERE chronicle_page_id IN ({placeholders}) "
                f"   OR entity_page_id IN ({placeholders})",
                block + block,
            )
        except sqlite3.OperationalError:
            # 想起用タグが一度も走っていない DB にはテーブルが無い。削除の
            # 巻き添えで落とさない (辺が無いのだから孤児も生まれない)。
            return deleted
        deleted += cur.rowcount
    return deleted


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
