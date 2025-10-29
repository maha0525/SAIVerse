import asyncio
import json

import pytest
import websockets

from discord_gateway.config import GatewaySettings
from discord_gateway.gateway_service import DiscordGatewayService
from discord_gateway.translator import GatewayCommand


@pytest.mark.asyncio
async def test_gateway_service_message_flow():
    received_commands: asyncio.Queue[str] = asyncio.Queue()
    event_sent = asyncio.Event()

    async def handler(websocket):
        raw = await websocket.recv()
        hello = json.loads(raw)
        assert hello["type"] == "hello"
        assert hello["token"] == "test-token"
        await websocket.send(json.dumps({"type": "hello_ack", "status": "ok"}))
        await websocket.send(json.dumps({"type": "discord_event", "payload": {"content": "hello"}}))
        event_sent.set()
        command_raw = await websocket.recv()
        await received_commands.put(command_raw)
        await asyncio.sleep(0.05)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    settings = GatewaySettings(
        bot_ws_url=f"ws://127.0.0.1:{port}/ws",
        handshake_token="test-token",
        reconnect_initial_delay=0.1,
        reconnect_max_delay=0.2,
        incoming_queue_maxsize=10,
        outgoing_queue_maxsize=10,
    )

    service = DiscordGatewayService(settings)
    await service.start()

    await asyncio.wait_for(event_sent.wait(), timeout=5)
    event = await asyncio.wait_for(service.incoming_queue.get(), timeout=5)
    assert event.type == "discord_event"
    assert event.payload["content"] == "hello"

    await service.outgoing_queue.put(GatewayCommand(type="send_message", payload={"text": "hi"}))
    sent_payload = await asyncio.wait_for(received_commands.get(), timeout=5)
    assert json.loads(sent_payload) == {
        "type": "send_message",
        "payload": {"text": "hi"},
    }

    await service.stop()
    server.close()
    await server.wait_closed()
