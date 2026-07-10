"""机 (desk) ストア — head の開きっぱなし領域、机の物理。

concept_consolidation.md「開閉制御 — 机の物理」の実装。Memory Atlas の
``memory_open`` / ``memory_close`` (P2実装分割 P2a) が使う唯一の「開いている」
真実源で、Metabolism を跨いで head に残り続けるページの集合を表す。

- **机 = 有限の作業面**。文字数予算 (``DEFAULT_DESK_BUDGET_CHARS``、env
  ``SAIVERSE_DESK_BUDGET_CHARS`` で上書き可) を持つ。
- **溢れたら決定論 LRU で自動追い出し** (``evict_lru``)。最も長く触られて
  いない (``last_touched_at`` が最古の) ページから閉じる。判断点には載せない
  (頻発しうる事象でノイズになる)。
- **touch の定義は決定論**: そのページを対象にした read / write / clip /
  参照が触った扱い (呼び出し側が ``touch_item`` を呼ぶ)。
- **コア記憶は机の予算外**のシステム常設ピン — 本ストアの対象外 (Atlas
  ファサード側が core / c:N を desk に一切触れさせない)。

保存先は memory.db 相乗り (Memopedia / Chronicle / 写真と同じ conn)。
``ref`` は Memory Atlas の参照書式 (``m:N`` / ``ch:N`` 等) をそのまま主キーに
使う。時刻は必ず ``saiverse.clock.now()`` 経由で刻む (仮想クロック尊重、
autonomous_behavior_v2.md §12)。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional

from saiverse import clock

LOGGER = logging.getLogger(__name__)

DEFAULT_DESK_BUDGET_CHARS = 8000


@dataclass(frozen=True)
class DeskItem:
    """机に開かれているページ 1 件。"""

    ref: str
    opened_at: int
    last_touched_at: int
    purpose_ref: Optional[str]


def _now_epoch() -> int:
    """現在時刻 (epoch 秒)。仮想クロック尊重のため必ず clock.now() を通す。"""
    return int(clock.now().timestamp())


def resolve_desk_budget_chars() -> int:
    """机の文字数予算を解決する。env ``SAIVERSE_DESK_BUDGET_CHARS`` で上書き可。"""
    env_val = os.getenv("SAIVERSE_DESK_BUDGET_CHARS")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            LOGGER.warning(
                "Invalid SAIVERSE_DESK_BUDGET_CHARS=%r, using default %d",
                env_val, DEFAULT_DESK_BUDGET_CHARS,
            )
    return DEFAULT_DESK_BUDGET_CHARS


def init_desk_tables(conn: sqlite3.Connection) -> None:
    """desk_items テーブルを初期化する (冪等)。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS desk_items (
            ref TEXT PRIMARY KEY,
            opened_at INTEGER NOT NULL,
            last_touched_at INTEGER NOT NULL,
            purpose_ref TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_desk_items_touched ON desk_items(last_touched_at)"
    )
    conn.commit()


_DESK_COLS = "ref, opened_at, last_touched_at, purpose_ref"


def _row_to_item(row: tuple) -> DeskItem:
    return DeskItem(
        ref=row[0],
        opened_at=int(row[1]),
        last_touched_at=int(row[2]),
        purpose_ref=row[3],
    )


def _get_item(conn: sqlite3.Connection, ref: str) -> Optional[DeskItem]:
    cur = conn.execute(f"SELECT {_DESK_COLS} FROM desk_items WHERE ref = ?", (ref,))
    row = cur.fetchone()
    return _row_to_item(row) if row else None


def open_item(
    conn: sqlite3.Connection,
    ref: str,
    *,
    purpose_ref: Optional[str] = None,
) -> DeskItem:
    """ページを机に開く。既に開いていれば touch 扱い (opened_at は初回のまま保つ)。

    再 open で ``purpose_ref`` を渡した場合は最新の目的で上書きする
    (「いまの目的との関係」で開きが動く、concept_consolidation.md 「Track
    紐づきの開き」)。省略時 (None) は既存の purpose_ref を保つ。
    """
    if not ref:
        raise ValueError("ref is required")
    now = _now_epoch()
    existing = _get_item(conn, ref)
    if existing is not None:
        new_purpose = purpose_ref if purpose_ref is not None else existing.purpose_ref
        conn.execute(
            "UPDATE desk_items SET last_touched_at = ?, purpose_ref = ? WHERE ref = ?",
            (now, new_purpose, ref),
        )
        conn.commit()
        return DeskItem(
            ref=ref, opened_at=existing.opened_at, last_touched_at=now,
            purpose_ref=new_purpose,
        )
    conn.execute(
        "INSERT INTO desk_items (ref, opened_at, last_touched_at, purpose_ref) "
        "VALUES (?, ?, ?, ?)",
        (ref, now, now, purpose_ref),
    )
    conn.commit()
    return DeskItem(ref=ref, opened_at=now, last_touched_at=now, purpose_ref=purpose_ref)


def close_item(conn: sqlite3.Connection, ref: str) -> bool:
    """ページを机から閉じる (棚に戻す)。開いていれば True。"""
    cur = conn.execute("DELETE FROM desk_items WHERE ref = ?", (ref,))
    conn.commit()
    return cur.rowcount > 0


def touch_item(conn: sqlite3.Connection, ref: str) -> Optional[DeskItem]:
    """机に開いている対象への参照 (read/write/clip 等) を touch する。

    開いていなければ何もせず None を返す (touch は「既に開いているものを
    LRU の先頭から遠ざける」操作であり、勝手に開きはしない)。
    """
    now = _now_epoch()
    cur = conn.execute(
        "UPDATE desk_items SET last_touched_at = ? WHERE ref = ?", (now, ref)
    )
    if cur.rowcount == 0:
        return None
    conn.commit()
    return _get_item(conn, ref)


def list_open(conn: sqlite3.Connection) -> List[DeskItem]:
    """机に開いている全ページ (opened_at 昇順)。"""
    cur = conn.execute(f"SELECT {_DESK_COLS} FROM desk_items ORDER BY opened_at ASC")
    return [_row_to_item(row) for row in cur.fetchall()]


def evict_lru(
    conn: sqlite3.Connection,
    budget_chars: int,
    size_resolver: Callable[[str], int],
    *,
    keep_ref: Optional[str] = None,
) -> List[str]:
    """机の予算を超えている間、最も長く触られていないページから自動で閉じる。

    Args:
        budget_chars: 机の文字数予算。
        size_resolver: ``ref`` → 現在の本文文字数を返す callable。呼び出し側の
            責務 (Atlas ファサードが地図の種類ごとに解決する)。
        keep_ref: 同一呼び出し内では絶対に追い出さない ref (いま open した本人)。
            「開きました」の直後に「棚に戻しました」が同居する矛盾を防ぐ。
            予算より大きい単独ページは机を独占して溢れたまま残る (人間の机に
            広げた大判図面と同じ。次の open/evict で通常の LRU 対象に戻る)。

    Returns:
        追い出された (閉じられた) ref のリスト (閉じた順)。
    """
    items = list_open(conn)
    # LRU 順 (last_touched_at 昇順) に並べ替えて評価する。
    items.sort(key=lambda it: it.last_touched_at)
    sizes = {item.ref: max(0, int(size_resolver(item.ref) or 0)) for item in items}
    total = sum(sizes.values())

    evicted: List[str] = []
    for item in items:
        if total <= budget_chars:
            break
        if keep_ref is not None and item.ref == keep_ref:
            continue
        close_item(conn, item.ref)
        total -= sizes.get(item.ref, 0)
        evicted.append(item.ref)
    return evicted
