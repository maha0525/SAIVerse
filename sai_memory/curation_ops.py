"""編纂プランの永続化層 — per-persona memory.db の curation_plans テーブル。

P4-a の三層（検知 → 裁定 → 実行）のうち「裁定から実行への橋渡し」を担う。
就寝判断（day_close）の finalize が approve された op_id を
``enqueue_plan`` でここに書き込む。

実際の分割・統合の書き換え（merge/split 本体）は P4-a2 で実装する——
**このモジュールには実行系関数を作らない**。その領分であることを
下記 docstring に明記してある。

テーブル定義（冪等）:
    id          TEXT PRIMARY KEY         -- UUID
    created_at  INTEGER                  -- epoch 秒
    kind        TEXT                     -- "split" | "merge" | "fold"
    op_id       TEXT                     -- 検知層が付けた決定論の一意 ID
    refs_json   TEXT                     -- JSON 配列 [m:N, ...] ページ参照
    status      TEXT DEFAULT 'pending'   -- "pending"|"done"|"failed"|"rejected"
    result_json TEXT NULL                -- 実行後の結果 JSON（P4-a2 が書く）
    executed_at INTEGER NULL             -- 実行完了 epoch 秒（P4-a2 が書く）
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Dict, List

LOGGER = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"


# ---------------------------------------------------------------------------
# テーブル初期化（冪等）
# ---------------------------------------------------------------------------


def init_curation_tables(conn: sqlite3.Connection) -> None:
    """curation_plans テーブルを冪等に初期化する。

    adapter init（saiverse_memory/adapter.py）から呼び出す。
    既にテーブルが存在する場合は何もしない。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS curation_plans (
            id          TEXT PRIMARY KEY,
            created_at  INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            op_id       TEXT NOT NULL,
            refs_json   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT,
            executed_at INTEGER
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_curation_plans_op_id"
        " ON curation_plans(op_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_curation_plans_status"
        " ON curation_plans(status)"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def enqueue_plan(
    conn: sqlite3.Connection,
    kind: str,
    op_id: str,
    refs: List[str],
) -> str:
    """編纂プランを curation_plans に追加する。

    同じ ``op_id`` の pending プランが既に存在する場合は **重複挿入しない**
    （冪等。approve を二度押しされても行は 1 件のまま）。

    Args:
        conn:   per-persona memory.db の接続
        kind:   "split" | "merge" | "fold"
        op_id:  検知層が付けた決定論の一意 ID（例: "split:m:12"）
        refs:   操作対象ページの参照ラベル（例: ["m:12"]）

    Returns:
        既存 pending 行の id、または新規挿入した行の id。

    P4-a2 の領分（このモジュールでは実装しない）:
        - merge 本体（残す側に消える側を逐語結合、子ページ付け替え、soft-delete）
        - split 本体（段落ブロック割り当て + コード逐語移動 + 保存則機械検証）
        - status を "done"/"failed" に更新し result_json / executed_at を書く
    """
    # 既存 pending を検索
    cur = conn.execute(
        "SELECT id FROM curation_plans WHERE op_id = ? AND status = ?",
        (op_id, STATUS_PENDING),
    )
    existing = cur.fetchone()
    if existing:
        LOGGER.debug(
            "[curation_ops] op_id=%r already has a pending plan (%s); skipping",
            op_id, existing[0],
        )
        return existing[0]

    plan_id = str(uuid.uuid4())
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO curation_plans
            (id, created_at, kind, op_id, refs_json, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (plan_id, now, kind, op_id, json.dumps(refs, ensure_ascii=False), STATUS_PENDING),
    )
    conn.commit()
    LOGGER.info(
        "[curation_ops] enqueued plan id=%s kind=%s op_id=%r refs=%r",
        plan_id, kind, op_id, refs,
    )
    return plan_id


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------


def list_pending(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """pending 状態の編纂プランを古い順で返す。

    Returns:
        list of dict with keys: id, created_at, kind, op_id, refs_json
    """
    cur = conn.execute(
        """
        SELECT id, created_at, kind, op_id, refs_json
        FROM curation_plans
        WHERE status = ?
        ORDER BY created_at
        """,
        (STATUS_PENDING,),
    )
    rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "created_at": row[1],
            "kind": row[2],
            "op_id": row[3],
            "refs": json.loads(row[4] or "[]"),
        }
        for row in rows
    ]
