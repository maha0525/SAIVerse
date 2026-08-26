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
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)


def _is_database_locked(exc: BaseException) -> bool:
    """SQLite ``database is locked`` 系のエラーか判定する。"""
    if isinstance(exc, sqlite3.OperationalError):
        return "database is locked" in str(exc)
    cause = getattr(exc, "__cause__", None) or getattr(exc, "orig", None)
    if isinstance(cause, sqlite3.OperationalError):
        return "database is locked" in str(cause)
    return False


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


def fetch_game_session_log(
    session_factory: Optional[Callable[[], "Session"]],
    building_ids: List[str],
    *,
    viewer_id: Optional[str] = None,
    since_ts: Optional[str] = None,
    after_message_id: Optional[str] = None,
    before_message_id: Optional[str] = None,
    limit: int = 50,
) -> "tuple[List[Dict[str, Any]], bool]":
    """複数 Building のメッセージを挿入順 (id) で merge した「セッションログビュー」。

    Region RPG のセッションログ用 (temp/region_rpg_intent.md §I-2、リポジトリ外管理)。
    書き込みは通常の building_messages 経路のまま、読み出しだけを合成する。

    - ``viewer_id``: heard_by に viewer_id を含む行のみ返す。セッションを跨いだ
      情報流入をクエリレベルで防ぐため必須運用 (時間窓だけでは過去ログ閲覧時に
      防壁にならない)。SQL LIKE で前置フィルタし、deserialize 後に厳密判定する。
    - ``since_ts``: timestamp >= since_ts (ISO 文字列比較、セッション開始以降)。
    - ``after_message_id``: その行より後を返す (ポーリング用)。
    - ``before_message_id``: その行より前を返す (scrollback 用)。
    - カーソルは Building 横断で単調な挿入順 PK (id) を内部使用し、外部 IF は
      message_id で受ける (チャット UI の既存 ID 体系と揃えるため)。

    Returns:
        (messages, has_more_older)。各 dict は building_id キーを含む。
    """
    if session_factory is None or not building_ids:
        return [], False
    from database.models import BuildingMessage
    db = session_factory()
    try:
        def _resolve_row_id(message_id: str) -> Optional[int]:
            row = (
                db.query(BuildingMessage.id)
                .filter(
                    BuildingMessage.building_id.in_(building_ids),
                    BuildingMessage.message_id == message_id,
                )
                .first()
            )
            return row[0] if row else None

        q = db.query(BuildingMessage).filter(
            BuildingMessage.building_id.in_(building_ids)
        )
        if since_ts:
            q = q.filter(BuildingMessage.timestamp >= since_ts)
        if viewer_id:
            q = q.filter(BuildingMessage.heard_by.like(f'%"{viewer_id}"%'))

        has_more = False
        if after_message_id:
            anchor = _resolve_row_id(after_message_id)
            if anchor is None:
                LOGGER.warning(
                    "fetch_game_session_log: after_message_id %s not found",
                    after_message_id,
                )
                return [], False
            rows = (
                q.filter(BuildingMessage.id > anchor)
                .order_by(BuildingMessage.id.asc())
                .limit(limit)
                .all()
            )
        elif before_message_id:
            anchor = _resolve_row_id(before_message_id)
            if anchor is None:
                LOGGER.warning(
                    "fetch_game_session_log: before_message_id %s not found",
                    before_message_id,
                )
                return [], False
            rows = (
                q.filter(BuildingMessage.id < anchor)
                .order_by(BuildingMessage.id.desc())
                .limit(limit + 1)
                .all()
            )
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        else:
            rows = (
                q.order_by(BuildingMessage.id.desc())
                .limit(limit + 1)
                .all()
            )
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))

        out: List[Dict[str, Any]] = []
        for r in rows:
            msg = deserialize_building_message(r)
            msg["building_id"] = r.building_id
            # LIKE は前置フィルタ。 厳密な heard_by 判定はここで行う
            # (誤マッチで除外された分だけ limit より少なく返ることがある)
            if viewer_id and viewer_id not in msg.get("heard_by", []):
                continue
            out.append(msg)
        return out, has_more
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

    DB エラーは **raise する** (W5/M8): マーク失敗を False に潰すと、呼び出し元
    (Building→個人記憶の転記) が失敗を跨いで cursor を確定し、未マークの
    メッセージが再試行されないまま取り込み済み扱いになる。「既に登録済み /
    該当行なし」は決着 (False)、「書けなかった」は失敗 (例外) — 二つは別物。
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
        raise
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
        # origin_track_id: 生きた書き手はもう無い (2026-08-21 の会話経路の Track
        # なし化で全経路が退役)。列と既存データの読み手が残っている間は「値が
        # 来たら書く」受け口だけ残す。掃除は Track テーブル退役の migration。
        "origin_track_id": building_msg.get("origin_track_id"),
        "pulse_id": building_msg.get("pulse_id"),
    }


