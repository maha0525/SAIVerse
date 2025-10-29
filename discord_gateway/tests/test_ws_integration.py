import asyncio
import json
from datetime import timedelta

import pytest
import websockets
from websockets.legacy import server as legacy_server

from discord_gateway.bot.connection_manager import ConnectionManager
from discord_gateway.bot.database import BotDatabase, utcnow
from discord_gateway.bot.ws_server import GatewayWebSocketServer


@pytest.mark.asyncio
async def test_ws_server_handles_backlog_and_resync(make_settings, tmp_path, monkeypatch):
    settings = make_settings(
        websocket_host="127.0.0.1",
        websocket_port=0,
        pending_replay_limit=2,
        replay_batch_size=10,
    )
    db = BotDatabase(f"sqlite:///{tmp_path/'bot.db'}")
    db.migrate()

    token = "integration-token"
    db.create_session_token(
        discord_user_id="user-1",
        raw_token=token,
        label="integration",
        expires_at=utcnow() + timedelta(hours=1),
    )

    manager = ConnectionManager(settings, db)
    server = GatewayWebSocketServer(settings, manager)
    monkeypatch.setattr(
        "discord_gateway.bot.ws_server.websockets.serve", legacy_server.serve
    )
    await server.start()

    assert server._server is not None  # pragma: no cover - defensive
    port = server._server.sockets[0].getsockname()[1]
    uri = f"ws://{settings.websocket_host}:{port}{settings.websocket_path}"

    # queue events while offline to create backlog
    for idx in range(3):
        await manager.send_to_owner(
            "user-1",
            {
                "type": "discord_message",
                "payload": {
                    "channel_id": "lobby",
                    "content": f"queued-{idx}",
                },
            },
        )
    assert await manager.pending_count("user-1") == 3

    async with websockets.connect(uri, max_size=settings.websocket_max_size) as ws:
        await ws.send(json.dumps({"type": "hello", "token": token}))
        hello_ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert hello_ack["type"] == "hello_ack"

        initial_messages = [
            json.loads(await asyncio.wait_for(ws.recv(), timeout=3)) for _ in range(3)
        ]
        assert {msg["payload"]["content"] for msg in initial_messages} == {
            "queued-0",
            "queued-1",
            "queued-2",
        }

        # add an additional event while backlog still exists to trigger resync warning
        await manager.send_to_owner(
            "user-1",
            {
                "type": "discord_message",
                "payload": {
                    "channel_id": "lobby",
                    "content": "live-event",
                },
            },
        )

        live_event = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        resync_notice = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert live_event["payload"]["content"] == "live-event"
        assert resync_notice["type"] == "resync_required"

        # acknowledge received events
        for message in initial_messages + [live_event, resync_notice]:
            await ws.send(
                json.dumps(
                    {
                        "type": "ack",
                        "payload": {"event_id": message["payload"]["event_id"]},
                    }
                )
            )

        await ws.send(json.dumps({"type": "state_sync_request", "payload": {"mode": "full"}}))
        sync_ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert sync_ack["type"] == "state_sync_ack"
        assert sync_ack["payload"]["pending_events"] >= 0

    await server.stop()
