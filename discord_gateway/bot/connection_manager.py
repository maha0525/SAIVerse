from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from websockets.server import WebSocketServerProtocol

from .config import BotSettings
from .database import AuthenticatedSession, BotDatabase

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectedClient:
    """Represents an active WebSocket connection to a local application."""

    websocket: WebSocketServerProtocol
    session: AuthenticatedSession
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)

    async def send_json(self, payload: dict) -> None:
        await self.websocket.send(json.dumps(payload))


class ConnectionManager:
    """Tracks and manages `ローカルアプリケーション` WebSocket connections."""

    def __init__(self, settings: BotSettings, database: BotDatabase):
        self._settings = settings
        self._database = database
        self._connections: dict[str, ConnectedClient] = {}
        self._lock = asyncio.Lock()

    async def authenticate(
        self, token: str, websocket: WebSocketServerProtocol
    ) -> ConnectedClient | None:
        session = self._database.authenticate_token(token)
        if not session:
            return None

        client = ConnectedClient(websocket=websocket, session=session)
        async with self._lock:
            previous = self._connections.get(session.discord_user_id)
            if previous:
                await previous.websocket.close(code=4001, reason="Replaced by new session")
            self._connections[session.discord_user_id] = client
        logger.info(
            "Registered local app connection user_id=%s session_id=%s",
            session.discord_user_id,
            session.session_id,
        )
        return client

    async def unregister(self, client: ConnectedClient) -> None:
        async with self._lock:
            current = self._connections.get(client.session.discord_user_id)
            if current is client:
                self._connections.pop(client.session.discord_user_id, None)
        logger.info(
            "Unregistered local app connection user_id=%s session_id=%s",
            client.session.discord_user_id,
            client.session.session_id,
        )

    async def send_to_owner(self, owner_discord_id: str, payload: dict) -> bool:
        async with self._lock:
            client = self._connections.get(owner_discord_id)
        if not client:
            logger.warning(
                "No active connection for owner_discord_id=%s. Dropping payload.",
                owner_discord_id,
            )
            return False

        try:
            await client.send_json(payload)
            return True
        except Exception:
            logger.exception("Failed to dispatch payload to owner_discord_id=%s", owner_discord_id)
            return False

    async def heartbeat(self, client: ConnectedClient) -> None:
        client.last_heartbeat = datetime.utcnow()

    async def get_connection(self, owner_discord_id: str) -> ConnectedClient | None:
        async with self._lock:
            return self._connections.get(owner_discord_id)

    async def active_connections(self) -> dict[str, ConnectedClient]:
        async with self._lock:
            return dict(self._connections)
