from __future__ import annotations

import asyncio
import json
import logging
import ssl
from pathlib import Path
from typing import Any

import websockets
from pydantic import ValidationError
from websockets.server import WebSocketServerProtocol

from .command_processor import CommandProcessor
from .config import BotSettings
from .connection_manager import ConnectedClient, ConnectionManager
from .security import HandshakePayload

logger = logging.getLogger(__name__)


class GatewayWebSocketServer:
    """Secure WebSocket server that accepts connections from local apps."""

    def __init__(
        self,
        settings: BotSettings,
        connections: ConnectionManager,
        *,
        command_processor: CommandProcessor,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self._settings = settings
        self._connections = connections
        self._command_processor = command_processor
        self._external_ssl_context = ssl_context
        self._server = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("WebSocket server already started")

        ssl_context = self._resolve_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        self._server = await websockets.serve(
            ws_handler=self._handle_client,
            host=self._settings.websocket_host,
            port=self._settings.websocket_port,
            create_protocol=None,
            ping_interval=self._settings.websocket_heartbeat_seconds,
            max_size=self._settings.websocket_max_size,
            process_request=self._process_request,
            ssl=ssl_context,
        )
        if ssl_context is None:
            if self._settings.websocket_tls_enabled:
                raise RuntimeError(
                    "TLS is enabled but the SSL context could not be created. Check certificate configuration."
                )
            logger.info(
                "Gateway WebSocket server is serving plain ws:// connections (TLS disabled)."
            )
        logger.info(
            "Gateway WebSocket server listening on %s://%s:%s%s",
            scheme,
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
                    {
                        "type": "hello_ack",
                        "status": "ok",
                        "session_id": client.session.session_id,
                    }
                )
            )
            await self._connections.replay_pending(client, full=True)

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
            elif event_type == "ack":
                payload = message.get("payload") or {}
                event_ids = payload.get("event_ids")
                if isinstance(event_ids, str):
                    event_ids = [event_ids]
                if not event_ids:
                    single = payload.get("event_id")
                    if single:
                        event_ids = [single]
                if event_ids:
                    await self._connections.process_ack(client.session.discord_user_id, event_ids)
            elif event_type == "state_sync_request":
                await self._connections.handle_state_sync_request(client)
                await websocket.send(
                    json.dumps(
                        {
                            "type": "state_sync_ack",
                            "payload": {
                                "pending_events": await self._connections.pending_count(
                                    client.session.discord_user_id
                                )
                            },
                        }
                    )
                )
            else:
                try:
                    handled = await self._command_processor.handle(client, message)
                except Exception:
                    logger.exception(
                        "Unhandled exception while processing client command: %s",
                        message,
                    )
                    continue
                if not handled:
                    logger.debug(
                        "Ignoring unhandled client message type=%s payload=%s",
                        event_type,
                        message,
                    )

    def _resolve_ssl_context(self) -> ssl.SSLContext | None:
        if self._external_ssl_context is not None:
            return self._external_ssl_context
        if not self._settings.websocket_tls_enabled:
            return None
        return self._build_ssl_context()

    def _build_ssl_context(self) -> ssl.SSLContext:
        certfile = self._settings.websocket_tls_certfile
        keyfile = self._settings.websocket_tls_keyfile
        if not certfile or not keyfile:
            raise RuntimeError("TLS is enabled but certificate/key paths are not configured.")

        cert_path = Path(certfile).expanduser()
        key_path = Path(keyfile).expanduser()
        if not cert_path.exists() or not key_path.exists():
            raise RuntimeError(
                f"TLS certificate ({cert_path}) or key ({key_path}) could not be found."
            )

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

        verify_mode = {
            "none": ssl.CERT_NONE,
            "optional": ssl.CERT_OPTIONAL,
            "required": ssl.CERT_REQUIRED,
        }[self._settings.websocket_tls_client_auth]

        if verify_mode != ssl.CERT_NONE:
            ca_file = self._settings.websocket_tls_ca_file
            if not ca_file:
                raise RuntimeError(
                    "Client authentication requires SAIVERSE_WS_TLS_CA_FILE to be set."
                )
            ca_path = Path(ca_file).expanduser()
            if not ca_path.exists():
                raise RuntimeError(f"TLS CA file {ca_path} could not be found.")
            context.load_verify_locations(cafile=str(ca_path))
        context.verify_mode = verify_mode

        return context
