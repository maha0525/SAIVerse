from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from .config import GatewaySettings

logger = logging.getLogger(__name__)


class GatewayClientError(RuntimeError):
    """Gatewayクライアントの致命的なエラー。"""


class HandshakeError(GatewayClientError):
    """Botとのハンドシェイクに失敗した場合の例外。"""


class WebSocketGatewayClient:
    """Discord Bot のGateway WebSocketと通信する低レベルクライアント。"""

    def __init__(self, settings: GatewaySettings):
        self._settings = settings
        self._ws: WebSocketClientProtocol | None = None

    async def __aenter__(self) -> WebSocketGatewayClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        logger.debug("Connecting to gateway %s", self._settings.bot_ws_url)
        self._ws = await websockets.connect(
            self._settings.bot_ws_url,
            max_size=self._settings.max_payload_bytes,
        )

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def handshake(self, token: str) -> dict[str, Any]:
        if self._ws is None:
            raise GatewayClientError("WebSocket is not connected")

        handshake_payload = {"type": "hello", "token": token}
        await self.send_json(handshake_payload)

        try:
            response = await asyncio.wait_for(
                self.recv_json(), timeout=self._settings.handshake_timeout_seconds
            )
        except TimeoutError as exc:
            raise HandshakeError("Gateway handshake timed out") from exc

        if response.get("type") != "hello_ack" or response.get("status") != "ok":
            raise HandshakeError(f"Gateway handshake failed: {response}")
        return response

    async def recv_json(self) -> dict[str, Any]:
        if self._ws is None:
            raise GatewayClientError("WebSocket is not connected")

        raw = await asyncio.wait_for(self._ws.recv(), timeout=self._settings.recv_timeout_seconds)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise GatewayClientError("WebSocket is not connected")

        await self._ws.send(json.dumps(payload))