def insert_building_message_in_session(
    db: "Session",
    building_id: str,
    building_msg: Dict[str, Any],
) -> Dict[str, Any]:
    """``building_messages`` へ 1 件 INSERT する **session 内核** (commit しない)。

    呼び出し元のトランザクションに同居する (W5/B1: 移動の位置遷移と
    leave/enter イベントを単一 commit にするための口)。seq / message_id の
    採番規則は :func:`insert_building_message` と同一 (max(seq)+1 — 呼び出し元
    tx が先行の書き込みで write ロックを取っていれば SQLite の単一書き手
    直列化により採番レースは起きない)。client_message_id の重複は既存行を
    返して INSERT しない。ロックリトライは行わない — 失敗は例外で表明し、
    呼び出し元 tx ごと失敗させる。

    Returns:
        確定された行 dict (``_was_inserted`` True/False)。
    """
    from database.models import BuildingMessage
    from sqlalchemy import func as sa_func

    record = serialize_building_message(building_id, building_msg)
    orig_seq = record.get("seq")
    orig_message_id = record.get("message_id")
    cmid = record.get("client_message_id")
    if cmid:
        existing = db.query(BuildingMessage).filter_by(
            client_message_id=cmid
        ).first()
        if existing is not None:
            LOGGER.info(
                "insert_building_message: duplicate client_message_id=%s → returning existing seq=%d",
                cmid, existing.seq,
            )
            result = deserialize_building_message(existing)
            result["_was_inserted"] = False
            return result

    max_seq = db.query(sa_func.coalesce(sa_func.max(BuildingMessage.seq), 0)).filter_by(
        building_id=building_id
    ).scalar()
    # 通常の発言は必ず 1 以上。過去ログの取り込みは 0 未満を使う
    # (saiverse/legacy_log_import.py) ので、その行しか無い部屋でも 0 や負に
    # 落ちないよう下限を敷く。
    new_seq = max(int(max_seq or 0), 0) + 1
    record["legacy_seq"] = orig_seq or None
    record["legacy_message_id"] = orig_message_id or None
    record["seq"] = new_seq
    record["message_id"] = f"{building_id}:{new_seq}"
    obj = BuildingMessage(**record)
    db.add(obj)
    # seq/一意制約の違反 (IntegrityError) を commit 前にこの場で顕在化させる
    db.flush()
    result = deserialize_building_message(obj)
    result["_was_inserted"] = True
    return result


def insert_building_message(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    building_msg: Dict[str, Any],
    _max_retries: int = 5,
) -> Optional[Dict[str, Any]]:
    """``building_messages`` テーブルへ 1 件 INSERT し、 確定された行 dict を返す。

    seq / message_id は **DB 側で独立採番** する (max(seq)+1)。 呼び出し元 dict の
    seq / message_id は legacy_seq / legacy_message_id として保存される (= 過去
    JSON 由来の値を traceable に残す)。

    ``database is locked`` 時はリトライする。INSERT の中身は
    :func:`insert_building_message_in_session` (session 内核) と共通。
    """
    if session_factory is None:
        return None

    from database.models import BuildingMessage
    from sqlalchemy.exc import IntegrityError

    for attempt in range(_max_retries):
        db = session_factory()
        try:
            try:
                result = insert_building_message_in_session(
                    db, building_id, building_msg
                )
                if result.get("_was_inserted"):
                    db.commit()
                return result
            except IntegrityError:
                db.rollback()
                cmid = building_msg.get("client_message_id")
                if cmid:
                    existing = db.query(BuildingMessage).filter_by(
                        client_message_id=cmid
                    ).first()
                    if existing is not None:
                        LOGGER.info(
                            "insert_building_message: race-resolved duplicate cmid=%s → existing seq=%d",
                            cmid, existing.seq,
                        )
                        result = deserialize_building_message(existing)
                        result["_was_inserted"] = False
                        return result
                raise
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            if _is_database_locked(exc) and attempt < _max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                LOGGER.warning(
                    "insert_building_message: database locked (attempt %d/%d), "
                    "retrying in %.1fs: bid=%s",
                    attempt + 1, _max_retries, wait, building_id,
                )
                time.sleep(wait)
                continue
            LOGGER.warning(
                "Failed to insert building message to DB: building_id=%s msg_id=%s",
                building_id,
                building_msg.get("message_id"),
                exc_info=True,
            )
            return None
        finally:
            db.close()
    return None


