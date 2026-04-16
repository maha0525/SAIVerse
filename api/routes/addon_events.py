"""アドオンイベント用常設 SSE エンドポイント。

フロントエンドは EventSource で接続し続け、アドオンからの非同期イベントを受け取る。
チャット応答用の NDJSON ストリームとは独立した、長命なコネクション。
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_KEEPALIVE_INTERVAL = 25  # seconds


@router.get("/events")
async def addon_events():
    """アドオンイベントの常設 SSE エンドポイント。

    フロントエンドは EventSource('/api/addon/events') で接続する。
    サーバー送信イベント形式 (text/event-stream) で配信する。
    """
    from saiverse.addon_events import subscribe, unsubscribe

    queue: asyncio.Queue = asyncio.Queue()
    subscribe(queue)
    LOGGER.debug("addon_events: new SSE client connected")

    async def generate():
        try:
            # 初回 keep-alive で接続を確立させる
            yield ": keep-alive\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL)
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"data: {data}\n\n"
                    LOGGER.debug("addon_events: sent event type=%s", event.get("type"))
                except asyncio.TimeoutError:
                    # 接続維持のための keep-alive コメント
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            LOGGER.debug("addon_events: SSE client disconnected (cancelled)")
        except Exception:
            LOGGER.exception("addon_events: unexpected error in SSE generator")
        finally:
            unsubscribe(queue)
            LOGGER.debug("addon_events: SSE client cleanup done")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
