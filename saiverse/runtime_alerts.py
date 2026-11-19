"""サーバーの動作中に見つけた、部屋に結びつかない警告 (docs/intent/runtime_alerts.md)。

画面の右下に出て、ユーザーが閉じるまで残る知らせを作る。保存先は DB の
``runtime_alerts`` 表 (``database.models.RuntimeAlert``)、即時の配信は常設 SSE
(``saiverse.addon_events.broadcast_event``、``type: "runtime_alert"``)。ブラウザが
繋がっていなかった間の分は、画面が ``GET /api/system/runtime-alerts`` で取り直す。

- 書き込みは ``post_runtime_alert`` だけ。どのスレッドからでも呼べる (スケジュールの
  処理や処理待ちの列から走る実行は Web の処理と別のスレッドで動くため)。
- 同じ ``key`` でまだ閉じていない行があれば、新しい行を作らず回数・最後の時刻・
  見出し・本文・詳細を更新する。閉じた後に起きたら新しい行になる。
- 見出しと本文は ``api.ui_messages.ui_message`` の形で渡す (文言は
  ``frontend/src/i18n/messages.json`` に置く)。閉じるまでアップデートをまたいで
  残るので、ここで使う文言のキーは後から名前を変えない。
- 画面だけの知らせ。部屋の記録にもペルソナの記憶にも書かない。

書き込み先は起動時に ``configure`` で受け取る (``SAIVerseManager`` の
``_init_database`` が自分の ``SessionLocal`` を渡す)。受け取っていないプロセス
(pytest など) では保存も配信もせず、警告ログだけ出す — ``database.session`` の
モジュール共通の ``SessionLocal`` には倒さない (本番の DB を指しているため。
``saiverse/usage_tracker.py`` と同じ判断)。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

LEVELS = ("warning", "error")

_session_factory: Optional[Callable[[], Any]] = None
_warned_unconfigured = False
# 同じキーの「読んでから書く」を一本にする (同じプロセスの別スレッドが同時に同じ
# 警告を出しても、閉じていない行が二行にならないように)。DB の部分一意 index
# ``uq_runtime_alerts_open_key`` が同じ不変条件の最後の守り。
_upsert_lock = threading.Lock()


class RuntimeAlertsUnavailable(RuntimeError):
    """書き込み先がまだ受け取られていない (``configure`` 前)。"""


def configure(session_factory: Optional[Callable[[], Any]]) -> None:
    """警告を書く DB のセッションの作り方を受け取る。``None`` で外す。"""
    global _session_factory, _warned_unconfigured
    _session_factory = session_factory
    _warned_unconfigured = False
    LOGGER.debug("runtime_alerts: session factory %s", "configured" if session_factory else "cleared")


def _require_factory() -> Callable[[], Any]:
    if _session_factory is None:
        raise RuntimeAlertsUnavailable("runtime_alerts is not configured")
    return _session_factory


def _is_ui_message(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("$ui"), str)
        and isinstance(value.get("params"), dict)
        and set(value) == {"$ui", "params"}
    )


def _load_json(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        LOGGER.warning("runtime_alerts: stored JSON could not be read: %r", text[:200])
        return None


def serialize_alert(row: Any) -> Dict[str, Any]:
    """``RuntimeAlert`` の行を、API と SSE で送る形にする。

    ``title`` / ``message`` は ``ui_message`` の形のまま返す (画面の
    ``decodeUIMessages`` が表示用の文へ変える。``content`` / ``metadata`` の中は
    変換の対象にならないので、その名前の項目には置かない)。時刻は epoch 秒。
    """
    return {
        "id": row.ALERT_ID,
        "key": row.ALERT_KEY,
        "level": row.LEVEL,
        "title": _load_json(row.TITLE_JSON),
        "message": _load_json(row.MESSAGE_JSON),
        "details": _load_json(row.DETAILS_JSON),
        "count": row.OCCURRENCE_COUNT,
        "first_at": row.FIRST_AT,
        "last_at": row.LAST_AT,
    }


def _broadcast(payload: Dict[str, Any]) -> None:
    try:
        from saiverse.addon_events import broadcast_event

        broadcast_event(payload)
    except Exception:  # noqa: BLE001 — 配信の失敗で呼び出し元を止めない (保存は済んでいる)
        LOGGER.warning("runtime_alerts: broadcast failed", exc_info=True)


def _upsert(factory: Callable[[], Any], key: str, level: str, title_json: str,
            message_json: str, details_json: Optional[str], now: int) -> Dict[str, Any]:
    from database.models import RuntimeAlert

    db = factory()
    try:
        row = (
            db.query(RuntimeAlert)
            .filter(RuntimeAlert.ALERT_KEY == key, RuntimeAlert.DISMISSED_AT.is_(None))
            .first()
        )
        if row is None:
            row = RuntimeAlert(
                ALERT_KEY=key,
                LEVEL=level,
                TITLE_JSON=title_json,
                MESSAGE_JSON=message_json,
                DETAILS_JSON=details_json,
                OCCURRENCE_COUNT=1,
                FIRST_AT=now,
                LAST_AT=now,
            )
            db.add(row)
        else:
            row.LEVEL = level
            row.TITLE_JSON = title_json
            row.MESSAGE_JSON = message_json
            row.DETAILS_JSON = details_json
            row.OCCURRENCE_COUNT = (row.OCCURRENCE_COUNT or 0) + 1
            row.LAST_AT = now
        db.commit()
        db.refresh(row)
        return serialize_alert(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def post_runtime_alert(
    key: str,
    level: str,
    title: Dict[str, Any],
    message: Dict[str, Any],
    details: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """部屋に結びつかない警告を一件出す (または同じ ``key`` の閉じていない一枚を更新する)。

    Args:
        key: まとめるための識別キー。「何が・どの対象で・どんな原因で」起きたかを
            入れる (例: ``memorize_failed:<persona_id>``)。
        level: ``"warning"`` か ``"error"``。
        title: 見出し。``ui_message(...)`` の戻り値。
        message: 本文。``ui_message(...)`` の戻り値。
        details: 任意の詳細 (JSON にできる値)。最後の発生の内容で置き換わる。

    Returns:
        保存した知らせ (``serialize_alert`` の形)。書き込み先が無い・DB に書けな
        かったときは ``None`` (ログを出すだけで、呼び出し元へは例外を上げない)。

    Raises:
        ValueError: ``level`` か ``title`` / ``message`` の形が契約と違う (呼び出し側の
            書き間違い。黙って直さない)。
    """
    global _warned_unconfigured
    if level not in LEVELS:
        raise ValueError(f"runtime alert level must be one of {LEVELS}: {level!r}")
    if not _is_ui_message(title) or not _is_ui_message(message):
        raise ValueError("runtime alert title and message must be ui_message envelopes")
    if not key:
        raise ValueError("runtime alert key must not be empty")

    try:
        title_json = json.dumps(title, ensure_ascii=False)
        message_json = json.dumps(message, ensure_ascii=False)
        details_json = json.dumps(details, ensure_ascii=False, default=str) if details is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"runtime alert content is not JSON serializable: {exc}") from exc

    factory = _session_factory
    if factory is None:
        if not _warned_unconfigured:
            _warned_unconfigured = True
            LOGGER.warning(
                "runtime_alerts is not configured; dropping alert key=%s. "
                "Call configure() at startup to persist runtime alerts.", key,
            )
        else:
            LOGGER.debug("runtime_alerts not configured; dropped alert key=%s", key)
        return None

    now = int(time.time())
    try:
        with _upsert_lock:
            try:
                alert = _upsert(factory, key, level, title_json, message_json, details_json, now)
            except Exception as exc:
                # 別の経路が同じキーの行を先に作っていた (部分一意 index に当たった)
                # 場合は、読み直して更新側で一度だけやり直す。
                from sqlalchemy.exc import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                alert = _upsert(factory, key, level, title_json, message_json, details_json, now)
    except Exception:  # noqa: BLE001 — 警告の保存の失敗で呼び出し元の処理を止めない
        LOGGER.error("runtime_alerts: failed to store alert key=%s", key, exc_info=True)
        return None

    LOGGER.info(
        "runtime_alerts: posted key=%s level=%s count=%s id=%s",
        key, level, alert["count"], alert["id"],
    )
    _broadcast({"type": "runtime_alert", "action": "upsert", "alert": alert})
    return alert


def list_runtime_alerts() -> List[Dict[str, Any]]:
    """閉じていない知らせを、最後に起きた時刻の新しい順に返す。

    Raises:
        RuntimeAlertsUnavailable: 書き込み先をまだ受け取っていない。
    """
    from database.models import RuntimeAlert

    factory = _require_factory()
    db = factory()
    try:
        rows = (
            db.query(RuntimeAlert)
            .filter(RuntimeAlert.DISMISSED_AT.is_(None))
            .order_by(RuntimeAlert.LAST_AT.desc(), RuntimeAlert.ALERT_ID.desc())
            .all()
        )
        return [serialize_alert(row) for row in rows]
    finally:
        db.close()


def dismiss_runtime_alert(alert_id: int) -> Optional[bool]:
    """知らせを閉じる。閉じた時刻を書き、繋がっている全ブラウザへ「閉じた」を送る。

    Returns:
        ``True`` — いま閉じた。``False`` — 既に閉じてあった (何も変えない)。
        ``None`` — その番号の知らせが無い。

    Raises:
        RuntimeAlertsUnavailable: 書き込み先をまだ受け取っていない。
    """
    from database.models import RuntimeAlert

    factory = _require_factory()
    db = factory()
    try:
        row = db.query(RuntimeAlert).filter(RuntimeAlert.ALERT_ID == alert_id).first()
        if row is None:
            return None
        if row.DISMISSED_AT is not None:
            return False
        row.DISMISSED_AT = int(time.time())
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    LOGGER.info("runtime_alerts: dismissed id=%s", alert_id)
    _broadcast({"type": "runtime_alert", "action": "dismiss", "id": alert_id})
    return True
