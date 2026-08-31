"""active occupancy (EXIT_TIMESTAMP IS NULL) の重複修復と部分一意 index。

分離監査 P1-2 (W7 柱5): 「AIID ごとに active 行は高々 1 行」を DB 制約として
持たせ、既存の重複行は index 作成前に明示修復する。設計の正典は
docs/handoff/2026-07-21_w7_location_occupancy_handoff.md D1。

利用箇所:
- database/migrate.py `ensure_active_occupancy_unique` — 起動時の修復 + index 作成
- manager/persona.py `_load_occupancy_from_db` — startup checker が重複検出時に
  明示 tx で修復 (P2-2)

index はモデル metadata には載せない (全書換 migration が「修復前の重複を含む
旧 DB」のコピーで unique 違反を起こし起動不能になるため)。全書換で index が
消えても、次回起動の ensure が再作成する。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text

ACTIVE_OCCUPANCY_UNIQUE_INDEX = "uq_occupancy_active_ai"

_LOGGER = logging.getLogger(__name__)

# main.py の起動前修復 (ensure_active_occupancy_unique) の明細を manager の
# startup_warnings へ引き継ぐための同一プロセス内ステージング (Codex 第四巡 P2:
# 起動前に修復すると後段の checker が重複を見られず、行を自動選択して閉じた
# 事実が /config/startup-warnings に現れない)。
_STARTUP_REPAIRS: List[Dict[str, Any]] = []


def record_startup_repairs(repairs: List[Dict[str, Any]]) -> None:
    """起動前修復の明細を積む (manager 初期化時に consume される)。"""
    _STARTUP_REPAIRS.extend(repairs)


def consume_startup_repairs() -> List[Dict[str, Any]]:
    """積まれた起動前修復の明細を取り出してクリアする。"""
    repairs = list(_STARTUP_REPAIRS)
    _STARTUP_REPAIRS.clear()
    return repairs


def repair_duplicate_active_occupancy(
    executor: Any,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """AIID ごとの active 行を高々 1 行へ修復する。

    canonical = ENTRY_TIMESTAMP 最新 (同時刻は ID 最大)。それ以外の active 行に
    修復時刻の EXIT_TIMESTAMP を刻んで close する。

    executor は SQLAlchemy Connection / Session (``execute(text(...))`` が使える
    もの)。commit / rollback は呼び出し側の責務 (明示 tx の内側で呼ぶこと)。

    Returns:
        修復明細のリスト (監査ログ・startup_warnings 用)。
        ``[{"ai_id", "canonical_building_id", "closed_rows": [(row_id, building_id), ...]}]``
    """
    log = logger or _LOGGER
    dup_ai_ids = [
        row[0]
        for row in executor.execute(text(
            "SELECT AIID FROM building_occupancy_log "
            "WHERE EXIT_TIMESTAMP IS NULL "
            "GROUP BY AIID HAVING COUNT(*) > 1"
        )).fetchall()
    ]
    repairs: List[Dict[str, Any]] = []
    if not dup_ai_ids:
        return repairs

    # SQLAlchemy DateTime (SQLite) と同じ 'YYYY-MM-DD HH:MM:SS.ffffff' 形式
    repaired_at = datetime.now().isoformat(sep=" ")
    for ai_id in dup_ai_ids:
        # canonical 選択は「参照整合な行 (Building が実在し City も一致) を優先し、
        # その中で ENTRY_TIMESTAMP 最新」。最新行が削除済み/別 City の Building を
        # 指す場合に有効な旧行を潰さない (2026-07-21 Codex 第七巡 P1 —
        # そうしないと startup checker の無効行 close と合わさって所在地が
        # 全喪失する)。全行無効なら従来どおり最新を残す (checker 側が close)。
        rows = executor.execute(text(
            "SELECT o.ID, o.BUILDINGID, "
            "  EXISTS(SELECT 1 FROM building b "
            "         WHERE b.BUILDINGID = o.BUILDINGID "
            "           AND b.CITYID = o.CITYID) AS ref_valid "
            "FROM building_occupancy_log o "
            "WHERE o.AIID = :ai AND o.EXIT_TIMESTAMP IS NULL "
            "ORDER BY ref_valid DESC, o.ENTRY_TIMESTAMP DESC, o.ID DESC"
        ), {"ai": ai_id}).fetchall()
        canonical_id, canonical_bid = rows[0][0], rows[0][1]
        closed: List[tuple] = []
        for row_id, building_id, _ref_valid in rows[1:]:
            executor.execute(text(
                "UPDATE building_occupancy_log SET EXIT_TIMESTAMP = :ts "
                "WHERE ID = :row_id"
            ), {"ts": repaired_at, "row_id": row_id})
            closed.append((row_id, building_id))
        repairs.append({
            "ai_id": ai_id,
            "canonical_building_id": canonical_bid,
            "closed_rows": closed,
        })
        log.warning(
            "occupancy repair: AI '%s' の active 行 %d 件を修復 — "
            "canonical=row %s (%s) を残し、%s を close しました",
            ai_id, len(rows), canonical_id, canonical_bid,
            ", ".join(f"row {rid} ({bid})" for rid, bid in closed),
        )
    return repairs


def ensure_active_occupancy_unique_index(executor: Any) -> None:
    """部分一意 index を冪等に作成する (重複修復の後に呼ぶこと)。"""
    executor.execute(text(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_OCCUPANCY_UNIQUE_INDEX} "
        "ON building_occupancy_log (AIID) WHERE EXIT_TIMESTAMP IS NULL"
    ))
