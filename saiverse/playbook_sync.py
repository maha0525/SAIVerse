"""起動時プレイブック自動同期モジュール。

ファイルベースのプレイブック（builtin_data / expansion_data / user_data）を
スキャンして DB に差分同期する。

優先順位（高い方が勝つ）:
    user_data/<project>/playbooks/public/  >  expansion_data/<addon>/playbooks/public/  >  builtin_data/playbooks/public/

同名プレイブックが複数ソースに存在する場合は最も優先度が高いファイルを採用する。

ハッシュ比較:
    ファイルの JSON を sort_keys=True でシリアライズした SHA-256 (16 文字) を
    DB の source_hash と照合し、差分がある場合のみ更新する。

save_playbook ツール経由で DB を直接編集した場合は source_file / source_hash が
クリアされるため、次回起動時にファイル版で上書きされない（設計上のユーザー優先）。

Orphan の自動削除:
    sync 終了時に scope='public' AND source_file IS NOT NULL でかつ
    source_file が指すファイルが disk に無いレコードを削除する
    (PlaybookPermission の対応行も削除)。
    save_playbook ツール経由で作った Playbook は source_file IS NULL なので
    保護される。addon を一時的に外した場合は対応するファイルが消えて該当
    Playbook も削除されるが、addon を再追加すれば再 import される。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _canonical_hash(data: Any) -> str:
    """JSON の正規化ダンプに対する SHA-256 の先頭 16 文字を返す。"""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _rel_path(path: Path) -> str:
    """プロジェクトルートからの相対パスを文字列で返す。"""
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return str(path)


def _collect_file_playbooks() -> Dict[str, Dict[str, Any]]:
    """ファイルベースのプレイブックを優先順に収集する。

    戻り値: {playbook_name: {"path": Path, "data": dict, "source_rel": str, "hash": str}}
    同名は最初に見つかったもの（= 高優先）のみ保持。
    """
    from saiverse.data_paths import iter_project_subdirs, PLAYBOOKS_DIR

    result: Dict[str, Dict[str, Any]] = {}

    for playbooks_dir in iter_project_subdirs(PLAYBOOKS_DIR):
        # public/ サブディレクトリを対象にする（personal/building は対象外）
        public_dir = playbooks_dir / "public"
        if not public_dir.exists():
            continue
        for json_path in sorted(public_dir.glob("*.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception as exc:
                LOGGER.warning("playbook_sync: failed to read %s: %s", json_path, exc)
                continue

            name = data.get("name")
            if not name:
                LOGGER.warning("playbook_sync: missing 'name' in %s, skipping", json_path)
                continue

            if name in result:
                # 低優先ソースは無視（高優先が既に登録済み）
                continue

            result[name] = {
                "path": json_path,
                "data": data,
                "source_rel": _rel_path(json_path),
                "hash": _canonical_hash(data),
            }

    return result


def _build_db_record(
    name: str,
    data: dict,
    source_rel: str,
    source_hash: str,
) -> dict:
    """Playbook DB レコードのフィールド辞書を構築する。"""
    schema_payload = {
        "name": name,
        "description": data.get("description", ""),
        "input_schema": data.get("input_schema", []),
        "start_node": data.get("start_node"),
    }
    required_creds = data.get("required_credentials")
    return {
        "description": data.get("description", ""),
        "display_name": data.get("display_name"),
        "schema_json": json.dumps(schema_payload, ensure_ascii=False),
        "nodes_json": json.dumps(data, ensure_ascii=False),
        "router_callable": bool(data.get("router_callable", False)),
        "user_selectable": bool(data.get("user_selectable", False)),
        "dev_only": bool(data.get("dev_only", False)),
        "required_credentials": (
            json.dumps(required_creds, ensure_ascii=False) if required_creds else None
        ),
        "source_file": source_rel,
        "source_hash": source_hash,
    }


def _prune_orphan_playbooks(db) -> int:
    """source_file が指すファイルが disk に無い public Playbook を削除する。

    対象:
        scope='public' AND source_file IS NOT NULL AND ファイルが disk に無い

    保護:
        - save_playbook ツール経由で作った Playbook (source_file IS NULL)
        - personal/building scope の Playbook

    PlaybookPermission の対応行も削除する。

    Args:
        db: 呼び出し側で管理する DB セッション (commit はここでは行わない)

    Returns:
        削除件数
    """
    from database.models import Playbook, PlaybookPermission

    pruned_count = 0
    candidates = (
        db.query(Playbook)
        .filter(Playbook.scope == "public")
        .filter(Playbook.source_file.isnot(None))
        .all()
    )

    for pb in candidates:
        src_path = Path(pb.source_file)
        if not src_path.is_absolute():
            src_path = _PROJECT_ROOT / src_path

        if src_path.exists():
            continue

        perms_deleted = (
            db.query(PlaybookPermission)
            .filter(PlaybookPermission.playbook_name == pb.name)
            .delete(synchronize_session=False)
        )
        db.delete(pb)
        LOGGER.info(
            "playbook_sync: pruned orphan '%s' (missing source: %s, permissions removed: %d)",
            pb.name, pb.source_file, perms_deleted,
        )
        pruned_count += 1

    return pruned_count


def sync_playbooks_from_files(session_factory=None) -> Dict[str, int]:
    """ファイルベースのプレイブックを DB に差分同期する。

    Args:
        session_factory: DB セッションファクトリ（省略時は database.session.SessionLocal を使用）

    Returns:
        {"imported": int, "updated": int, "skipped": int, "errors": int, "pruned": int}
    """
    from database.models import Playbook

    if session_factory is None:
        from database.session import SessionLocal
        session_factory = SessionLocal

    counts = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0, "pruned": 0}

    try:
        file_playbooks = _collect_file_playbooks()
    except Exception:
        LOGGER.exception("playbook_sync: failed to collect file playbooks")
        return counts

    LOGGER.debug("playbook_sync: found %d file-based playbook(s)", len(file_playbooks))

    db = session_factory()
    try:
        for name, info in file_playbooks.items():
            try:
                existing: Optional[Playbook] = (
                    db.query(Playbook).filter(Playbook.name == name).first()
                )
                fields = _build_db_record(
                    name=name,
                    data=info["data"],
                    source_rel=info["source_rel"],
                    source_hash=info["hash"],
                )

                if existing is None:
                    # 新規インポート
                    record = Playbook(
                        name=name,
                        scope="public",
                        created_by_persona_id=None,
                        building_id=None,
                        **fields,
                    )
                    db.add(record)
                    counts["imported"] += 1
                    LOGGER.info(
                        "playbook_sync: imported '%s' from %s", name, info["source_rel"]
                    )
                elif existing.source_hash != info["hash"]:
                    # ハッシュが変わっている → 更新
                    for k, v in fields.items():
                        setattr(existing, k, v)
                    counts["updated"] += 1
                    LOGGER.info(
                        "playbook_sync: updated '%s' from %s", name, info["source_rel"]
                    )
                else:
                    # 差分なし
                    counts["skipped"] += 1
                    LOGGER.debug("playbook_sync: '%s' is up-to-date, skipping", name)

            except Exception:
                LOGGER.exception("playbook_sync: error processing playbook '%s'", name)
                counts["errors"] += 1

        # Disk から消えた public プレイブックを DB からも削除
        try:
            counts["pruned"] = _prune_orphan_playbooks(db)
        except Exception:
            LOGGER.exception("playbook_sync: error during orphan prune")
            counts["errors"] += 1

        db.commit()

    except Exception:
        LOGGER.exception("playbook_sync: DB error during sync")
        db.rollback()
    finally:
        db.close()

    LOGGER.info(
        "playbook_sync: done — imported=%d updated=%d skipped=%d pruned=%d errors=%d",
        counts["imported"], counts["updated"], counts["skipped"], counts["pruned"], counts["errors"],
    )
    return counts
