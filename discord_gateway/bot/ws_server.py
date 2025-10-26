from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from pydantic import ValidationError
from websockets.server import WebSocketServerProtocol

from .config import BotSettings
from .connection_manager import ConnectedClient, ConnectionManager
from .security import HandshakePayload

logger = logging.getLogger(__name__)


class GatewayWebSocketServer:
    """Secure WebSocket server that accepts connections from local apps."""

    def __init__(self, settings: BotSettings, connections: ConnectionManager):
        self._settings = settings
        self._connections = connections
        self._server = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("WebSocket server already started")

        self._server = await websockets.serve(
            ws_handler=self._handle_client,
            host=self._settings.websocket_host,
            port=self._settings.websocket_port,
            create_protocol=None,
            ping_interval=self._settings.websocket_heartbeat_seconds,
            max_size=self._settings.websocket_max_size,
            process_request=self._process_request,
        )
        logger.info(
            "Gateway WebSocket server listening on %s:%s%s",
            self._settings.websocket_host,
            self._settings.websocket_port,
            self._settings.websocket_path,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Gateway WebSocket server stopped.")

    async def _process_request(self, path: str, request_headers: dict[str, Any]) -> Any:
        """Reject connections that do not match the configured path."""

        if path != self._settings.websocket_path:
            logger.warning("Rejected connection with unexpected path: %s", path)
            return (404, [], b"Not Found")
        return None

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        client: ConnectedClient | None = None
        try:
            handshake_raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            try:
                handshake_data = json.loads(handshake_raw)
            except json.JSONDecodeError:
                await websocket.close(code=4000, reason="Invalid handshake payload")
                return
            try:
                handshake = HandshakePayload.model_validate(handshake_data)
            except ValidationError as exc:
                logger.warning("Invalid handshake payload: %s", exc)
                await websocket.close(code=4000, reason="Invalid handshake payload")
                return

            client = await self._connections.authenticate(handshake.token, websocket)
            if not client:
                await websocket.close(code=4003, reason="Invalid token")
                return

            await websocket.send(
                json.dumps(
                    {"type": "hello_ack", "status": "ok", "session_id": client.session.session_id}
                )
            )

            await self._receive_loop(client)
        except TimeoutError:
            logger.warning("WebSocket handshake timeout")
            await websocket.close(code=4004, reason="Handshake timeout")
        except websockets.ConnectionClosedOK:
            pass
        except websockets.ConnectionClosedError:
            pass
        except Exception:
            logger.exception("Unexpected server error during WebSocket session")
            if websocket.open:
                await websocket.close(code=1011, reason="Internal error")
        finally:
            if client:
                await self._connections.unregister(client)

    async def _receive_loop(self, client: ConnectedClient) -> None:
        websocket = client.websocket
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("Received malformed JSON payload: %s", raw_message)
                continue

            event_type = message.get("type")
            if event_type == "heartbeat":
                await self._connections.heartbeat(client)
                await websocket.send(json.dumps({"type": "heartbeat_ack"}))
            else:
                logger.debug(
                    "Ignoring unhandled client message type=%s payload=%s",
                    event_type,
                    message,
                )
