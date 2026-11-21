"""常設 SSE チャネル (GET /api/addon/events) のブロードキャスト管理モジュール。

チャット応答の NDJSON ストリームとは独立した、つなぎっぱなしの SSE チャネルを
提供する。フロントエンドは GET /api/addon/events に EventSource で接続し続ける
(URL はアドオンが使っているので変えない)。載る知らせは ``type`` で分かれる:

- ``type: "addon_event"`` — アドオンの非同期イベント。拡張パックからは以下のように
  インポートして使用する::

      from saiverse.addon_events import emit_addon_event

- ``type: "runtime_alert"`` — サーバーの動作中に見つけた、部屋に結びつかない警告
  (``saiverse/runtime_alerts.py`` が ``broadcast_event`` で送る。
  docs/intent/runtime_alerts.md)。

受け手がいない間に送った知らせは保持しない (届かない)。取りこぼしてはいけない
知らせは、送り手の側で保存して、画面が開いた・繋ぎ直したときに一覧で取らせる。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# 接続中のクライアントキューのリスト
_subscribers: List[asyncio.Queue] = []
# メインスレッドのイベントループ（非asyncioスレッドからのemit用）
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """起動時にメインのイベントループを登録する。

    main.py の uvicorn 起動前に呼ぶことで、バックグラウンドスレッドからの
    emit_addon_event が正しくキューに積めるようになる。
    """
    global _event_loop
    _event_loop = loop
    LOGGER.debug("addon_events: event loop registered")


def subscribe(queue: asyncio.Queue) -> None:
    """SSEクライアントのキューを登録する。"""
    _subscribers.append(queue)
    LOGGER.debug("addon_events: subscriber added (total=%d)", len(_subscribers))


def unsubscribe(queue: asyncio.Queue) -> None:
    """SSEクライアントのキューを登録解除する。"""
    try:
        _subscribers.remove(queue)
    except ValueError:
        pass
    LOGGER.debug("addon_events: subscriber removed (total=%d)", len(_subscribers))


def emit_addon_event(
    addon: str,
    event: str,
    data: Optional[Dict[str, Any]] = None,
    message_id: Optional[str] = None,
) -> None:
    """アドオンイベントを全接続クライアントにブロードキャストする。

    asyncio スレッドからも非asyncioスレッド（バックグラウンドTTSワーカー等）
    からも安全に呼べる。

    Args:
        addon: アドオン名（例: "saiverse-voice-tts"）
        event: イベント種別（例: "audio_ready"）
        data: イベントに付随するデータ辞書
        message_id: 関連するメッセージID（任意）
    """
    payload: Dict[str, Any] = {
        "type": "addon_event",
        "addon": addon,
        "event": event,
    }
    if message_id is not None:
        payload["message_id"] = message_id
    if data:
        payload["data"] = data

    LOGGER.debug(
        "addon_events: emit addon=%s event=%s message_id=%s subscribers=%d",
        addon, event, message_id, len(_subscribers),
    )
    broadcast_event(payload)


def broadcast_event(payload: Dict[str, Any]) -> None:
    """``payload`` をそのまま全接続クライアントにブロードキャストする。

    ``payload`` には画面側が振り分けに使う ``type`` を必ず入れる。
    asyncio スレッドからも非asyncioスレッドからも安全に呼べる。受け手が
    いなければ何もしない。
    """
    if not _subscribers:
        return

    loop = _event_loop
    if loop is None:
        LOGGER.warning("addon_events: event loop not registered, event dropped")
        return

    # asyncioスレッド内なら直接put_nowait、外からは thread-safe に積む
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is loop:
        # 同一ループ内から呼ばれた場合
        for q in list(_subscribers):
            q.put_nowait(payload)
    else:
        # バックグラウンドスレッドから呼ばれた場合
        for q in list(_subscribers):
            loop.call_soon_threadsafe(q.put_nowait, payload)
