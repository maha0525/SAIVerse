"""Building ログを ``building_messages`` テーブルへ書き込むための共通 helper。

Phase 1 (dual-write 期間) では JSON 側の log.json が source of truth。 本モジュールは
``saiverse.db`` テーブルへの mirror 書き込みを行う最小 API を提供する。
失敗時は WARNING で続行し、 JSON 側の書き込みを巻き戻さない。

呼び出し元:
- ``persona/history_manager.py``: add_message / add_to_building_only / update_building_message
- ``manager/history.py``: add_building_event (OccupancyManager 経路)

詳細: docs/intent/building_memory_unified.md
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)


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
) -> None:
    """``building_messages`` テーブルへ 1 件 INSERT する dual-write 補助。"""
    if session_factory is None:
        return
    try:
        from database.models import BuildingMessage
        record = serialize_building_message(building_id, building_msg)
        db = session_factory()
        try:
            obj = BuildingMessage(**record)
            db.add(obj)
            db.commit()
        finally:
            db.close()
    except Exception:
        LOGGER.warning(
            "Failed to mirror building message to DB: building_id=%s seq=%s msg_id=%s",
            building_id,
            building_msg.get("seq"),
            building_msg.get("message_id"),
            exc_info=True,
        )


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