def insert_building_message_with_location_guard(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    building_msg: Dict[str, Any],
    *,
    user_id: Any,
    expected_building_id: str,
    _max_retries: int = 5,
) -> Optional[Dict[str, Any]]:
    """ユーザー発言の INSERT を「現在地検証と同一トランザクション」で行う。

    W7 柱5 (2026-07-21 Codex 第六巡 P1): 発言境界の照合 (in-memory state) と
    INSERT の間に別デバイスの移動が確定すると、照合済みの旧 Building へ発言が
    永続化されその部屋のペルソナへの Pulse まで起動する。ここでは
    **無変化 UPDATE で write ロックを先取り**してから User.CURRENT_BUILDINGID を
    読むことで、検証〜commit の間に移動 (別の書き手) が割り込めないことを
    SQLite の単一書き手直列化で保証する。

    user 行が引けない環境 (テストスタブ等) は検証をスキップして従来どおり
    保存する (fail-open — 単一位置モデルの執行は user 行がある本番形でのみ
    意味を持つ)。

    Returns:
        確定行 dict (``_was_inserted`` 付き) / ``None`` (保存失敗) /
        ``{"_location_conflict": True, "current_building_id": ...}``
        (現在地不一致 — **何も書いていない**)。
    """
    if session_factory is None:
        return None

    from sqlalchemy import text as sa_text
    from database.models import BuildingMessage
    from sqlalchemy.exc import IntegrityError

    for attempt in range(_max_retries):
        db = session_factory()
        try:
            try:
                # 無変化 UPDATE で write ロックを取る (SELECT は pysqlite の
                # autocommit 挙動でトランザクション外に出るため、先に書き手に
                # なっておかないと read-then-write の競合窓が残る)
                db.execute(sa_text(
                    "UPDATE user SET CURRENT_BUILDINGID = CURRENT_BUILDINGID "
                    "WHERE USERID = :uid"
                ), {"uid": user_id})
                row = db.execute(sa_text(
                    "SELECT CURRENT_BUILDINGID FROM user WHERE USERID = :uid"
                ), {"uid": user_id}).fetchone()
                if row is not None and row[0] is not None \
                        and row[0] != expected_building_id:
                    db.rollback()
                    LOGGER.warning(
                        "insert_building_message_with_location_guard: user %s "
                        "moved to %s during dispatch (expected %s) — refusing",
                        user_id, row[0], expected_building_id,
                    )
                    return {
                        "_location_conflict": True,
                        "current_building_id": row[0],
                    }
                result = insert_building_message_in_session(
                    db, building_id, building_msg
                )
                if result.get("_was_inserted"):
                    db.commit()
                else:
                    db.rollback()
                return result
            except IntegrityError:
                db.rollback()
                cmid = building_msg.get("client_message_id")
                if cmid:
                    existing = db.query(BuildingMessage).filter_by(
                        client_message_id=cmid
                    ).first()
                    if existing is not None:
                        result = deserialize_building_message(existing)
                        result["_was_inserted"] = False
                        return result
                raise
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            if _is_database_locked(exc) and attempt < _max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                LOGGER.warning(
                    "insert_building_message_with_location_guard: database "
                    "locked (attempt %d/%d), retrying in %.1fs: bid=%s",
                    attempt + 1, _max_retries, wait, building_id,
                )
                time.sleep(wait)
                continue
            LOGGER.warning(
                "Failed to insert guarded building message: building_id=%s",
                building_id, exc_info=True,
            )
            return None
        finally:
            db.close()
    return None


def update_building_message_in_db(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    message_id: str,
    *,
    content: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    _max_retries: int = 5,
) -> None:
    """``building_messages`` テーブルの既存行を update する。

    ``database is locked`` (SQLite ロック競合) 時はリトライする。
    ストリーミング placeholder の finalize など、消失が許されない更新がある。
    """
    if session_factory is None:
        return
    if content is None and metadata is None:
        return

    from database.models import BuildingMessage

    for attempt in range(_max_retries):
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
            return
        except Exception as exc:
            db.rollback()
            if _is_database_locked(exc) and attempt < _max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                LOGGER.warning(
                    "update_building_message: database locked (attempt %d/%d), "
                    "retrying in %.1fs: bid=%s msg_id=%s",
                    attempt + 1, _max_retries, wait, building_id, message_id,
                )
                time.sleep(wait)
                continue
            LOGGER.warning(
                "Failed to update building message in DB: building_id=%s msg_id=%s",
                building_id, message_id,
                exc_info=True,
            )
            return
        finally:
            db.close()


