from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Iterable
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

        self._pending_events: defaultdict[str, OrderedDict[str, dict]] = defaultdict(OrderedDict)
        self._channel_sequences: defaultdict[str, int] = defaultdict(int)
        self._resync_needed: set[str] = set()
        self._resync_sent: set[str] = set()

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
        event, client = await self._enqueue_event(owner_discord_id, payload)
        if not client:
            logger.warning(
                "No active connection for owner_discord_id=%s. Queued for later dispatch.",
                owner_discord_id,
            )
            return False

        try:
            await client.send_json(event)
            await self._maybe_emit_resync(owner_discord_id, client)
            return True
        except Exception:
            logger.exception("Failed to dispatch payload to owner_discord_id=%s", owner_discord_id)
            return False

    async def _enqueue_event(
        self, owner_discord_id: str, payload: dict
    ) -> tuple[dict, ConnectedClient | None]:
        event = copy.deepcopy(payload)
        event_payload = event.setdefault("payload", {})
        event_id = str(uuid.uuid4())
        event_payload.setdefault("event_id", event_id)

        channel_id = event_payload.get("channel_id")
        channel_id_str = str(channel_id) if channel_id is not None else None
        if channel_id_str:
            event_payload["channel_id"] = channel_id_str
            sequence = self._next_channel_sequence(channel_id_str)
            event_payload.setdefault("channel_seq", sequence)

        async with self._lock:
            pending = self._pending_events[owner_discord_id]
            pending[event_id] = event
            pending_length = len(pending)
            if pending_length > max(1, self._settings.pending_replay_limit):
                self._resync_needed.add(owner_discord_id)
            client = self._connections.get(owner_discord_id)

        return event, client

    def _next_channel_sequence(self, channel_id: str) -> int:
        self._channel_sequences[channel_id] += 1
        return self._channel_sequences[channel_id]

    async def _maybe_emit_resync(self, owner_discord_id: str, client: ConnectedClient) -> None:
        async with self._lock:
            if owner_discord_id not in self._resync_needed or owner_discord_id in self._resync_sent:
                return
            event_id = str(uuid.uuid4())
            event = {
                "type": "resync_required",
                "payload": {
                    "event_id": event_id,
                    "reason": "pending_backlog",
                },
            }
            pending = self._pending_events[owner_discord_id]
            pending[event_id] = event
            self._resync_sent.add(owner_discord_id)

        try:
            await client.send_json(event)
        except Exception:
            logger.exception(
                "Failed to notify owner_discord_id=%s about resync requirement.",
                owner_discord_id,
            )

    async def process_ack(self, owner_discord_id: str, event_ids: Iterable[str]) -> None:
        event_ids = list(event_ids or [])
        if not event_ids:
            return

        async with self._lock:
            pending = self._pending_events.get(owner_discord_id)
            if not pending:
                return

            for event_id in event_ids:
                event = pending.pop(str(event_id), None)
                if not event:
                    continue
                if event.get("type") == "resync_required":
                    # keep resync flags until client explicitly requests sync
                    pass

            if not pending:
                self._resync_needed.discard(owner_discord_id)
                self._resync_sent.discard(owner_discord_id)
            elif len(pending) <= self._settings.pending_replay_limit:
                self._resync_needed.discard(owner_discord_id)
                self._resync_sent.discard(owner_discord_id)

    async def replay_pending(self, client: ConnectedClient, *, full: bool = False) -> int:
        owner_id = client.session.discord_user_id
        async with self._lock:
            events = list(self._pending_events.get(owner_id, {}).values())
        if not events:
            return 0

        batch_size = None if full else self._settings.replay_batch_size
        if batch_size is not None and batch_size > 0:
            events = events[:batch_size]

        sent = 0
        for event in events:
            try:
                await client.send_json(event)
                sent += 1
            except Exception:
                logger.exception(
                    "Failed during replay dispatch to owner_discord_id=%s",
                    owner_id,
                )
                break
        return sent

    async def handle_state_sync_request(self, client: ConnectedClient) -> None:
        owner_id = client.session.discord_user_id
        async with self._lock:
            self._resync_needed.discard(owner_id)
            self._resync_sent.discard(owner_id)
        await self.replay_pending(client, full=True)

    async def heartbeat(self, client: ConnectedClient) -> None:
        client.last_heartbeat = datetime.utcnow()

    async def get_connection(self, owner_discord_id: str) -> ConnectedClient | None:
        async with self._lock:
            return self._connections.get(owner_discord_id)

    async def active_connections(self) -> dict[str, ConnectedClient]:
        async with self._lock:
            return dict(self._connections)

    async def pending_count(self, owner_discord_id: str) -> int:
        async with self._lock:
            return len(self._pending_events.get(owner_discord_id, {}))
