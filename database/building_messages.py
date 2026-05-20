"""``building_messages`` テーブルへの書き込み / 読み出し共通 helper。

Phase 2+3 移行後、 本モジュールが Building ログの単一 source of truth として
動作する (= JSON 側 log.json は過去ログアーカイブとして残るのみ、 dual-write は廃止)。

呼び出し元:
- ``persona/history_manager.py``: add_message / add_to_building_only / update_building_message
- ``manager/history.py``: add_building_event (OccupancyManager 経路)

詳細: docs/intent/building_memory_unified.md
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)


def deserialize_building_message(row: Any) -> Dict[str, Any]:
    """``BuildingMessage`` 行を 旧 ``building_histories[bid]`` 要素互換の dict に変換。

    既存呼び出し元 (= dict[role/content/seq/message_id/heard_by/ingested_by/metadata] を
    期待) との互換性を保つために、 event_type/event_data は metadata.event に復元する。
    """
    metadata: Dict[str, Any] = {}
    if row.metadata_json:
        try:
            loaded = json.loads(row.metadata_json)
            if isinstance(loaded, dict):
                metadata = loaded
        except json.JSONDecodeError:
            LOGGER.debug("deserialize: metadata_json parse failed (msg_id=%s)", row.message_id)
    if row.event_type and row.event_data:
        try:
            metadata["event"] = json.loads(row.event_data)
        except json.JSONDecodeError:
            LOGGER.debug("deserialize: event_data parse failed (msg_id=%s)", row.message_id)
    heard_by: List[str] = []
    if row.heard_by:
        try:
            parsed = json.loads(row.heard_by)
            if isinstance(parsed, list):
                heard_by = [str(p) for p in parsed if p]
        except json.JSONDecodeError:
            heard_by = []
    ingested_by: List[str] = []
    if row.ingested_by:
        try:
            parsed = json.loads(row.ingested_by)
            if isinstance(parsed, list):
                ingested_by = [str(p) for p in parsed if p]
        except json.JSONDecodeError:
            ingested_by = []
    result: Dict[str, Any] = {
        "role": row.role,
        "content": row.content,
        "seq": row.seq,
        "message_id": row.message_id,
        "timestamp": row.timestamp,
        "heard_by": heard_by,
        "ingested_by": ingested_by,
    }
    if metadata:
        result["metadata"] = metadata
    if row.persona_id:
        result["persona_id"] = row.persona_id
    if row.origin_track_id:
        result["origin_track_id"] = row.origin_track_id
    if row.pulse_id:
        result["pulse_id"] = row.pulse_id
    return result


def fetch_building_messages(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    *,
    limit: Optional[int] = None,
    after_seq: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """``building_id`` のメッセージを seq 昇順で取得し、 dict 形式で返す。

    ``limit``: None なら全件、 整数なら末尾から N 件 (= 直近 N 件)。
    ``after_seq``: seq > after_seq の行のみ。
    """
    if session_factory is None:
        return []
    from database.models import BuildingMessage
    db = session_factory()
    try:
        q = db.query(BuildingMessage).filter_by(building_id=building_id)
        if after_seq is not None:
            q = q.filter(BuildingMessage.seq > after_seq)
        if limit is not None:
            # 末尾 N 件 → desc + limit + reverse
            rows = q.order_by(BuildingMessage.seq.desc()).limit(limit).all()
            rows = list(reversed(rows))
        else:
            rows = q.order_by(BuildingMessage.seq.asc()).all()
        return [deserialize_building_message(r) for r in rows]
    finally:
        db.close()


def fetch_max_seq(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
) -> int:
    """``building_id`` の最大 seq を返す (該当なし = 0)。"""
    if session_factory is None:
        return 0
    from database.models import BuildingMessage
    from sqlalchemy import func
    db = session_factory()
    try:
        val = db.query(func.max(BuildingMessage.seq)).filter_by(
            building_id=building_id
        ).scalar()
        return int(val) if val is not None else 0
    finally:
        db.close()


def mark_ingested(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    message_id: str,
    persona_id: str,
) -> bool:
    """指定メッセージの ingested_by に persona_id を追加する (idempotent)。

    Returns True if updated (= 新規追加された)、 False if skipped (= 既に登録済み /
    該当行なし / session_factory が None)。
    """
    if session_factory is None or not persona_id:
        return False
    from database.models import BuildingMessage
    db = session_factory()
    try:
        row = db.query(BuildingMessage).filter_by(
            building_id=building_id, message_id=message_id
        ).first()
        if row is None:
            return False
        ingested: List[str] = []
        if row.ingested_by:
            try:
                parsed = json.loads(row.ingested_by)
                if isinstance(parsed, list):
                    ingested = [str(p) for p in parsed if p]
            except json.JSONDecodeError:
                ingested = []
        if persona_id in ingested:
            return False
        ingested.append(persona_id)
        row.ingested_by = json.dumps(sorted(set(ingested)), ensure_ascii=False)
        db.commit()
        return True
    except Exception:
        db.rollback()
        LOGGER.warning(
            "mark_ingested failed: bid=%s msg_id=%s persona=%s",
            building_id, message_id, persona_id,
            exc_info=True,
        )
        return False
    finally:
        db.close()


def mark_event_recalled(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    event_key: str,
    persona_id: str,
) -> bool:
    """指定 event_key の event_data.recalled_by に persona_id を追加する。

    occupancy event の 「このペルソナはこの enter event をもう想起した」 マーカー。
    """
    if session_factory is None or not persona_id or not event_key:
        return False
    from database.models import BuildingMessage
    db = session_factory()
    try:
        rows = db.query(BuildingMessage).filter_by(
            building_id=building_id, event_type="occupancy",
        ).all()
        for row in rows:
            if not row.event_data:
                continue
            try:
                event_obj = json.loads(row.event_data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event_obj, dict):
                continue
            if event_obj.get("event_key") != event_key:
                continue
            recalled = event_obj.get("recalled_by") or []
            if not isinstance(recalled, list):
                recalled = []
            if persona_id in recalled:
                return True
            recalled.append(persona_id)
            event_obj["recalled_by"] = recalled
            row.event_data = json.dumps(event_obj, ensure_ascii=False, default=str)
            db.commit()
            return True
        return False
    except Exception:
        db.rollback()
        LOGGER.warning(
            "mark_event_recalled failed: bid=%s event_key=%s persona=%s",
            building_id, event_key, persona_id,
            exc_info=True,
        )
        return False
    finally:
        db.close()


def serialize_building_message(building_id: str, building_msg: Dict[str, Any]) -> Dict[str, Any]:
    """``building_histories`` の dict を ``building_messages`` のカラム dict へ変換。

    ``metadata.event`` は event_type / event_data へ構造化分離、 その他のメタデータは
    ``metadata_json`` に JSON 文字列として詰める。
    """
    metadata = building_msg.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    event_obj = metadata.get("event") if isinstance(metadata, dict) else None
    event_type: Optional[str] = None
    event_data_json: Optional[str] = None
    if isinstance(event_obj, dict):
        ev_type = event_obj.get("type")
        event_type = ev_type if isinstance(ev_type, str) else None
        event_data_json = json.dumps(event_obj, ensure_ascii=False, default=str)

    if isinstance(event_obj, dict):
        residual = {k: v for k, v in metadata.items() if k != "event"}
    else:
        residual = metadata
    metadata_json = json.dumps(residual, ensure_ascii=False, default=str) if residual else None

    heard_by = building_msg.get("heard_by") or []
    ingested_by = building_msg.get("ingested_by") or []

    try:
        seq_int = int(building_msg.get("seq", 0))
    except (TypeError, ValueError):
        seq_int = 0

    return {
        "building_id": building_id,
        "seq": seq_int,
        "role": building_msg.get("role") or "",
        "persona_id": building_msg.get("persona_id"),
        "content": building_msg.get("content") or "",
        "timestamp": building_msg.get("timestamp") or "",
        "heard_by": json.dumps(heard_by, ensure_ascii=False),
        "ingested_by": json.dumps(ingested_by, ensure_ascii=False),
        "event_type": event_type,
        "event_data": event_data_json,
        "metadata_json": metadata_json,
        "message_id": building_msg.get("message_id"),
        "client_message_id": building_msg.get("client_message_id"),
        "origin_track_id": building_msg.get("origin_track_id"),
        "pulse_id": building_msg.get("pulse_id"),
    }


def insert_building_message(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    building_msg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """``building_messages`` テーブルへ 1 件 INSERT し、 確定された行 dict を返す。

    seq / message_id は **DB 側で独立採番** する (max(seq)+1)。 呼び出し元 dict の
    seq / message_id は legacy_seq / legacy_message_id として保存される (= 過去
    JSON 由来の値を traceable に残す)。
    """
    if session_factory is None:
        return None
    try:
        from database.models import BuildingMessage
        from sqlalchemy import func as sa_func
        record = serialize_building_message(building_id, building_msg)
        db = session_factory()
        try:
            max_seq = db.query(sa_func.coalesce(sa_func.max(BuildingMessage.seq), 0)).filter_by(
                building_id=building_id
            ).scalar()
            new_seq = int(max_seq or 0) + 1
            new_message_id = f"{building_id}:{new_seq}"
            # 旧 seq / message_id を legacy_* に保存し、 新採番で上書き
            record["legacy_seq"] = record.get("seq") or None
            record["legacy_message_id"] = record.get("message_id") or None
            record["seq"] = new_seq
            record["message_id"] = new_message_id
            obj = BuildingMessage(**record)
            db.add(obj)
            db.commit()
            return deserialize_building_message(obj)
        finally:
            db.close()
    except Exception:
        LOGGER.warning(
            "Failed to insert building message to DB: building_id=%s msg_id=%s",
            building_id,
            building_msg.get("message_id"),
            exc_info=True,
        )
        return None


def update_building_message_in_db(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    message_id: str,
    *,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """``building_messages`` テーブルの既存行を update する dual-write 補助。"""
    if session_factory is None:
        return
    if content is None and metadata is None:
        return
    try:
        from database.models import BuildingMessage
        db = session_factory()
        try:
            obj = db.query(BuildingMessage).filter_by(
                building_id=building_id,
                message_id=message_id,
            ).first()
            if obj is None:
                LOGGER.debug(
                    "update_building_message DB mirror: row not found bid=%s msg_id=%s",
                    building_id, message_id,
                )
                return
            if content is not None:
                obj.content = content
            if metadata is not None and isinstance(metadata, dict):
                existing_meta: Dict[str, Any] = {}
                if obj.metadata_json:
                    try:
                        existing_meta = json.loads(obj.metadata_json) or {}
                    except json.JSONDecodeError:
                        LOGGER.warning(
                            "update_building_message DB mirror: existing metadata_json malformed; replacing"
                        )
                        existing_meta = {}
                event_obj = metadata.get("event")
                if isinstance(event_obj, dict):
                    ev_type = event_obj.get("type")
                    obj.event_type = ev_type if isinstance(ev_type, str) else None
                    obj.event_data = json.dumps(event_obj, ensure_ascii=False, default=str)
                residual_new = {k: v for k, v in metadata.items() if k != "event"}
                merged = {**existing_meta, **residual_new}
                obj.metadata_json = (
                    json.dumps(merged, ensure_ascii=False, default=str) if merged else None
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        LOGGER.warning(
            "Failed to update building message mirror in DB: building_id=%s msg_id=%s",
            building_id, message_id,
            exc_info=True,
        )
