"""vivid ページ → 机 (desk_items) への一回きり冪等 migration。

P4-c (concept_consolidation.md「P4-c: vividness 除去＋vivid→机移行」) の
移行本体。vividness='vivid' かつ未削除・非 trunk ページを desk_items に open する。

意図: vivid（鮮明）をメモ＝常設掲示として使っていた実ユーザーの需要は、
「机に開きっぱなしにしたい」需要の先行表現。desk が生まれる前から存在した
その意図を desk に継承する。P4-c 除去後、vividness カラムは読み書きが止まる
（死置き）ため、この移行を逃すと二度と回収できない。

流儀は marks→photos / P3a / P3c と同じ adapter init の一回きり冪等 migration:
- desk_items に既に該当 ref があれば触らない（冪等）
- trunk / 削除済み / category='core' は対象外
- 予算超過は移行では気にしない（次の Metabolism の LRU に委ねる）
- 移行時刻は saiverse.clock.now() 経由（仮想クロック尊重）

呼び出し元: SAIMemoryAdapter.__init__ の init_desk_tables() の直後。
"""
from __future__ import annotations

import logging
import sqlite3

LOGGER = logging.getLogger(__name__)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def migrate_vivid_pages_to_desk(conn: sqlite3.Connection) -> int:
    """vivid ページを desk_items へ移行する (冪等)。

    Returns:
        新たに desk_items へ追加した件数 (既に開いていたものは 0 カウント)。
    """
    if not _table_exists(conn, "memopedia_pages"):
        return 0
    if not _table_exists(conn, "desk_items"):
        return 0

    try:
        from saiverse import clock
        now = int(clock.now().timestamp())
    except Exception:
        import time
        now = int(time.time())

    # vivid かつ未削除・非 trunk・category != 'core' のページを取得
    # short_id から m:N 形式の ref を組む
    cur = conn.execute(
        """
        SELECT id, short_id
        FROM memopedia_pages
        WHERE vividness = 'vivid'
          AND (is_deleted = 0 OR is_deleted IS NULL)
          AND (is_trunk = 0 OR is_trunk IS NULL)
          AND (category != 'core' OR category IS NULL)
          AND id NOT LIKE 'root_%'
        """,
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    added = 0
    migrated_ids = []
    for page_id, short_id in rows:
        if short_id is None:
            # short_id 未採番 (通常は init の backfill 済みなので稀)。vivid の
            # 印を残して次回 init で再試行する
            LOGGER.debug("vivid_to_desk: page %s has no short_id, skip", page_id)
            continue
        ref = f"m:{short_id}"
        migrated_ids.append(page_id)

        # 既に机に開いていれば挿入不要 (意図は既に満たされている)
        existing = conn.execute(
            "SELECT 1 FROM desk_items WHERE ref = ?", (ref,)
        ).fetchone()
        if existing:
            continue

        conn.execute(
            "INSERT INTO desk_items (ref, opened_at, last_touched_at, purpose_ref) "
            "VALUES (?, ?, ?, NULL)",
            (ref, now, now),
        )
        added += 1

    # 一回きり性の担保: 処理済みページの vivid の印を落とす (カラムは死置きだが
    # 印を残すと、本人が後で机から閉じたページを次の起動で再度開き直してしまう —
    # 「閉じる」という本人の行為を移行が上書きしないための必須ステップ)
    if migrated_ids:
        placeholders = ",".join("?" for _ in migrated_ids)
        conn.execute(
            f"UPDATE memopedia_pages SET vividness = 'rough' WHERE id IN ({placeholders})",
            migrated_ids,
        )
        conn.commit()
    if added > 0:
        LOGGER.info(
            "vivid_to_desk_migration: %d vivid page(s) opened on desk", added
        )
    return added
