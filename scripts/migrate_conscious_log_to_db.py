#!/usr/bin/env python
"""既存の conscious_log.json から persona_pulse_cursor テーブルへ移管する。

実体は ``saiverse/legacy_log_import.py`` (バージョンアップグレード経路と共用)。
このスクリプトは個別復旧・再実行用の CLI の薄い入口。

リマップロジック:
- ``pulse_cursor_format=="seq"``:
    旧 cursor 値 = 旧 seq とみなし、 ``building_messages.legacy_seq=旧値`` の行の
    **最大** 新 seq を取得 → 新 cursor とする (= 「その seq までを既読」 を最大限尊重)
- ``pulse_cursor_format=="count"`` (旧来 default):
    旧 cursor 値 = N 件目を表す。 building log 取り込みは JSON 出現順で 1, 2, 3,... と
    新採番したため、 **新 seq = N** で対応する。
- 該当 legacy_seq が DB にない場合 (fallback): cursor = 0 (= 全未読)。
    ingested_by フラグが既処理マーカーとして機能するため、 過去メッセージの
    重複再 ingest は防がれる。

⚠️ 前提: building log の取り込み (migrate_building_logs_to_db.py) が先。
   legacy_seq を引いてリマップするため、順序が逆だと cursor=0 (全未読) に落ちる。

Usage:
    python scripts/migrate_conscious_log_to_db.py [--dry-run] [--persona-id PID]
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
from database.models import BuildingMessage, PersonaPulseCursor  # noqa: E402
from database.schema_sync import ensure_table_columns_indexes  # noqa: E402
from saiverse.legacy_log_import import CursorStats, migrate_persona_cursors  # noqa: E402

LOGGER = logging.getLogger("migrate_conscious_log")


def _ensure_schema() -> None:
    bound_engine = SessionLocal.kw.get("bind") if hasattr(SessionLocal, "kw") else None
    if bound_engine is None:
        bound_engine = _default_engine
    ensure_table_columns_indexes(bound_engine, PersonaPulseCursor.__table__, LOGGER)
    ensure_table_columns_indexes(bound_engine, BuildingMessage.__table__, LOGGER)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--persona-id", type=str, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=log_level, format="%(message)s")

    saiverse_home = get_saiverse_home()
    LOGGER.info("SAIVerse home: %s", saiverse_home)
    LOGGER.info("dry-run: %s", args.dry_run)

    _ensure_schema()

    personas_root = saiverse_home / "personas"
    if not personas_root.exists():
        LOGGER.warning("personas/ ディレクトリが存在しません: %s", personas_root)
        return 0

    stats = CursorStats()
    failed = 0
    db = SessionLocal()
    try:
        # ペルソナごとに確定させる。1 人分の失敗で、先に移せた人の cursor まで
        # 巻き戻さない (巻き戻すと、その人たちが過去ログを再消化しに行く)。
        for persona_dir in sorted(personas_root.iterdir()):
            if not persona_dir.is_dir():
                continue
            if args.persona_id and persona_dir.name != args.persona_id:
                continue
            LOGGER.info("→ %s", persona_dir.name)
            try:
                migrate_persona_cursors(db, persona_dir, stats, dry_run=args.dry_run)
                if not args.dry_run:
                    db.commit()
            except Exception:
                LOGGER.error(
                    "  %s: cursor 移管に失敗 — このペルソナだけ巻き戻して続行",
                    persona_dir.name, exc_info=True,
                )
                db.rollback()
                failed += 1
    finally:
        db.close()

    LOGGER.warning("")
    LOGGER.warning("=== summary ===")
    LOGGER.warning("personas_scanned        : %d", stats.personas_scanned)
    LOGGER.warning("personas_processed      : %d", stats.personas_processed)
    LOGGER.warning("personas_skipped_missing: %d", stats.personas_skipped_missing)
    LOGGER.warning("personas_failed         : %d", failed)
    LOGGER.warning("cursors_inserted        : %d", stats.cursors_inserted)
    LOGGER.warning("cursors_updated         : %d", stats.cursors_updated)
    LOGGER.warning("cursors_zero_fallback   : %d", stats.cursors_zero_fallback)
    if args.dry_run:
        LOGGER.warning("(dry-run: DB に書き込みは行っていません)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
