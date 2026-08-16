"""旧ファイル形式 (log.json / conscious_log.json) → DB への取り込みロジック。

Building のチャットログは Phase 2+3 (2026-05-20, ec9eba70) で
``cities/<city>/buildings/<bid>/log.json`` から ``building_messages`` テーブルへ、
ペルソナの pulse cursor は ``conscious_log.json`` から ``persona_pulse_cursor``
テーブルへ移行した。本モジュールはその「過去データの取り込み」の実体で、
次の 3 経路から呼ばれる:

1. **起動時の検算** (``manager/initialization.py``): 毎起動、log.json に履歴が
   あるのに DB に無い部屋を探し、**その場で取り込む**。直せなかったものだけ
   startup_alerts (UI バナー) に載せ続ける。リリース版 (v0.2.x, log.json 時代)
   から上がってきた環境の過去ログも、この経路が拾う。
2. **手動 CLI** (``scripts/migrate_building_logs_to_db.py`` /
   ``scripts/migrate_conscious_log_to_db.py``): 個別復旧・再実行用の薄い入口。

かつてはバージョンアップグレードの dev5 エッジでも取り込んでいたが、SQLite が
最外周の SAVEPOINT の RELEASE で確定してしまい、枠組みの commit 境界と食い違う
ため撤去した (2026-08-16)。同じ仕事を 2 箇所に置く理由も無い。

設計上の約束 (docs/intent/building_memory_unified.md):

- **スキップ判定は「現物が読めるか」だけで行う。** 過去の隔離マーカー
  (``log.json.corrupted_*``) の有無では判定しない。マーカーは事故時の退避物で、
  その後修復された log.json の健全性について何も語らないため
  (2026-08-16 テスタロッサの部屋の取り込み漏れの根因)。
- **黙って諦めない。** 取り込めなかった部屋は戻り値の stats / 検算結果に必ず
  現れ、呼び出し側がアラートにする。
- **部屋ひとつが「全部入るか、1 つも入らないか」の単位。** 1 行でも入らなければ
  その部屋は丸ごと巻き戻す (理由は :func:`migrate_building` の docstring)。
  1 部屋の失敗は他の部屋を道連れにしない (部屋ごとに SAVEPOINT を張る)。
  CLI 経路だけは ``commit_per_building=True`` で部屋ごとに確定させ、後半の失敗で
  前半まで巻き戻らないようにする。
- **既定では commit しない。** トランザクション境界は呼び出し側が持つ。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, or_

from database.models import AddonMessageMetadata, BuildingMessage, PersonaPulseCursor
from database.building_messages import serialize_building_message

LOGGER = logging.getLogger(__name__)


@dataclass
class BuildingImportResult:
    """1 部屋の取り込み結果。**呼び出し側が確定を見届けてから** stats へ合流させる。

    status: "imported" (取り込んだ) / "already" (入れるものが無い)
    """
    status: str
    id_map: Dict[Tuple[str, str], str] = field(default_factory=dict)
    inserted: int = 0


class LegacyLogPartialImport(Exception):
    """1 部屋の取り込みが途中で失敗したことを示す。

    呼び出し側はこの部屋の書き込みを丸ごと巻き戻す (:func:`migrate_building` の
    docstring 参照)。
    """


# ---------------------------------------------------------------------------
# Building log (log.json → building_messages)
# ---------------------------------------------------------------------------

@dataclass
class MigrationStats:
    buildings_scanned: int = 0
    buildings_skipped_unreadable: int = 0
    buildings_skipped_already_migrated: int = 0
    buildings_failed: int = 0
    messages_seen: int = 0
    messages_inserted: int = 0
    addon_metadata_updated: int = 0
    addon_metadata_not_found: int = 0
    addon_metadata_skipped_conflict: int = 0
    # (building_id, legacy_message_id) -> new message_id ; 重複時は最初に出会った方
    legacy_message_id_map: Dict[Tuple[str, str], str] = field(default_factory=dict)


def imported_row_filter():
    """「この行は過去ログ取り込みで作られた」を判定する SQL 条件。

    使うのは検算 (:func:`scan_legacy_log_deficits`) だけで、報告の種類を
    ``not_imported`` / ``live_rows_only`` / ``partial`` に振り分けるために引く。
    **取り込むかどうかの判断には使わない** — そちらは「その発言が既に DB に
    居るか」を message_id で見る (:func:`migrate_building`)。印の有無に頼ると、
    印の残らない行があったときに二重取り込みを起こすため。

    見分けの手がかり:

    - ``seq < 0``: 現行の取り込みが使う番号帯。通常の発言は必ず 1 以上なので、
      これだけで確実に見分けられる (元ファイルの中身に依存しない)。
    - ``legacy_seq`` / ``legacy_message_id`` が埋まっている: 2026-08-16 より前の
      取り込みは正の番号で入れていたので、この 2 列が唯一の手がかりになる。
      通常の書き込み経路はこの 2 列を NULL のまま入れる。

    旧取り込み分については、元ファイルの行が seq も message_id も持たない場合に
    痕跡が残らず漏れる (全行がその形の部屋が本番データに 3 つある)。漏れても
    影響は報告の種類がずれることだけ。その部屋に取り込むものがあるかどうかは
    message_id の突き合わせが決めるので、二重取り込みにはならない。
    """
    return or_(
        BuildingMessage.seq < 0,
        BuildingMessage.legacy_seq.isnot(None),
        BuildingMessage.legacy_message_id.isnot(None),
    )


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

    読めない理由 (str) が返るのは 0 バイト / 読み取り失敗 / 文字コード異常 /
    JSON 破損 / 構造異常 のときで、その場合 messages は None。

    **ファイルが原因で例外を投げない。** 呼び出し側の 1 つは毎起動の検算で、
    そこで例外が漏れると 1 部屋の壊れたファイルが検算全体を黙らせる。読めない
    ことは戻り値で伝え、判断は呼び出し側に渡す。
    """
    try:
        if log_path.stat().st_size == 0:
            return None, "0 バイト"
    except OSError as e:
        return None, f"stat 失敗 ({e})"
    try:
        raw = log_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return None, f"UTF-8 として読めない ({e})"
    except OSError as e:
        return None, f"読み取り失敗 ({e})"
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
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
    """1 building 分のメッセージを全件 DB に取り込む。commit しない。

    **取り込んだ過去ログは 0 より小さい seq を持つ。** 通常の発言は 1 から順に
    採番されるので、過去ログは必ずその手前に並ぶ。この一本の規則で 3 つのことが
    同時に片付く:

    - **並び順**: 既に会話が始まっている部屋でも、過去ログは時系列どおり前に入る。
      既存の行は 1 つも動かさないので、行の seq / message_id を指している他の記録
      (ペルソナ個人の記憶に残る転記元の目印、AddonMessageMetadata) がずれない。
    - **既読の扱い**: 「どこまで読んだか」は 0 以上なので、負の seq は常にそれ以下 =
      既読。過去ログを「未読の新着」に化けさせないための cursor 操作が要らなくなる。
    - **見分け**: その部屋の過去ログは seq < 0 で正確に引ける。

    入れるのは **まだこの部屋に居ない発言だけ**。判定は起動時の検算
    (:func:`scan_legacy_log_deficits`) と同じ規則 — ファイルの各発言の
    message_id がこの部屋の DB 行のどこにも無いものを「欠け」と数える。同じ規則を
    使うのは、「取り込む対象」と「欠けとして警告する対象」がずれないため。
    照合を部屋ごとに閉じるのは、message_id が ``<部屋ID>:<番号>`` の形で、部屋を
    またいで同じ ID が出ないから (本番データで重複 0 件を実測)。

    message_id を持たない発言は、その部屋に行が 1 つも無いとき (= ファイルが
    唯一の写し) だけ入れる。行があるときは、既に入っているのか未取り込みなのかを
    見分けられないので触らない — 入れると同じ発言を二重に並べる。

    **1 行でも入らなければ、この部屋は丸ごと入らなかったことにする**
    (:class:`LegacyLogPartialImport` を送出し、呼び出し側が巻き戻す)。部分的に
    入れると 2 つの壊れ方が出る:

    - message_id を持たない行が残ると、次回からその部屋は「行がある」側に回って
      対象外になり、検算も欠けを 0 と数える。**欠けたまま誰にも気づかれない。**
    - 残りを次回入れると、採番が既存の負 seq より手前に付くので、ファイル順が
      崩れる (A,B,C の B だけ失敗 → 再実行で B,A,C)。

    全件やり直しなら、失敗した部屋は行が 1 つも無い状態で残り、検算が「未取込」
    として毎起動アラートにする。うるさいが、黙って欠けるよりよい。

    Returns:
        "imported" ... 取り込みを実行した
        "already"  ... 入れるものが無い — 冪等 skip

    Raises:
        LegacyLogPartialImport: 1 行でも取り込めなかったとき
    """
    messages_list = list(messages)
    stats.messages_seen += len(messages_list)

    total_rows = (
        db.query(BuildingMessage).filter_by(building_id=building_id).count()
    )
    db_ids = set()
    if total_rows:
        for mid, legacy_mid in (
            db.query(BuildingMessage.message_id, BuildingMessage.legacy_message_id)
            .filter_by(building_id=building_id)
            .all()
        ):
            if mid:
                db_ids.add(mid)
            if legacy_mid:
                db_ids.add(legacy_mid)

    pending: List[dict] = []
    for msg in messages_list:
        mid = msg.get("message_id") if isinstance(msg, dict) else None
        if isinstance(mid, str) and mid:
            if mid not in db_ids:
                pending.append(msg)
        elif total_rows == 0:
            pending.append(msg)

    if not pending:
        LOGGER.info(
            "  %s: 古いファイルの %d 件はすべて移し終わっているので、何もしません",
            building_id, len(messages_list),
        )
        stats.buildings_skipped_already_migrated += 1
        return BuildingImportResult("already")

    # 採番の起点。既に負の seq がある部屋ではその手前へ続ける。無ければ 0 の手前から。
    min_seq = db.query(func.min(BuildingMessage.seq)).filter_by(
        building_id=building_id
    ).scalar()
    base = min(int(min_seq), 0) if min_seq is not None else 0

    total = len(pending)
    local_inserted = 0
    # 対応表はこの部屋ぶんをまず手元に作る。stats へ合流させるのは全件入り切って
    # から — 途中で失敗すると DB 行は巻き戻るのに、Python の辞書は巻き戻らない。
    # 残ったまま update_addon_metadata が走ると、**存在しない行の ID** へ
    # AddonMessageMetadata を付け替えて確定する (2026-08-16 Codex 指摘)。
    local_id_map: Dict[Tuple[str, str], str] = {}
    for index, msg in enumerate(pending):
        # ファイル順を保ったまま base の手前へ詰める (先頭が最も小さい)。
        new_seq = base - total + index
        new_message_id = f"{building_id}:{new_seq}"
        legacy_seq_int: Optional[int] = None
        try:
            # ファイルの 1 要素が dict でないことがある (壊れた log.json)。
            # ここの .get も行単位 try の中に置く — 外に出すと 1 行の異常で
            # building 全体が巻き戻る。
            legacy_msg_id = msg.get("message_id")
            legacy_seq_raw = msg.get("seq")
            try:
                legacy_seq_int = (
                    int(legacy_seq_raw) if legacy_seq_raw is not None else None
                )
            except (TypeError, ValueError):
                legacy_seq_int = None

            # serialize_building_message は metadata.event を構造化分離してくれる。
            # 新 seq / 新 message_id で上書きする。serialize も行単位 try の中 —
            # 1 行の異常データで building 全体を落とさない。
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
                if map_key not in local_id_map:
                    local_id_map[map_key] = new_message_id

            if dry_run:
                local_inserted += 1
                continue
            db.add(BuildingMessage(**record))
            db.flush()
            local_inserted += 1
        except Exception as e:
            # 1 行でも入らなければ、この部屋は「入らなかった」ことにする
            # (下の docstring 参照 — 部分的に入れると欠けが沈黙するため)。
            raise LegacyLogPartialImport(
                f"{building_id}: 古い会話 1 件を移せませんでした "
                f"(ファイル側の番号={legacy_seq_int}): {e}"
            ) from e

    # cursor の操作は要らない。取り込んだ行の seq は必ず 0 未満で、「どこまで
    # 読んだか」は 0 以上なので、負の seq は常に既読側に入る。
    LOGGER.info(
        "  %s: 古い会話 %d 件を移しました (会話の並びでは %d 番から %d 番、"
        "いま話している分より前に入ります)",
        building_id, local_inserted, base - total, base - 1,
    )
    # **ここでは stats へ合流させない。** SAVEPOINT が RELEASE されるのは呼び出し側の
    # with を抜けたときで、そこで失敗すれば DB 行は残らない。合流を関数の中でやると
    # 「行は無いのに対応表と件数だけ残る」状態を作り、update_addon_metadata が
    # 存在しない ID へ付け替える (2026-08-16 Codex 3 巡目の指摘)。
    return BuildingImportResult("imported", local_id_map, local_inserted)


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
    commit_per_building: bool = False,
) -> MigrationStats:
    """対象の log.json 群を building_messages へ取り込む。

    冪等: 取り込み痕跡のある building は skip。読めないファイル・通常経路の行が
    先行する building も skip するが、いずれも stats に現れ、起動時の検算
    (:func:`scan_legacy_log_deficits`) が拾ってアラートにする。

    部屋ひとつを SAVEPOINT で囲うので、1 部屋の失敗は他の部屋を巻き込まない
    (失敗した部屋は行が 1 つも残らず、検算が「未取込」として拾う)。

    Args:
        commit_per_building: True なら部屋ごとに commit して確定させる。手動 CLI
            用 — 後半の部屋の失敗で前半の取り込みまで巻き戻らないようにする。
            False (既定) では commit せず、境界は呼び出し側が持つ
            (アップグレード経路はエンティティ単位の commit に相乗りする)。
    """
    log_files = find_log_files(
        saiverse_home, city_filter=city_filter, building_filter=building_filter,
    )
    LOGGER.info("対象 log.json: %d 件", len(log_files))

    commit_each = commit_per_building and not dry_run
    stats = MigrationStats()
    for log_path in log_files:
        building_id = log_path.parent.name
        stats.buildings_scanned += 1
        LOGGER.info("→ %s", log_path)

        messages, unreadable_reason = load_log(log_path)
        if messages is None:
            LOGGER.warning(
                "  %s: 古いファイルが読めません (%s): %s",
                building_id, unreadable_reason, log_path,
            )
            stats.buildings_skipped_unreadable += 1
            continue
        try:
            with db.begin_nested():
                result = migrate_building(
                    db, building_id, messages, stats, dry_run=dry_run,
                )
            # SAVEPOINT を抜けた = この部屋の書き込みが確定した。ここで初めて
            # 対応表と件数を合流させる (RELEASE が失敗すれば上の except に入り、
            # 行も対応表も残らない)。
            for key, value in result.id_map.items():
                stats.legacy_message_id_map.setdefault(key, value)
            stats.messages_inserted += result.inserted
        except LegacyLogPartialImport as e:
            # 1 行でも入らなければ全件やり直し。中途半端に入れると欠けが沈黙する。
            LOGGER.error(
                "  %s: 途中で 1 件入らなかったので、この部屋は全件やり直しにします "
                "(%s)。会話は古いファイルに残っていて、次の起動でもう一度試します",
                building_id, e,
            )
            # messages_inserted はループを抜けた後に加算されるので、途中で
            # 抜けたこの部屋の分は最初から数えられていない。
            stats.buildings_failed += 1
            continue
        except Exception:
            LOGGER.error(
                "  %s: この部屋の古い会話を移せませんでした。"
                "この部屋の分だけ元に戻して、他の部屋は続けます",
                building_id, exc_info=True,
            )
            stats.buildings_failed += 1
            continue
        if commit_each:
            db.commit()

    # 既存 building_messages から legacy_message_id → 新 message_id を補う。
    # **map が空のときだけ、にしてはいけない** — 新しく取り込んだ部屋と、既に
    # 取り込み済みで skip した部屋が混ざると、後者の対応が永久に補われず、
    # その部屋の AddonMessageMetadata が旧 ID のまま取り残される
    # (2026-08-16 Codex 3 巡目の指摘)。今回入れた分を優先し、足りない分を DB から
    # 埋める (rebuild 側が既存キーを上書きしない)。
    rebuild_legacy_map_from_db(db, stats)

    # メタデータの付け替えに失敗しても、確定済みの取り込み行は残す
    # (メッセージ本体の方が addon メタデータより失って困る)。
    try:
        with db.begin_nested():
            update_addon_metadata(db, stats, dry_run=dry_run)
    except Exception:
        LOGGER.error(
            "AddonMessageMetadata の付け替えに失敗 — 取り込んだメッセージは保持",
            exc_info=True,
        )
    else:
        if commit_each:
            db.commit()
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

    欠けの判定は件数比較でなく **メッセージ ID の突き合わせ**: ファイルの各
    メッセージの message_id が、DB の message_id / legacy_message_id のどこにも
    無いものだけを「欠け (missing)」と数える。dual-write 期の環境ではファイル
    末尾の数件が「通常経路の行」として DB に居るため、件数比較だと欠けて
    いないのに欠けて見える (2026-08-16 に本番データで 6 部屋の偽陽性を実測)。

    戻り値の各 dict: {building_id, kind, reason, file_entries, missing,
    imported_rows, live_rows, path}
    kind:
        "unreadable"     ... log.json が壊れていて読めない (取り込み痕跡も無い)
        "not_imported"   ... 履歴があるのに DB に 1 行も無い
        "live_rows_only" ... ファイル時代の履歴が DB に無く、通常経路の行だけある
        "partial"        ... 取り込みはあるが一部のメッセージが DB に無い
        "check_failed"   ... その部屋の検算自体が例外で完了しなかった

    部屋ひとつの失敗で検算全体を黙らせない: 例外は部屋ごとに捕まえ、"check_failed"
    として結果に載せる (黙って 0 件を返すと「漏れ無し」と区別がつかない)。

    精度の限界: message_id を持たないファイル行 (旧形式の host イベント等) は
    個別に突き合わせられない。DB にその部屋の行が 1 つも無いとき (= ファイルが
    唯一の写し) だけ欠けと数え、行があるときは「同じ実行系が両側に書いた」と
    みなして ID 付きの行だけで判定する (dual-write 期の移動イベントはファイル側
    だけ ID 無しで、欠け扱いすると偽陽性になることを本番データで実測)。
    """
    deficits: List[dict] = []
    for building_id in building_ids:
        try:
            deficit = _scan_one_building(db, saiverse_home, city_name, building_id)
        except Exception as e:
            # 失敗したクエリでセッションが汚れていると次の部屋も巻き添えになる。
            try:
                db.rollback()
            except Exception:
                LOGGER.debug("検算の rollback にも失敗: building=%s", building_id)
            LOGGER.warning(
                "%s: 古い会話が移し終わっているかを確認できませんでした。"
                "この部屋だけ飛ばして、他の部屋は確認します",
                building_id, exc_info=True,
            )
            deficits.append({
                "building_id": building_id,
                "kind": "check_failed",
                "reason": f"{type(e).__name__}: {e}",
                "file_entries": None,
                "missing": None,
                "imported_rows": None,
                "live_rows": None,
                "path": str(
                    saiverse_home / "cities" / city_name / "buildings"
                    / building_id / "log.json"
                ),
            })
            continue
        if deficit is not None:
            deficits.append(deficit)
    return deficits


def _scan_one_building(
    db, saiverse_home: Path, city_name: str, building_id: str,
) -> Optional[dict]:
    """1 部屋分の突き合わせ。欠けが無ければ None。

    判定の意味と精度の限界は :func:`scan_legacy_log_deficits` の docstring 参照。
    """
    log_path = (
        saiverse_home / "cities" / city_name / "buildings" / building_id / "log.json"
    )
    if not log_path.exists():
        return None  # Phase 2+3 以降に作られた部屋。旧ファイルが無いのは正常

    imported_rows = (
        db.query(BuildingMessage)
        .filter(BuildingMessage.building_id == building_id)
        .filter(imported_row_filter())
        .count()
    )
    total_rows = (
        db.query(BuildingMessage).filter_by(building_id=building_id).count()
    )

    messages, unreadable_reason = load_log(log_path)
    if messages is None:
        if imported_rows == 0:
            return {
                "building_id": building_id,
                "kind": "unreadable",
                "reason": unreadable_reason,
                "file_entries": None,
                "missing": None,
                "imported_rows": 0,
                "live_rows": total_rows,
                "path": str(log_path),
            }
        return None

    file_entries = len(messages)
    if file_entries == 0:
        return None

    file_ids = set()
    no_id_entries = 0
    for m in messages:
        mid = m.get("message_id") if isinstance(m, dict) else None
        if isinstance(mid, str) and mid:
            file_ids.add(mid)
        else:
            no_id_entries += 1

    db_ids = set()
    for mid, legacy_mid in (
        db.query(BuildingMessage.message_id, BuildingMessage.legacy_message_id)
        .filter_by(building_id=building_id)
        .all()
    ):
        if mid:
            db_ids.add(mid)
        if legacy_mid:
            db_ids.add(legacy_mid)

    missing = len(file_ids - db_ids)
    if total_rows == 0:
        missing += no_id_entries
    if missing == 0:
        return None

    if imported_rows == 0 and total_rows == 0:
        kind = "not_imported"
    elif imported_rows == 0:
        kind = "live_rows_only"
    else:
        kind = "partial"
    return {
        "building_id": building_id,
        "kind": kind,
        "reason": None,
        "file_entries": file_entries,
        "missing": missing,
        "imported_rows": imported_rows,
        "live_rows": total_rows - imported_rows,
        "path": str(log_path),
    }


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
