#!/usr/bin/env python
"""既存の cities/<city>/buildings/<bid>/log.json を building_messages テーブルへ取り込む。

Phase 1 dual-write 開始後、 過去ログを DB 側に反映するための一回限り (再実行
可能) のマイグレーション。

設計判断:
- **新規連番採番**: JSON 出現順で seq を 1, 2, 3, ... と新採番する。 元の log.json
  の seq は ``legacy_seq`` カラムに保存。 これにより旧 OccupancyManager の
  seq なし host event 痕跡で生じた seq 重複 (1012 件) を全件保存できる。
- **AddonMessageMetadata 一括 UPDATE**: 元 message_id (``building_id:旧seq``) は
  ``legacy_message_id`` カラムに保存し、 取り込み完了後に AddonMessageMetadata
  テーブルの message_id を旧→新へ一括書き換えする。 これで TTS 音声等の
  紐付けが Phase 2 で読み出しを DB に切り替えた後も維持される。
- **冪等性**: 既に取り込み済みの building (= building_messages に 1 件以上ある)
  は skip する。 部分取り込み済みは想定しない (= 全件 INSERT or 全件 skip)。

Usage:
    python scripts/migrate_building_logs_to_db.py [--dry-run]
                                                  [--city CITY_NAME]
                                                  [--building-id BUILDING_ID]
                                                  [--quiet]

詳細: docs/intent/building_memory_unified.md
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saiverse.data_paths import get_saiverse_home  # noqa: E402
from database.session import SessionLocal, engine as _default_engine  # noqa: E402
from database.models import Base, BuildingMessage, AddonMessageMetadata  # noqa: E402
from database.building_messages import serialize_building_message  # noqa: E402

LOGGER = logging.getLogger("migrate_building_logs")


def _ensure_schema(bound_engine) -> None:
    """building_messages テーブルとカラム / インデックスが揃っていることを保証する。
    See database.schema_sync.ensure_table_columns_indexes for details."""
    from database.schema_sync import ensure_table_columns_indexes
    ensure_table_columns_indexes(bound_engine, BuildingMessage.__table__, LOGGER)


@dataclass
class MigrationStats:
    buildings_scanned: int = 0
    buildings_skipped_quarantined: int = 0
    buildings_skipped_unreadable: int = 0
    buildings_skipped_already_migrated: int = 0
    messages_seen: int = 0
    messages_inserted: int = 0
    messages_skipped_invalid: int = 0
    addon_metadata_updated: int = 0
    addon_metadata_not_found: int = 0
    addon_metadata_skipped_conflict: int = 0
    # (building_id, legacy_message_id) -> new message_id ; 重複時は最初に出会った方
    legacy_message_id_map: Dict[Tuple[str, str], str] = field(default_factory=dict)


def find_log_files(
    saiverse_home: Path,
    *,
    city_filter: Optional[str] = None,
    building_filter: Optional[str] = None,
) -> List[Path]:
    cities_root = saiverse_home / "cities"
    if not cities_root.exists():
        LOGGER.warning("cities ディレクトリが存在しません: %s", cities_root)
        return []
    found: List[Path] = []
    for city_dir in sorted(cities_root.iterdir()):
        if not city_dir.is_dir():
            continue
        if city_filter and city_dir.name != city_filter:
            continue
        buildings_root = city_dir / "buildings"
        if not buildings_root.exists():
            continue
        for building_dir in sorted(buildings_root.iterdir()):
            if not building_dir.is_dir():
                continue
            if building_filter and building_dir.name != building_filter:
                continue
            log_path = building_dir / "log.json"
            if log_path.exists():
                found.append(log_path)
    return found


def load_log(log_path: Path, stats: MigrationStats) -> Optional[List[dict]]:
    try:
        if log_path.stat().st_size == 0:
            LOGGER.warning("  0バイト — スキップ: %s", log_path)
            stats.buildings_skipped_unreadable += 1
            return None
    except OSError as e:
        LOGGER.warning("  stat 失敗 — スキップ: %s (%s)", log_path, e)
        stats.buildings_skipped_unreadable += 1
        return None
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        LOGGER.warning("  JSON parse 失敗 — スキップ: %s (%s)", log_path, e)
        stats.buildings_skipped_unreadable += 1
        return None
    if not isinstance(data, list):
        LOGGER.warning("  list ではない — スキップ: %s (type=%s)", log_path, type(data).__name__)
        stats.buildings_skipped_unreadable += 1
        return None
    return data


def is_quarantined(log_path: Path) -> bool:
    parent = log_path.parent
    for sibling in parent.glob("log.json.corrupted_*"):
        if sibling.is_file():
            return True
    return False


def migrate_building(
    building_id: str,
    messages: Iterable[dict],
    stats: MigrationStats,
    *,
    dry_run: bool,
) -> None:
    """1 building 分のメッセージを全件 DB に取り込む (連番採番)。

    冪等性: 当該 building の building_messages が既に存在する場合は skip。
    部分 migration 済みは想定しない (= 全件 or 何もしない)。
    """
    messages_list = list(messages)
    stats.messages_seen += len(messages_list)

    db = SessionLocal()
    try:
        existing_count = (
            db.query(BuildingMessage).filter_by(building_id=building_id).count()
        )
        if existing_count > 0:
            LOGGER.info(
                "  %s: 既に %d 件取り込み済み — skip",
                building_id, existing_count,
            )
            stats.buildings_skipped_already_migrated += 1
            return

        local_inserted = 0
        local_invalid = 0
        new_seq = 0
        for msg in messages_list:
            new_seq += 1
            new_message_id = f"{building_id}:{new_seq}"
            legacy_msg_id = msg.get("message_id")
            legacy_seq_raw = msg.get("seq")
            try:
                legacy_seq_int: Optional[int] = (
                    int(legacy_seq_raw) if legacy_seq_raw is not None else None
                )
            except (TypeError, ValueError):
                legacy_seq_int = None

            # serialize_building_message は metadata.event を構造化分離してくれる。
            # 新 seq / 新 message_id で上書きする。
            record = serialize_building_message(building_id, msg)
            record["seq"] = new_seq
            record["message_id"] = new_message_id
            record["legacy_seq"] = legacy_seq_int
            record["legacy_message_id"] = legacy_msg_id

            # legacy_message_id → 新 message_id マッピングを記録
            # (重複時は最初に出会ったものを保持。 AddonMessageMetadata が
            # 1 つの旧 message_id 1 つにしか紐付かない前提と整合)
            if isinstance(legacy_msg_id, str) and legacy_msg_id:
                map_key = (building_id, legacy_msg_id)
                if map_key not in stats.legacy_message_id_map:
                    stats.legacy_message_id_map[map_key] = new_message_id

            if dry_run:
                local_inserted += 1
                continue
            try:
                obj = BuildingMessage(**record)
                db.add(obj)
                db.flush()
                local_inserted += 1
            except Exception as e:
                db.rollback()
                LOGGER.warning(
                    "  INSERT 失敗 — skip: bid=%s legacy_seq=%s (%s)",
                    building_id, legacy_seq_int, e,
                )
                local_invalid += 1
                # rollback 後は seq counter が狂うので、 残りの行は new_seq を
                # 連続させたまま試みる (= 失敗行の seq 番号は欠番になる)。
                # rollback 直後は同 session で再 INSERT 可能。

        if not dry_run:
            db.commit()

        stats.messages_inserted += local_inserted
        stats.messages_skipped_invalid += local_invalid

        LOGGER.info(
            "  %s: 取り込み=%d / 不正/エラー=%d",
            building_id, local_inserted, local_invalid,
        )
    finally:
        db.close()


def update_addon_metadata(stats: MigrationStats, *, dry_run: bool) -> None:
    """legacy_message_id マッピングに基づき AddonMessageMetadata.message_id を一括 UPDATE。

    旧 message_id 空間と新採番空間は当然被るため、 逐次 UPDATE では中間状態で
    UNIQUE(message_id, addon_name, key) 違反になる。 二段階で実行する:

    Phase 1: 対象行を一意な一時 message_id (``__migrating_<id>__``) に退避
    Phase 2: 一時 message_id を最終 new_message_id に書き換え

    Phase 2 で「target に既存行 (= migration 対象外で偶然同じ new_msg_id を持つ行)」
    があれば skip し、 統計に記録する (現状の本番データではこのケースは発生しない
    想定だが、 念のため衝突しないことを保証する)。
    """
    if not stats.legacy_message_id_map:
        LOGGER.info("AddonMessageMetadata UPDATE: マッピングが空 — skip")
        return

    LOGGER.info(
        "AddonMessageMetadata UPDATE 開始 (マッピング件数=%d, dry_run=%s)",
        len(stats.legacy_message_id_map), dry_run,
    )

    db = SessionLocal()
    try:
        # plans: 「この行をこの new_msg_id に動かす」 リスト
        plans: List[Tuple[int, str, str, str]] = []  # (row_id, new_msg_id, addon_name, key)
        not_found = 0
        for (_building_id, legacy_msg_id), new_message_id in stats.legacy_message_id_map.items():
            if legacy_msg_id == new_message_id:
                continue
            rows = db.query(AddonMessageMetadata).filter(
                AddonMessageMetadata.message_id == legacy_msg_id
            ).all()
            if not rows:
                not_found += 1
                continue
            for row in rows:
                plans.append((row.id, new_message_id, row.addon_name, row.key))

        if dry_run:
            stats.addon_metadata_updated = len(plans)
            stats.addon_metadata_not_found = not_found
            LOGGER.info(
                "AddonMessageMetadata UPDATE (dry-run): 更新予定=%d / 該当なし legacy_id=%d",
                len(plans), not_found,
            )
            return

        # Phase 1: 対象を一意な一時 ID に退避
        for row_id, _new_msg, _addon, _key in plans:
            db.query(AddonMessageMetadata).filter_by(id=row_id).update(
                {"message_id": f"__migrating_{row_id}__"},
                synchronize_session=False,
            )
        db.flush()
        LOGGER.info("Phase 1 (退避) 完了: %d 行", len(plans))

        # Phase 2: 一時 ID から最終 new_msg_id へ。 衝突回避
        updated = 0
        skipped_conflict = 0
        for row_id, new_msg_id, addon_name, key in plans:
            # この時点で同じ (new_msg_id, addon_name, key) を既に持つ行があるかを確認。
            # ある場合は、 現行の行 (= migration 対象) を捨てる (= 既存を尊重)。
            # ただし「既存」 が他の Phase 2 で同じ new_msg_id を持つことになった
            # ものではないか、 同一性を見る (= 自分自身ではない)。
            existing = db.query(AddonMessageMetadata).filter(
                AddonMessageMetadata.message_id == new_msg_id,
                AddonMessageMetadata.addon_name == addon_name,
                AddonMessageMetadata.key == key,
                AddonMessageMetadata.id != row_id,
            ).first()
            if existing is not None:
                # 衝突: 自分は削除して既存を残す
                db.query(AddonMessageMetadata).filter_by(id=row_id).delete(
                    synchronize_session=False
                )
                skipped_conflict += 1
                db.flush()
                continue
            db.query(AddonMessageMetadata).filter_by(id=row_id).update(
                {"message_id": new_msg_id}, synchronize_session=False
            )
            updated += 1

        db.commit()
        stats.addon_metadata_updated = updated
        stats.addon_metadata_not_found = not_found
        stats.addon_metadata_skipped_conflict = skipped_conflict
        LOGGER.info(
            "AddonMessageMetadata UPDATE 完了: 更新=%d / 衝突 skip=%d / 該当なし legacy_id=%d",
            updated, skipped_conflict, not_found,
        )
    finally:
        db.close()


def rebuild_legacy_map_from_db(stats: MigrationStats) -> None:
    """既存 building_messages から legacy_message_id → new message_id を SELECT で再構築。

    再実行時 (= 全 building が既取り込みで migrate_building が skip される) でも
    update_addon_metadata が機能するように、 マッピングを DB から復元する。
    """
    db = SessionLocal()
    try:
        rows = db.query(
            BuildingMessage.building_id,
            BuildingMessage.message_id,
            BuildingMessage.legacy_message_id,
        ).filter(BuildingMessage.legacy_message_id.isnot(None)).all()
        rebuilt = 0
        for building_id, message_id, legacy_msg_id in rows:
            if not legacy_msg_id or not message_id:
                continue
            key = (building_id, legacy_msg_id)
            if key in stats.legacy_message_id_map:
                continue
            stats.legacy_message_id_map[key] = message_id
            rebuilt += 1
        LOGGER.info("legacy_message_id_map 再構築: %d 件追加 (DB から復元)", rebuilt)
    finally:
        db.close()


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
    # main.py の needs_migration 経路 (全 schema rebuild) を待たずに軽量に追従する。
    try:
        bound_engine = SessionLocal.kw.get("bind") if hasattr(SessionLocal, "kw") else None
        if bound_engine is None:
            bound_engine = _default_engine
        _ensure_schema(bound_engine)
    except Exception as e:
        LOGGER.warning("building_messages スキーマ整備に失敗: %s", e)

    log_files = find_log_files(
        saiverse_home,
        city_filter=args.city,
        building_filter=args.building_id,
    )
    LOGGER.info("対象 log.json: %d 件", len(log_files))

    stats = MigrationStats()

    for log_path in log_files:
        building_id = log_path.parent.name
        stats.buildings_scanned += 1
        LOGGER.info("→ %s", log_path.relative_to(saiverse_home))

        if is_quarantined(log_path):
            LOGGER.warning("  隔離 (corrupted sibling 有) — スキップ")
            stats.buildings_skipped_quarantined += 1
            continue

        messages = load_log(log_path, stats)
        if messages is None:
            continue
        migrate_building(building_id, messages, stats, dry_run=args.dry_run)

    # 再実行時 (全 building が既取り込みで map が空) のために、 既存 building_messages
    # から legacy_message_id → 新 message_id を再構築する。
    if not stats.legacy_message_id_map:
        rebuild_legacy_map_from_db(stats)

    update_addon_metadata(stats, dry_run=args.dry_run)

    LOGGER.warning("")
    LOGGER.warning("=== summary ===")
    LOGGER.warning("buildings_scanned             : %d", stats.buildings_scanned)
    LOGGER.warning("buildings_skipped_quarantined : %d", stats.buildings_skipped_quarantined)
    LOGGER.warning("buildings_skipped_unreadable  : %d", stats.buildings_skipped_unreadable)
    LOGGER.warning("buildings_skipped_migrated    : %d", stats.buildings_skipped_already_migrated)
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
