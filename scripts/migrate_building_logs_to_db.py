#!/usr/bin/env python
"""既存の cities/<city>/buildings/<bid>/log.json を building_messages テーブルへ取り込む。

実体は ``saiverse/legacy_log_import.py`` (バージョンアップグレード経路と共用)。
このスクリプトは個別復旧・再実行用の CLI の薄い入口。

スキップ判定は「現物の log.json が読めるか」だけで行う。隔離マーカー
(``log.json.corrupted_*``) の有無では判定しない — マーカーは事故時の退避物で、
修復後の log.json の健全性について何も語らないため。

Usage:
    python scripts/migrate_building_logs_to_db.py [--dry-run]
                                                  [--city CITY_NAME]
                                                  [--building-id BUILDING_ID]
                                                  [--quiet]

詳細: docs/intent/building_memory_unified.md
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saiverse.data_paths import get_saiverse_home  # noqa: E402
from database.session import SessionLocal, engine as _default_engine  # noqa: E402
from database.models import BuildingMessage  # noqa: E402
from saiverse.legacy_log_import import import_building_logs  # noqa: E402

LOGGER = logging.getLogger("migrate_building_logs")


def _ensure_schema(bound_engine) -> None:
    """building_messages テーブルとカラム / インデックスが揃っていることを保証する。
    See database.schema_sync.ensure_table_columns_indexes for details."""
    from database.schema_sync import ensure_table_columns_indexes
    ensure_table_columns_indexes(bound_engine, BuildingMessage.__table__, LOGGER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="DB には書かず件数だけ表示")
    parser.add_argument("--city", type=str, default=None, help="特定 city のみを対象")
    parser.add_argument("--building-id", type=str, default=None, help="特定 building のみを対象")
    parser.add_argument("--quiet", action="store_true", help="building 単位ログ抑制")
    args = parser.parse_args()

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")

    saiverse_home = get_saiverse_home()
    LOGGER.info("SAIVerse home: %s", saiverse_home)
    LOGGER.info("dry-run: %s", args.dry_run)
    if args.city:
        LOGGER.info("city filter: %s", args.city)
    if args.building_id:
        LOGGER.info("building filter: %s", args.building_id)

    # スキーマ整備: テーブル未作成なら create、 既存だがカラム不足なら ALTER で追加。
    try:
        bound_engine = SessionLocal.kw.get("bind") if hasattr(SessionLocal, "kw") else None
        if bound_engine is None:
            bound_engine = _default_engine
        _ensure_schema(bound_engine)
    except Exception as e:
        LOGGER.warning("building_messages スキーマ整備に失敗: %s", e)

    db = SessionLocal()
    try:
        # commit_per_building=True: 部屋ごとに確定させる。後半の部屋で失敗しても
        # 先に取り込めた部屋の履歴を巻き戻さない (個別復旧の入口なので、途中まで
        # でも進んだ分は残す方が復旧しやすい)。
        stats = import_building_logs(
            db,
            saiverse_home,
            city_filter=args.city,
            building_filter=args.building_id,
            dry_run=args.dry_run,
            commit_per_building=True,
        )
    finally:
        db.close()

    LOGGER.warning("")
    LOGGER.warning("=== summary ===")
    LOGGER.warning("buildings_scanned             : %d", stats.buildings_scanned)
    LOGGER.warning("buildings_skipped_unreadable  : %d", stats.buildings_skipped_unreadable)
    LOGGER.warning("buildings_skipped_migrated    : %d", stats.buildings_skipped_already_migrated)
    LOGGER.warning("buildings_skipped_live_rows   : %d", stats.buildings_skipped_live_rows)
    LOGGER.warning("buildings_failed              : %d", stats.buildings_failed)
    LOGGER.warning("messages_seen                 : %d", stats.messages_seen)
    LOGGER.warning("messages_inserted             : %d", stats.messages_inserted)
    LOGGER.warning("messages_skipped_invalid      : %d", stats.messages_skipped_invalid)
    LOGGER.warning("addon_metadata_updated        : %d", stats.addon_metadata_updated)
    LOGGER.warning("addon_metadata_not_found      : %d", stats.addon_metadata_not_found)
    LOGGER.warning("addon_metadata_skipped_conflict: %d", stats.addon_metadata_skipped_conflict)
    if args.dry_run:
        LOGGER.warning("(dry-run: DB に書き込みは行っていません)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
