import asyncio

import pytest

from discord_gateway.auth import StaticTokenProvider
from discord_gateway.config import GatewaySettings
from discord_gateway.gateway_service import DiscordGatewayService
from discord_gateway.translator import GatewayCommand


class FakeGatewayClient:
    def __init__(self, settings: GatewaySettings, inbound: asyncio.Queue, outbound: asyncio.Queue):
        self.settings = settings
        self._inbound = inbound
        self._outbound = outbound

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def handshake(self, token: str):
        assert token == "test-token"
        assert self.settings.bot_ws_url.endswith("/ws")
        return {"type": "hello_ack", "status": "ok"}

    async def recv_json(self):
        return await self._inbound.get()

    async def send_json(self, payload: dict[str, object]):
        await self._outbound.put(payload)


@pytest.mark.asyncio
async def test_gateway_service_message_flow():
    inbound_messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    outbound_messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    await inbound_messages.put(
        {"type": "discord_event", "payload": {"content": "hello"}}
    )

    settings = GatewaySettings(
        bot_ws_url="ws://test.local/ws",
        handshake_token="test-token",
        reconnect_initial_delay=0.1,
        reconnect_max_delay=0.2,
        incoming_queue_maxsize=10,
        outgoing_queue_maxsize=10,
    )

    service = DiscordGatewayService(
        settings,
        client_factory=lambda s: FakeGatewayClient(s, inbound_messages, outbound_messages),
        token_provider=StaticTokenProvider(token="test-token"),
    )
    await service.start()

    event = await asyncio.wait_for(service.incoming_queue.get(), timeout=5)
    assert event.type == "discord_event"
    assert event.payload["content"] == "hello"

    await service.outgoing_queue.put(GatewayCommand(type="send_message", payload={"text": "hi"}))
    sent_payload = await asyncio.wait_for(outbound_messages.get(), timeout=5)
    assert sent_payload == {
        "type": "send_message",
        "payload": {"text": "hi"},
    }

    await service.stop()
