import json
from datetime import timedelta

import pytest

from discord_gateway.bot.connection_manager import ConnectionManager
from discord_gateway.bot.database import (
    BotDatabase,
    LocalAppSession,
    hash_token,
    utcnow,
)


class DummyWebSocket:
    def __init__(self):
        self._sent = []
        self.closed = False
        self.close_args = None

    async def send(self, payload: str) -> None:
        self._sent.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.closed = True
        self.close_args = (code, reason)

    @property
    def sent(self):
        return list(self._sent)


@pytest.fixture()
def bot_settings(tmp_path, make_settings):
    db_path = tmp_path / "bot.db"
    return make_settings(
        database_url=f"sqlite:///{db_path}",
        websocket_host="127.0.0.1",
        websocket_port=0,
    )


@pytest.fixture()
def bot_database(bot_settings):
    db = BotDatabase(bot_settings.database_url)
    db.migrate()
    return db


@pytest.fixture()
def connection_manager(bot_settings, bot_database):
    return ConnectionManager(bot_settings, bot_database)


@pytest.mark.asyncio
async def test_authenticate_registers_connection(connection_manager, bot_database):
    token = "secret"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                label="workstation",
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    websocket = DummyWebSocket()
    client = await connection_manager.authenticate(token, websocket)

    assert client is not None
    assert client.session.discord_user_id == "user-1"


@pytest.mark.asyncio
async def test_second_connection_replaces_first(connection_manager, bot_database):
    token = "secret"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    first_ws = DummyWebSocket()
    await connection_manager.authenticate(token, first_ws)

    second_ws = DummyWebSocket()
    new_client = await connection_manager.authenticate(token, second_ws)

    assert new_client is not None
    assert first_ws.close_args == (4001, "Replaced by new session")


@pytest.mark.asyncio
async def test_authenticate_rejects_expired_token(connection_manager, bot_database):
    token = "expired"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                expires_at=utcnow() - timedelta(minutes=1),
            )
        )

    websocket = DummyWebSocket()
    client = await connection_manager.authenticate(token, websocket)
    assert client is None


@pytest.mark.asyncio
async def test_authenticate_rejects_revoked_token(connection_manager, bot_database):
    token = "revoked"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                expires_at=utcnow() + timedelta(hours=1),
                revoked_at=utcnow(),
            )
        )

    websocket = DummyWebSocket()
    client = await connection_manager.authenticate(token, websocket)
    assert client is None


@pytest.mark.asyncio
async def test_send_to_owner_dispatches_payload(connection_manager, bot_database):
    token = "secret"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    websocket = DummyWebSocket()
    client = await connection_manager.authenticate(token, websocket)
    assert client is not None

    dispatched = await connection_manager.send_to_owner(
        "user-1", {"type": "ping", "payload": {"channel_id": "room-1"}}
    )

    assert dispatched is True
    assert websocket.sent
    message = json.loads(websocket.sent[-1])
    assert message["type"] == "ping"
    assert "event_id" in message["payload"]

    await connection_manager.process_ack("user-1", [message["payload"]["event_id"]])
    assert await connection_manager.pending_count("user-1") == 0


@pytest.mark.asyncio
async def test_send_to_owner_without_connection(connection_manager):
    dispatched = await connection_manager.send_to_owner("missing", {"type": "ping"})
    assert dispatched is False


@pytest.mark.asyncio
async def test_pending_replayed_after_authentication(connection_manager, bot_database):
    token = "secret"
    digest = hash_token(token)
    with bot_database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user-1",
                token_hash=digest,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )

    dispatched = await connection_manager.send_to_owner(
        "user-1", {"type": "ping", "payload": {"channel_id": "room-1"}}
    )
    assert dispatched is False
    assert await connection_manager.pending_count("user-1") == 1

    websocket = DummyWebSocket()
    client = await connection_manager.authenticate(token, websocket)
    assert client is not None

    await connection_manager.replay_pending(client, full=True)
    assert websocket.sent
    message = json.loads(websocket.sent[-1])
    assert message["type"] == "ping"
