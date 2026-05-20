#!/usr/bin/env python
"""既存の conscious_log.json から persona_pulse_cursor テーブルへ移管する。

各 persona の ``~/.saiverse/personas/<id>/conscious_log.json`` を読み、
``pulse_cursors`` (per-building cursor) を新 seq 空間にリマップして
``persona_pulse_cursor`` テーブルに INSERT する。

リマップロジック:
- ``pulse_cursor_format=="seq"``:
    旧 cursor 値 = 旧 seq とみなし、 ``building_messages.legacy_seq=旧値`` の行の
    **最大** 新 seq を取得 → 新 cursor とする (= 「その seq までを既読」 を最大限尊重)
- ``pulse_cursor_format=="count"`` (旧来 default):
    旧 cursor 値 = N 件目を表す。 migration script は JSON 出現順で 1, 2, 3,... と
    新採番したため、 **新 seq = N** で対応する。
- 該当 legacy_seq が DB にない場合 (fallback): cursor = 0 (= 全未読)。
    ingested_by フラグが既処理マーカーとして機能するため、 過去メッセージの
    重複再 ingest は防がれる。

Usage:
    python scripts/migrate_conscious_log_to_db.py [--dry-run] [--persona-id PID]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saiverse.data_paths import get_saiverse_home  # noqa: E402
from database.session import SessionLocal, engine as _default_engine  # noqa: E402
from database.models import Base, BuildingMessage, PersonaPulseCursor  # noqa: E402
from database.schema_sync import ensure_table_columns_indexes  # noqa: E402
from sqlalchemy import func  # noqa: E402

LOGGER = logging.getLogger("migrate_conscious_log")


@dataclass
class Stats:
    personas_scanned: int = 0
    personas_skipped_missing: int = 0
    personas_processed: int = 0
    cursors_inserted: int = 0
    cursors_updated: int = 0
    cursors_zero_fallback: int = 0


def _ensure_schema() -> None:
    bound_engine = SessionLocal.kw.get("bind") if hasattr(SessionLocal, "kw") else None
    if bound_engine is None:
        bound_engine = _default_engine
    ensure_table_columns_indexes(bound_engine, PersonaPulseCursor.__table__, LOGGER)
    ensure_table_columns_indexes(bound_engine, BuildingMessage.__table__, LOGGER)


def _remap_seq_format(db, building_id: str, legacy_value: int) -> int:
    """``pulse_cursor_format='seq'`` の旧 cursor 値を新 seq にリマップ。

    legacy_seq=旧値 の行の MAX(新 seq) を取得。 該当なしなら 0 (fallback)。
    """
    val = db.query(func.max(BuildingMessage.seq)).filter(
        BuildingMessage.building_id == building_id,
        BuildingMessage.legacy_seq == legacy_value,
    ).scalar()
    if val is not None:
        return int(val)
    # Fallback: 同じ seq を持つ行がない時、 「旧 seq 値 以下の legacy_seq を持つ
    # 最大の新 seq」 を試す (= 近傍ヒット)
    val = db.query(func.max(BuildingMessage.seq)).filter(
        BuildingMessage.building_id == building_id,
        BuildingMessage.legacy_seq <= legacy_value,
        BuildingMessage.legacy_seq.isnot(None),
    ).scalar()
    if val is not None:
        return int(val)
    return 0


def _remap_count_format(db, building_id: str, count_value: int) -> int:
    """``pulse_cursor_format='count'`` の旧 cursor 値を新 seq にリマップ。

    旧 N 件目 → migration が JSON 順で連番採番したので新 seq = N で対応。
    ただし DB に N 件未満しかない場合は MAX(seq) でクランプ。
    """
    if count_value <= 0:
        return 0
    max_seq = db.query(func.max(BuildingMessage.seq)).filter_by(
        building_id=building_id
    ).scalar() or 0
    return min(count_value, int(max_seq))


def _upsert_cursor(
    db, persona_id: str, building_id: str, cursor_seq: int, entry_marker_seq: int,
    stats: Stats, dry_run: bool,
) -> None:
    row = db.query(PersonaPulseCursor).filter_by(
        PERSONA_ID=persona_id, BUILDING_ID=building_id
    ).first()
    if row is None:
        if not dry_run:
            db.add(PersonaPulseCursor(
                PERSONA_ID=persona_id,
                BUILDING_ID=building_id,
                CURSOR_SEQ=cursor_seq,
                ENTRY_MARKER_SEQ=entry_marker_seq,
            ))
        stats.cursors_inserted += 1
    else:
        if not dry_run:
            row.CURSOR_SEQ = cursor_seq
            row.ENTRY_MARKER_SEQ = entry_marker_seq
        stats.cursors_updated += 1


def migrate_persona(persona_dir: Path, stats: Stats, *, dry_run: bool) -> None:
    persona_id = persona_dir.name
    stats.personas_scanned += 1
    cl_path = persona_dir / "conscious_log.json"
    if not cl_path.exists():
        LOGGER.debug("  %s: conscious_log.json なし — skip", persona_id)
        stats.personas_skipped_missing += 1
        return

    try:
        data = json.loads(cl_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        LOGGER.warning("  %s: JSON parse 失敗 — skip (%s)", persona_id, e)
        stats.personas_skipped_missing += 1
        return
    if not isinstance(data, dict):
        LOGGER.warning("  %s: dict ではない — skip", persona_id)
        stats.personas_skipped_missing += 1
        return

    raw_cursors = data.get("pulse_cursors") or data.get("pulse_indices") or {}
    if not isinstance(raw_cursors, dict):
        raw_cursors = {}
    fmt = data.get("pulse_cursor_format")
    fmt = fmt if isinstance(fmt, str) and fmt in ("seq", "count") else "count"

    if not raw_cursors:
        LOGGER.debug("  %s: cursor 空 — skip", persona_id)
        stats.personas_skipped_missing += 1
        return

    db = SessionLocal()
    try:
        for building_id, raw_value in raw_cursors.items():
            try:
                value_int = int(raw_value)
            except (TypeError, ValueError):
                LOGGER.warning(
                    "  %s/%s: cursor 値 %r が parse 失敗 — 0 (全未読) に",
                    persona_id, building_id, raw_value,
                )
                new_cursor = 0
                stats.cursors_zero_fallback += 1
            else:
                if fmt == "seq":
                    new_cursor = _remap_seq_format(db, building_id, value_int)
                else:
                    new_cursor = _remap_count_format(db, building_id, value_int)
                if new_cursor == 0:
                    stats.cursors_zero_fallback += 1
            # entry_marker は cursor と同じで初期化 (旧 design では別だが、 cursor で十分)
            _upsert_cursor(
                db, persona_id, building_id, new_cursor, new_cursor,
                stats, dry_run,
            )

        if not dry_run:
            db.commit()
        stats.personas_processed += 1
        LOGGER.info(
            "  %s: 取り込み完了 (cursors=%d, fmt=%s)",
            persona_id, len(raw_cursors), fmt,
        )
    finally:
        db.close()


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

    stats = Stats()
    for persona_dir in sorted(personas_root.iterdir()):
        if not persona_dir.is_dir():
            continue
        if args.persona_id and persona_dir.name != args.persona_id:
            continue
        LOGGER.info("→ %s", persona_dir.name)
        migrate_persona(persona_dir, stats, dry_run=args.dry_run)

    LOGGER.warning("")
    LOGGER.warning("=== summary ===")
    LOGGER.warning("personas_scanned        : %d", stats.personas_scanned)
    LOGGER.warning("personas_processed      : %d", stats.personas_processed)
    LOGGER.warning("personas_skipped_missing: %d", stats.personas_skipped_missing)
    LOGGER.warning("cursors_inserted        : %d", stats.cursors_inserted)
    LOGGER.warning("cursors_updated         : %d", stats.cursors_updated)
    LOGGER.warning("cursors_zero_fallback   : %d", stats.cursors_zero_fallback)
    if args.dry_run:
        LOGGER.warning("(dry-run: DB に書き込みは行っていません)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
