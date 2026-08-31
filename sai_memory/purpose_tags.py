"""purpose_tags ストア — 目的タグ (多対多・後付け) のペルソナ別永続化。

life_concept_map.md §9「タグは多対多・後付け」/ §9.1「目的タグの付与 — 五層方式」
の受け皿。タグは「target (メッセージ・出来事・アイテム等) がこの目的に属する」
という後付けの所属宣言で、想起 (§9.2 随意側) が目的を鍵に過去を引き戻す橋になる。

保存先は clips (sai_memory/clips.py) と同じく **memory.db 相乗り**:
タグの target の主流はSAIMemory メッセージであり、注釈は注釈対象と同じファイルに
置く (アクセス・バックアップ・整合)。``init_purpose_tags_tables(conn)`` 冪等 +
memopedia 流儀の migration スタイルに倣う。adapter への組み込みは P4 時点では
行わない (ストア単体・休眠。呼び出し元は連想歩行 saiverse/recall_walk.py)。

行の設計 (§9.1):

- ``target_ref`` / ``purpose_ref`` は参照アドレッシング統一の文法
  (saiverse/references.py — ``message:<id>`` / ``task:N`` / ``episode:N`` 等)。
  本レイヤーは書式検証をしない (グラマー層と実体解決は呼び出し側の責務)。
- ``layer`` は付与された瞬間の層: 2=棚入れ (出来事の境界) / 3=再訪 (想起した
  ライン) / 4=代謝 (Metabolism バッチ)。**層 0 (継承) は保存しない** — 開いて
  いる出来事の目的参照からの導出で得る設計 (§9.1 の表、§8.1 層 0 の継承元)。
  層 1 は mark (観測点) であってタグではない (種が違う §3.1)。
- 同一 (target_ref, purpose_ref) ペアは 1 行 (UNIQUE)。タグの粗製濫造前提
  (§9.1) でも一意性は保ち、再付与は upsert で layer 最新化 + revisit_count+1
  (「タグ密度は使用頻度に比例して濃くなる」の濃さは行数でなく revisit_count に
  積む)。``created_at`` は初回付与の時刻を保持する (来歴)。

時刻は epoch 秒 int で、必ず ``saiverse.clock.now()`` 経由で刻む (一日シミュ
レータの仮想クロック尊重。autonomous_behavior_v2.md §12)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import List, Optional

from saiverse import clock

# --- 付与層 (life_concept_map.md §9.1 の表。層 0 は導出、層 1 は mark) ---
LAYER_SHELVE = 2      # 棚入れ: 出来事の境界 (判断点) の構造化出力
LAYER_REVISIT = 3     # 再訪: 想起が起きた Pulse で想起したラインが書く
LAYER_METABOLISM = 4  # 代謝: Metabolism バッチのまとめて densify
VALID_LAYERS = frozenset({LAYER_SHELVE, LAYER_REVISIT, LAYER_METABOLISM})


@dataclass(frozen=True)
class PurposeTag:
    """1 つの目的タグ (target が purpose に属するという後付けの宣言)。"""

    tag_id: str
    target_ref: str       # 統一文法の参照 (message:<id> / episode:N / item:N …)
    purpose_ref: str      # 目的ノード参照 (task:N / track:N 等)
    layer: int            # 2=棚入れ / 3=再訪 / 4=代謝
    created_at: int       # 初回付与の epoch 秒 (clock 経由)
    revisit_count: int    # 再付与・再訪の回数 (初回は 0)


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def init_purpose_tags_tables(conn: sqlite3.Connection) -> None:
    """purpose_tags テーブルを初期化する (冪等)。memopedia / marks の init 流儀。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS purpose_tags (
            tag_id TEXT PRIMARY KEY,
            target_ref TEXT NOT NULL,
            purpose_ref TEXT NOT NULL,
            layer INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            revisit_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(target_ref, purpose_ref)
        )
        """
    )
    # 連想歩行の常用クエリ 2 方向 (purpose → targets / target → purposes)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_purpose_tags_purpose ON purpose_tags(purpose_ref)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_purpose_tags_target ON purpose_tags(target_ref)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_purpose_tags_created ON purpose_tags(created_at)"
    )
    conn.commit()


_TAG_COLS = "tag_id, target_ref, purpose_ref, layer, created_at, revisit_count"


def _row_to_tag(row: tuple) -> PurposeTag:
    return PurposeTag(
        tag_id=row[0],
        target_ref=row[1],
        purpose_ref=row[2],
        layer=int(row[3]),
        created_at=int(row[4]),
        revisit_count=int(row[5]),
    )


def get_tag(conn: sqlite3.Connection, tag_id: str) -> Optional[PurposeTag]:
    """tag_id で 1 件引く。無ければ None。"""
    cur = conn.execute(
        f"SELECT {_TAG_COLS} FROM purpose_tags WHERE tag_id = ?", (tag_id,)
    )
    row = cur.fetchone()
    return _row_to_tag(row) if row else None


def _get_by_pair(
    conn: sqlite3.Connection, target_ref: str, purpose_ref: str
) -> Optional[PurposeTag]:
    cur = conn.execute(
        f"SELECT {_TAG_COLS} FROM purpose_tags WHERE target_ref = ? AND purpose_ref = ?",
        (target_ref, purpose_ref),
    )
    row = cur.fetchone()
    return _row_to_tag(row) if row else None


def add_tag(
    conn: sqlite3.Connection,
    *,
    target_ref: str,
    purpose_ref: str,
    layer: int,
) -> PurposeTag:
    """目的タグを付ける (同一ペアは upsert — layer 最新化 + revisit_count+1)。

    タグ付けは意味づけの完了ではなく開始 (§9)。同じ所属が別の瞬間に再び
    宣言されたら、それは重複行ではなく「再訪」として同じ行に濃さを積む。
    ``created_at`` は初回付与の時刻のまま残す (いつからの関心かという来歴)。
    """
    if not target_ref:
        raise ValueError("target_ref is required")
    if not purpose_ref:
        raise ValueError("purpose_ref is required")
    if layer not in VALID_LAYERS:
        raise ValueError(
            f"invalid layer: {layer!r} (expected one of {sorted(VALID_LAYERS)}; "
            "layer 0 is derived, layer 1 is a mark — see life_concept_map.md §9.1)"
        )
    existing = _get_by_pair(conn, target_ref, purpose_ref)
    if existing is not None:
        conn.execute(
            "UPDATE purpose_tags SET layer = ?, revisit_count = revisit_count + 1 "
            "WHERE tag_id = ?",
            (int(layer), existing.tag_id),
        )
        conn.commit()
        return get_tag(conn, existing.tag_id)
    tag = PurposeTag(
        tag_id=str(uuid.uuid4()),
        target_ref=target_ref,
        purpose_ref=purpose_ref,
        layer=int(layer),
        created_at=_now_epoch(),
        revisit_count=0,
    )
    conn.execute(
        f"INSERT INTO purpose_tags ({_TAG_COLS}) VALUES (?, ?, ?, ?, ?, ?)",
        (
            tag.tag_id, tag.target_ref, tag.purpose_ref,
            tag.layer, tag.created_at, tag.revisit_count,
        ),
    )
    conn.commit()
    return tag


def list_by_purpose(
    conn: sqlite3.Connection, purpose_ref: str
) -> List[PurposeTag]:
    """目的を鍵に target 群を引く (想起の主方向 §9「想起は目的を鍵に…引き戻す橋」)。

    新しさ降順 (created_at DESC)。連想歩行が予算内で消費する順に合わせる。
    """
    cur = conn.execute(
        f"SELECT {_TAG_COLS} FROM purpose_tags WHERE purpose_ref = ? "
        "ORDER BY created_at DESC, tag_id ASC",
        (purpose_ref,),
    )
    return [_row_to_tag(row) for row in cur.fetchall()]


def list_by_target(
    conn: sqlite3.Connection, target_ref: str
) -> List[PurposeTag]:
    """target に付いた目的群を引く (タグ共起の逆方向 §9.3「同じ件に絡んでいた別の話」)。"""
    cur = conn.execute(
        f"SELECT {_TAG_COLS} FROM purpose_tags WHERE target_ref = ? "
        "ORDER BY created_at DESC, tag_id ASC",
        (target_ref,),
    )
    return [_row_to_tag(row) for row in cur.fetchall()]


def touch_revisit(conn: sqlite3.Connection, tag_id: str) -> Optional[PurposeTag]:
    """再訪を刻む (revisit_count+1。層は変えない)。

    層 3 の再訪 (§9.1) で「既に付いているタグを想起がなぞった」ときの刻印。
    再訪の深化が重み (連想歩行のスコア) に効く。無ければ None。
    """
    cur = conn.execute(
        "UPDATE purpose_tags SET revisit_count = revisit_count + 1 WHERE tag_id = ?",
        (tag_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None
    return get_tag(conn, tag_id)
