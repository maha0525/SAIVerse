"""旧ファイル形式 (log.json / conscious_log.json) → DB への取り込みロジック。

Building のチャットログは Phase 2+3 (2026-05-20, ec9eba70) で
``cities/<city>/buildings/<bid>/log.json`` から ``building_messages`` テーブルへ、
ペルソナの pulse cursor は ``conscious_log.json`` から ``persona_pulse_cursor``
テーブルへ移行した。本モジュールはその「過去データの取り込み」の実体で、
次の 3 経路から呼ばれる:

1. **バージョンアップグレード** (``saiverse/upgrade_handlers.py`` の dev5 エッジ):
   リリース版 (v0.2.x, log.json 時代) から上がってきた環境で、世界が動き出す前に
   一度だけ自動実行される。リリース後のユーザーが手動スクリプトを知らなくても
   過去ログが失われないための本経路。
2. **起動時の検算** (``manager/initialization.py``): 毎起動、log.json に履歴が
   あるのに DB に取り込み痕跡が無い部屋を探し、見つかれば startup_alerts
   (UI バナー) に載せ続ける。取り込み漏れを沈黙させないための常設の関所。
3. **手動 CLI** (``scripts/migrate_building_logs_to_db.py`` /
   ``scripts/migrate_conscious_log_to_db.py``): 個別復旧・再実行用の薄い入口。

設計上の約束 (docs/intent/building_memory_unified.md):

- **スキップ判定は「現物が読めるか」だけで行う。** 過去の隔離マーカー
  (``log.json.corrupted_*``) の有無では判定しない。マーカーは事故時の退避物で、
  その後修復された log.json の健全性について何も語らないため
  (2026-08-16 テスタロッサの部屋の取り込み漏れの根因)。
- **黙って諦めない。** 取り込めなかった部屋は戻り値の stats / 検算結果に必ず
  現れ、呼び出し側がアラートにする。
- **この関数群は commit しない。** トランザクション境界は呼び出し側が持つ
  (アップグレード経路ではエンティティ単位の commit に相乗りする)。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func

from database.models import AddonMessageMetadata, BuildingMessage, PersonaPulseCursor
from database.building_messages import serialize_building_message

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Building log (log.json → building_messages)
# ---------------------------------------------------------------------------

@dataclass
class MigrationStats:
    buildings_scanned: int = 0
    buildings_skipped_unreadable: int = 0
    buildings_skipped_already_migrated: int = 0
    buildings_skipped_live_rows: int = 0
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


def load_log(log_path: Path) -> Tuple[Optional[List[dict]], Optional[str]]:
    """log.json を読む。戻り値は (messages, 読めない理由)。

    読めない理由 (str) が返るのは 0 バイト / JSON 破損 / 構造異常 のときで、
    その場合 messages は None。
    """
    try:
        if log_path.stat().st_size == 0:
            return None, "0 バイト"
    except OSError as e:
        return None, f"stat 失敗 ({e})"
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, f"JSON parse 失敗 ({e})"
    if not isinstance(data, list):
        return None, f"list ではない (type={type(data).__name__})"
    return data, None


def migrate_building(
    db,
    building_id: str,
    messages: Iterable[dict],
    stats: MigrationStats,
    *,
    dry_run: bool,
) -> str:
    """1 building 分のメッセージを全件 DB に取り込む (連番採番)。commit しない。

    Returns:
        "imported"  ... 取り込みを実行した
        "already"   ... 取り込み痕跡 (legacy_seq 付き行) が既にある — 冪等 skip
        "live_rows" ... 取り込み痕跡は無いが通常経路の行が既にある。ここで取り込むと
                        既存 seq の後ろに過去ログが並んで順序が壊れるため skip。
                        呼び出し側 (起動時の検算) がアラートにする
    """
    messages_list = list(messages)
    stats.messages_seen += len(messages_list)

    imported_count = (
        db.query(BuildingMessage)
        .filter(
            BuildingMessage.building_id == building_id,
            BuildingMessage.legacy_seq.isnot(None),
        )
        .count()
    )
    if imported_count > 0:
        LOGGER.info(
            "  %s: 既に %d 件取り込み済み — skip", building_id, imported_count,
        )
        stats.buildings_skipped_already_migrated += 1
        return "already"

    live_count = (
        db.query(BuildingMessage).filter_by(building_id=building_id).count()
    )
    if live_count > 0:
        LOGGER.warning(
            "  %s: 取り込み痕跡なしで通常経路の行が %d 件 — 過去ログを後付けすると"
            "順序が壊れるため skip (手動対応が必要)",
            building_id, live_count,
        )
        stats.buildings_skipped_live_rows += 1
        return "live_rows"

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
            # SAVEPOINT で行単位に隔離: 失敗行だけ巻き戻し、外側の
            # トランザクション (呼び出し側所有) には影響させない。
            with db.begin_nested():
                db.add(BuildingMessage(**record))
            local_inserted += 1
        except Exception as e:
            LOGGER.warning(
                "  INSERT 失敗 — skip: bid=%s legacy_seq=%s (%s)",
                building_id, legacy_seq_int, e,
            )
            local_invalid += 1

    stats.messages_inserted += local_inserted
    stats.messages_skipped_invalid += local_invalid

    LOGGER.info(
        "  %s: 取り込み=%d / 不正/エラー=%d",
        building_id, local_inserted, local_invalid,
    )
    return "imported"


def update_addon_metadata(db, stats: MigrationStats, *, dry_run: bool) -> None:
    """legacy_message_id マッピングに基づき AddonMessageMetadata.message_id を一括 UPDATE。

    旧 message_id 空間と新採番空間は当然被るため、 逐次 UPDATE では中間状態で
    UNIQUE(message_id, addon_name, key) 違反になる。 二段階で実行する:

    Phase 1: 対象行を一意な一時 message_id (``__migrating_<id>__``) に退避
    Phase 2: 一時 message_id を最終 new_message_id に書き換え

    Phase 2 で「target に既存行 (= migration 対象外で偶然同じ new_msg_id を持つ行)」
    があれば skip し、 統計に記録する。commit はしない (呼び出し側が持つ)。
    """
    if not stats.legacy_message_id_map:
        LOGGER.info("AddonMessageMetadata UPDATE: マッピングが空 — skip")
        return

    LOGGER.info(
        "AddonMessageMetadata UPDATE 開始 (マッピング件数=%d, dry_run=%s)",
        len(stats.legacy_message_id_map), dry_run,
    )

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
        existing = db.query(AddonMessageMetadata).filter(
            AddonMessageMetadata.message_id == new_msg_id,
            AddonMessageMetadata.addon_name == addon_name,
            AddonMessageMetadata.key == key,
            AddonMessageMetadata.id != row_id,
        ).first()
        if existing is not None:
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

    db.flush()
    stats.addon_metadata_updated = updated
    stats.addon_metadata_not_found = not_found
    stats.addon_metadata_skipped_conflict = skipped_conflict
    LOGGER.info(
        "AddonMessageMetadata UPDATE 完了: 更新=%d / 衝突 skip=%d / 該当なし legacy_id=%d",
        updated, skipped_conflict, not_found,
    )


def rebuild_legacy_map_from_db(db, stats: MigrationStats) -> None:
    """既存 building_messages から legacy_message_id → new message_id を SELECT で再構築。

    再実行時 (= 全 building が既取り込みで migrate_building が skip される) でも
    update_addon_metadata が機能するように、 マッピングを DB から復元する。
    """
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


def import_building_logs(
    db,
    saiverse_home: Path,
    *,
    city_filter: Optional[str] = None,
    building_filter: Optional[str] = None,
    dry_run: bool = False,
) -> MigrationStats:
    """対象の log.json 群を building_messages へ取り込む。commit しない。

    冪等: 取り込み痕跡のある building は skip。読めないファイル・通常経路の行が
    先行する building も skip するが、いずれも stats に現れ、起動時の検算
    (:func:`scan_legacy_log_deficits`) が拾ってアラートにする。
    """
    log_files = find_log_files(
        saiverse_home, city_filter=city_filter, building_filter=building_filter,
    )
    LOGGER.info("対象 log.json: %d 件", len(log_files))

    stats = MigrationStats()
    for log_path in log_files:
        building_id = log_path.parent.name
        stats.buildings_scanned += 1
        LOGGER.info("→ %s", log_path)

        messages, unreadable_reason = load_log(log_path)
        if messages is None:
            LOGGER.warning("  読めないため skip (%s): %s", unreadable_reason, log_path)
            stats.buildings_skipped_unreadable += 1
            continue
        migrate_building(db, building_id, messages, stats, dry_run=dry_run)

    # 再実行時 (全 building が既取り込みで map が空) のために、 既存 building_messages
    # から legacy_message_id → 新 message_id を再構築する。
    if not stats.legacy_message_id_map:
        rebuild_legacy_map_from_db(db, stats)

    update_addon_metadata(db, stats, dry_run=dry_run)
    return stats


# ---------------------------------------------------------------------------
# 起動時の検算 (常設): log.json にあるのに DB に無い履歴を見つける
# ---------------------------------------------------------------------------

def scan_legacy_log_deficits(
    db,
    saiverse_home: Path,
    city_name: str,
    building_ids: Iterable[str],
) -> List[dict]:
    """登録済み building ごとに log.json ↔ DB の取り込み状況を突き合わせる。

    ループ内の帳簿でなく DB を SELECT し直して確かめる (自分の記帳を証拠にしない)。
    戻り値の各 dict: {building_id, kind, file_entries, imported_rows, live_rows, path}
    kind:
        "unreadable"     ... log.json が壊れていて読めない (取り込み痕跡も無い)
        "not_imported"   ... 履歴があるのに DB に 1 行も無い
        "live_rows_only" ... 履歴があるのに取り込み痕跡が無く、通常経路の行だけある
        "partial"        ... 取り込み行数がファイルの件数より少ない
    """
    deficits: List[dict] = []
    for building_id in building_ids:
        log_path = (
            saiverse_home / "cities" / city_name / "buildings" / building_id / "log.json"
        )
        if not log_path.exists():
            continue  # Phase 2+3 以降に作られた部屋。旧ファイルが無いのは正常

        imported_rows = (
            db.query(BuildingMessage)
            .filter(
                BuildingMessage.building_id == building_id,
                BuildingMessage.legacy_seq.isnot(None),
            )
            .count()
        )
        total_rows = (
            db.query(BuildingMessage).filter_by(building_id=building_id).count()
        )

        messages, unreadable_reason = load_log(log_path)
        if messages is None:
            if imported_rows == 0:
                deficits.append({
                    "building_id": building_id,
                    "kind": "unreadable",
                    "reason": unreadable_reason,
                    "file_entries": None,
                    "imported_rows": 0,
                    "live_rows": total_rows,
                    "path": str(log_path),
                })
            continue

        file_entries = len(messages)
        if file_entries == 0:
            continue

        if imported_rows == 0 and total_rows == 0:
            kind = "not_imported"
        elif imported_rows == 0:
            kind = "live_rows_only"
        elif imported_rows < file_entries:
            kind = "partial"
        else:
            continue
        deficits.append({
            "building_id": building_id,
            "kind": kind,
            "reason": None,
            "file_entries": file_entries,
            "imported_rows": imported_rows,
            "live_rows": total_rows - imported_rows,
            "path": str(log_path),
        })
    return deficits


# ---------------------------------------------------------------------------
# Pulse cursor (conscious_log.json → persona_pulse_cursor)
# ---------------------------------------------------------------------------

@dataclass
class CursorStats:
    personas_scanned: int = 0
    personas_skipped_missing: int = 0
    personas_skipped_existing_rows: int = 0
    personas_processed: int = 0
    cursors_inserted: int = 0
    cursors_updated: int = 0
    cursors_zero_fallback: int = 0


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
    stats: CursorStats, dry_run: bool,
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


def migrate_persona_cursors(
    db,
    persona_dir: Path,
    stats: CursorStats,
    *,
    dry_run: bool,
    only_if_no_existing_rows: bool = False,
) -> None:
    """1 ペルソナ分の conscious_log.json cursor を persona_pulse_cursor へ。commit しない。

    Args:
        only_if_no_existing_rows: True のとき、当該ペルソナの cursor 行が DB に
            1 行でもあれば何もしない。アップグレード経路で使う — 稼働中の
            環境の生きた cursor を、古いファイルのリマップ値で上書きしないため。
    """
    persona_id = persona_dir.name
    stats.personas_scanned += 1
    cl_path = persona_dir / "conscious_log.json"
    if not cl_path.exists():
        LOGGER.debug("  %s: conscious_log.json なし — skip", persona_id)
        stats.personas_skipped_missing += 1
        return

    if only_if_no_existing_rows:
        existing = db.query(PersonaPulseCursor).filter_by(
            PERSONA_ID=persona_id
        ).count()
        if existing > 0:
            LOGGER.info(
                "  %s: 生きた cursor 行が %d 件ある — 上書きせず skip",
                persona_id, existing,
            )
            stats.personas_skipped_existing_rows += 1
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

    stats.personas_processed += 1
    LOGGER.info(
        "  %s: 取り込み完了 (cursors=%d, fmt=%s)",
        persona_id, len(raw_cursors), fmt,
    )