def withdraw_building_message_in_db(
    session_factory: Optional[Callable[[], "Session"]],
    building_id: str,
    message_id: str,
    *,
    expected_role: str = "user",
    _max_retries: int = 5,
) -> Tuple[bool, str, Optional[str]]:
    """まだ誰の記憶にも入っていないユーザー発言を、建物の記録から取り下げる。

    **世界の記録から行を消す唯一の経路**なので、条件は狭く固定する:

    - ``role`` が ``expected_role`` (既定はユーザー発言) であること
    - ``ingested_by`` が空であること — 一人でも記憶へ転記していたら消さない。
      消すと、そのペルソナは「聞いた覚えがあるのに記録が無い」状態になり、
      無言で消えるより悪い (2026-08-25 まはー裁定)

    ``ingested_by`` を見るのは、取り込みの目盛り (pulse_cursors) からの推測ではなく
    **実際に転記された記録そのもの**だから。目盛りが先へ進んでいても転記されて
    いない行は、以後どのペルソナにも読まれないので取り下げてよい。

    Returns:
        ``(取り下げたか, 理由コード, 取り下げた本文)``。理由コードは
        ``"withdrawn"`` / ``"not_found"`` / ``"already_heard"`` / ``"wrong_role"``
        / ``"unavailable"``。
    """
    if session_factory is None:
        return (False, "unavailable", None)

    from database.models import BuildingMessage

    for attempt in range(_max_retries):
        db = session_factory()
        try:
            obj = db.query(BuildingMessage).filter_by(
                building_id=building_id,
                message_id=message_id,
            ).first()
            if obj is None:
                return (False, "not_found", None)
            if obj.role != expected_role:
                return (False, "wrong_role", None)
            heard = []
            if obj.ingested_by:
                try:
                    parsed = json.loads(obj.ingested_by)
                except json.JSONDecodeError:
                    # 読めない = 判断できない。消さない方へ倒す。
                    LOGGER.warning(
                        "withdraw_building_message: ingested_by is malformed; "
                        "refusing to withdraw bid=%s msg_id=%s",
                        building_id, message_id,
                    )
                    return (False, "already_heard", None)
                if not isinstance(parsed, list):
                    # JSON としては読めたが、配列ではない (``{}`` / ``null`` /
                    # 数値など)。この列の意味が壊れている状態で、**「空の配列」と
                    # 同じには扱えない** — 誰が読んだのかを判断できないのだから、
                    # 読めなかったときと同じく消さない方へ倒す。
                    LOGGER.warning(
                        "withdraw_building_message: ingested_by is not a list "
                        "(%r); refusing to withdraw bid=%s msg_id=%s",
                        type(parsed).__name__, building_id, message_id,
                    )
                    return (False, "already_heard", None)
                heard = [str(p) for p in parsed if p]
            if heard:
                return (False, "already_heard", None)
            content = obj.content
            # 「誰も読んでいない」を確かめてから消すまでの間に、ペルソナの取り込みが
            # 走ることがある。**確かめた時点の値を消す条件に入れて**、変わっていたら
            # 0 行で終わらせる。そうしないと、読まれた発言を取り消せてしまい、
            # ペルソナの記憶には残ったまま建物の記録だけが消える。
            original_ingested = obj.ingested_by
            deleted = db.query(BuildingMessage).filter(
                BuildingMessage.building_id == building_id,
                BuildingMessage.message_id == message_id,
                BuildingMessage.ingested_by == original_ingested,
            ).delete(synchronize_session=False)
            db.commit()
            if not deleted:
                LOGGER.info(
                    "withdraw_building_message: someone read it while we were "
                    "checking; leaving it in place bid=%s msg_id=%s",
                    building_id, message_id,
                )
                return (False, "already_heard", None)
            LOGGER.info(
                "withdraw_building_message: removed bid=%s msg_id=%s (nobody had read it)",
                building_id, message_id,
            )
            return (True, "withdrawn", content)
        except Exception as exc:
            db.rollback()
            if _is_database_locked(exc) and attempt < _max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                LOGGER.warning(
                    "withdraw_building_message: database locked (attempt %d/%d), "
                    "retrying in %.1fs: bid=%s msg_id=%s",
                    attempt + 1, _max_retries, wait, building_id, message_id,
                )
                time.sleep(wait)
                continue
            LOGGER.warning(
                "Failed to withdraw building message: building_id=%s msg_id=%s",
                building_id, message_id,
                exc_info=True,
            )
            return (False, "unavailable", None)
        finally:
            db.close()
    return (False, "unavailable", None)
