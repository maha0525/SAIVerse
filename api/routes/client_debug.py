"""Client-side debug log relay.

フロントエンド (特にモバイルブラウザ) で DevTools が簡単に使えない状況で
console.log の代わりにサーバーへ POST してバックエンドログに記録する。

本番でも有効で害はないが、運用でノイズが気になる場合は
`SAIVERSE_CLIENT_DEBUG_LOG=0` で無効化できる。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

LOGGER = logging.getLogger("saiverse.client_debug")
router = APIRouter()


class ClientDebugPayload(BaseModel):
    level: Optional[str] = "info"  # "debug" | "info" | "warn" | "error"
    message: str = ""
    context: Optional[Dict[str, Any]] = None
    source: Optional[str] = None  # 呼び出し箇所識別用 (任意タグ)


_ENABLED = os.environ.get("SAIVERSE_CLIENT_DEBUG_LOG", "1") not in ("0", "false", "False")


@router.post("/log")
async def client_debug_log(body: ClientDebugPayload) -> dict:
    """Log a client-side debug message to the backend log."""
    if not _ENABLED:
        return {"ok": True, "logged": False}

    level = (body.level or "info").lower()
    source = body.source or "-"
    context = body.context or {}

    msg = f"[client] source={source} msg={body.message} ctx={context}"

    if level == "error":
        LOGGER.error(msg)
    elif level in ("warn", "warning"):
        LOGGER.warning(msg)
    elif level == "debug":
        LOGGER.debug(msg)
    else:
        LOGGER.info(msg)

    return {"ok": True, "logged": True}
