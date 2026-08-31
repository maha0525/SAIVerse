"""アドオンメタデータ管理モジュール。

チャットメッセージに紐付くアドオン固有のメタデータを読み書きする。
拡張パックからは以下のようにインポートして使用する:

    from saiverse.addon_metadata import set_metadata, get_metadata

アドオンが無効でもデータは保持される。無効時は参照側（フロントエンド等）が
アドオンの有効状態を確認してスキップする。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)


def _get_session():
    from database.session import SessionLocal
    return SessionLocal()


def set_metadata(message_id: str, addon_name: str, key: str, value: Any) -> None:
    """メッセージIDに対してアドオンメタデータをセットする。

    同一の (message_id, addon_name, key) が既にあれば上書きする。
    value は文字列以外も受け付ける（JSONシリアライズして保存）。
    """
    from database.models import AddonMessageMetadata
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)

    db = _get_session()
    try:
        stmt = sqlite_insert(AddonMessageMetadata).values(
            message_id=message_id,
            addon_name=addon_name,
            key=key,
            value=value,
        ).on_conflict_do_update(
            index_elements=["message_id", "addon_name", "key"],
            set_={"value": value},
        )
        db.execute(stmt)
        db.commit()
        LOGGER.debug(
            "addon_metadata: set message_id=%s addon=%s key=%s",
            message_id, addon_name, key,
        )
    except Exception:
        db.rollback()
        LOGGER.exception(
            "addon_metadata: failed to set message_id=%s addon=%s key=%s",
            message_id, addon_name, key,
        )
        raise
    finally:
        db.close()


def get_metadata(message_id: str, addon_name: str) -> Dict[str, Any]:
    """指定メッセージの指定アドオンのメタデータを全件取得する。

    Returns:
        {key: value, ...} の辞書。値はJSONデシリアライズを試みる。
    """
    from database.models import AddonMessageMetadata

    db = _get_session()
    try:
        rows = (
            db.query(AddonMessageMetadata)
            .filter(
                AddonMessageMetadata.message_id == message_id,
                AddonMessageMetadata.addon_name == addon_name,
            )
            .all()
        )
        result: Dict[str, Any] = {}
        for row in rows:
            try:
                result[row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                result[row.key] = row.value
        return result
    finally:
        db.close()


def get_metadata_value(
    message_id: str, addon_name: str, key: str
) -> Optional[Any]:
    """指定メッセージの指定アドオンの特定キーの値を取得する。"""
    from database.models import AddonMessageMetadata

    db = _get_session()
    try:
        row = (
            db.query(AddonMessageMetadata)
            .filter(
                AddonMessageMetadata.message_id == message_id,
                AddonMessageMetadata.addon_name == addon_name,
                AddonMessageMetadata.key == key,
            )
            .first()
        )
        if row is None:
            return None
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return row.value
    finally:
        db.close()
