import asyncio

import pytest

from discord_gateway.mapping import ChannelMapping
from discord_gateway.orchestrator import (
    DiscordGatewayOrchestrator,
    GatewayHostAdapter,
    MemorySyncCompletionResult,
    MemorySyncHandshakeResult,
)
from discord_gateway.translator import GatewayCommand, GatewayEvent
from discord_gateway.visitors import VisitorProfile, VisitorRegistry


class DummyService:
    def __init__(self):
        self.incoming_queue: asyncio.Queue[GatewayEvent] = asyncio.Queue()
        self.outgoing_queue: asyncio.Queue[GatewayCommand] = asyncio.Queue()

    async def start(self):
        return None

    async def stop(self):
        return None


class RecordingAdapter(GatewayHostAdapter):
    def __init__(self):
        self.human_messages: list[str] = []
        self.visitor_messages: list[str] = []
        self.chunk_events: list[int] = []
        self.complete_events: list[str] = []
        self.resync_payloads: list[dict] = []
        self.handshake_transfers: list[str] = []

    async def on_visitor_registered(self, visitor, context):
        return None

    async def on_visitor_departed(self, visitor):
        return None

    async def handle_human_message(self, context, event):
        self.human_messages.append(event.payload["content"])
        return [GatewayCommand(type="ack", payload={"channel_id": context.channel_id})]

    async def handle_remote_persona_message(self, context, event, visitor):
        self.visitor_messages.append(visitor.persona_id)
        return None

    async def handle_memory_sync_initiate(self, visitor, payload):
        transfer_id = payload.get("transfer_id")
        if transfer_id:
            self.handshake_transfers.append(transfer_id)
        return MemorySyncHandshakeResult(accepted=True)

    async def handle_memory_sync_chunk(self, visitor, payload):
        self.chunk_events.append(payload.get("index"))
        return None

    async def handle_memory_sync_complete(self, visitor, payload):
        self.complete_events.append(payload.get("transfer_id"))
        return MemorySyncCompletionResult(success=True)

    async def handle_resync_required(self, payload):
        self.resync_payloads.append(payload)
        return None


@pytest.mark.asyncio
async def test_orchestrator_routes_messages():
    service = DummyService()
    mapping = ChannelMapping.from_iterable(
        [
            {
                "channel_id": "123",
                "city_id": "CityA",
                "building_id": "Hall",
                "host_user_id": "host-1",
            }
        ]
    )
    visitors = VisitorRegistry(
        [
            VisitorProfile(
                discord_user_id="999",
                persona_id="persona-1",
                owner_user_id="owner-1",
                current_city_id="CityA",
                current_building_id="Hall",
            )
        ]
    )
    adapter = RecordingAdapter()
    orchestrator = DiscordGatewayOrchestrator(
        service, mapping=mapping, visitors=visitors, host_adapter=adapter
    )

    await orchestrator.start()
    await service.incoming_queue.put(
        GatewayEvent(
            type="discord_message",
            payload={
                "channel_id": "123",
                "content": "hello",
                "author": {"discord_user_id": "host-1", "roles": {"ids": [], "names": []}},
                "event_id": "evt-1",
            },
            raw={"type": "discord_message", "payload": {"channel_id": "123"}},
        )
    )
    await service.incoming_queue.put(
        GatewayEvent(
            type="discord_message",
            payload={
                "channel_id": "123",
                "content": "visitor",
                "author": {
                    "discord_user_id": "999",
                    "roles": {"ids": [], "names": []},
                },
                "event_id": "evt-2",
            },
            raw={"type": "discord_message", "payload": {"channel_id": "123"}},
        )
    )
    await asyncio.sleep(0.1)
    await orchestrator.stop()

    assert adapter.human_messages == ["hello"]
    assert adapter.visitor_messages == ["persona-1"]
    commands = [await service.outgoing_queue.get() for _ in range(3)]
    event_acks = [cmd for cmd in commands if cmd.type == "ack" and "event_id" in cmd.payload]
    assert any(cmd.payload["event_id"] == "evt-1" for cmd in event_acks)
    assert any(cmd.payload["event_id"] == "evt-2" for cmd in event_acks)


@pytest.mark.asyncio
async def test_orchestrator_requests_state_sync_on_resync():
    service = DummyService()
    mapping = ChannelMapping([])
    adapter = RecordingAdapter()
    orchestrator = DiscordGatewayOrchestrator(service, mapping=mapping, host_adapter=adapter)
    await orchestrator.start()
    await service.incoming_queue.put(
        GatewayEvent(
            type="resync_required",
            payload={"event_id": "evt-resync", "reason": "pending_backlog"},
            raw={"type": "resync_required"},
        )
    )
    await asyncio.sleep(0.05)
    await orchestrator.stop()

    commands = []
    while not service.outgoing_queue.empty():
        commands.append(await service.outgoing_queue.get())

    assert any(cmd.type == "state_sync_request" for cmd in commands)
    assert any(
        cmd.type == "ack" and cmd.payload.get("event_id") == "evt-resync" for cmd in commands
    )


@pytest.mark.asyncio
async def test_memory_sync_large_transfer():
    service = DummyService()
    mapping = ChannelMapping.from_iterable(
        [
            {
                "channel_id": "555",
                "city_id": "CityA",
                "building_id": "Lab",
                "host_user_id": "host-9",
            }
        ]
    )
    visitors = VisitorRegistry()
    visitor = VisitorProfile(
        discord_user_id="host-9",
        persona_id="persona-lab",
        owner_user_id="host-9",
        current_city_id="CityA",
        current_building_id="Lab",
    )
    visitors.register(visitor)

    adapter = RecordingAdapter()
    orchestrator = DiscordGatewayOrchestrator(
        service, mapping=mapping, visitors=visitors, host_adapter=adapter
    )

    await orchestrator.start()
    chunk_count = 50
    for index in range(chunk_count):
        await service.incoming_queue.put(
            GatewayEvent(
                type="memory_sync_chunk",
                payload={
                    "event_id": f"chunk-{index}",
                    "visitor": {
                        "discord_user_id": visitor.discord_user_id,
                        "persona_id": visitor.persona_id,
                        "owner_user_id": visitor.owner_user_id,
                    },
                    "index": index,
                    "total": chunk_count,
                    "bytes": "x" * 65536,
                },
            )
        )

    await service.incoming_queue.put(
        GatewayEvent(
            type="memory_sync_complete",
            payload={
                "event_id": "complete-1",
                "visitor": {
                    "discord_user_id": visitor.discord_user_id,
                    "persona_id": visitor.persona_id,
                    "owner_user_id": visitor.owner_user_id,
                },
                "transfer_id": "transfer-123",
            },
        )
    )

    await asyncio.sleep(0.1)
    await orchestrator.stop()

    assert adapter.chunk_events == list(range(chunk_count))
    assert adapter.complete_events == ["transfer-123"]

    ack_ids = []
    while not service.outgoing_queue.empty():
        command = await service.outgoing_queue.get()
        if command.type == "ack":
            ack_ids.append(command.payload.get("event_id"))

    assert len([aid for aid in ack_ids if str(aid).startswith("chunk-")]) == chunk_count
    assert "complete-1" in ack_ids


@pytest.mark.asyncio
async def test_memory_sync_duplicate_chunk_ack_without_duplicate_processing():
    service = DummyService()
    mapping = ChannelMapping([])
    visitors = VisitorRegistry()
    visitor = VisitorProfile(
        discord_user_id="user-x",
        persona_id="persona-x",
        owner_user_id="user-x",
        current_city_id="CityX",
        current_building_id="Room",
    )
    visitors.register(visitor)

    adapter = RecordingAdapter()
    orchestrator = DiscordGatewayOrchestrator(
        service, mapping=mapping, visitors=visitors, host_adapter=adapter
    )

    await orchestrator.start()
    duplicate_event = GatewayEvent(
        type="memory_sync_chunk",
        payload={
            "event_id": "chunk-dup",
            "visitor": {
                "discord_user_id": visitor.discord_user_id,
                "persona_id": visitor.persona_id,
                "owner_user_id": visitor.owner_user_id,
            },
            "index": 5,
            "total": 20,
            "bytes": "y" * 1024,
        },
    )

    await service.incoming_queue.put(duplicate_event)
    await service.incoming_queue.put(duplicate_event)
    await asyncio.sleep(0.05)
    await orchestrator.stop()

    assert adapter.chunk_events == [5]

    ack_ids = []
    while not service.outgoing_queue.empty():
        command = await service.outgoing_queue.get()
        if command.type == "ack":
            ack_ids.append(command.payload.get("event_id"))

    assert ack_ids.count("chunk-dup") == 2


@pytest.mark.asyncio
async def test_memory_sync_initiate_ack():
    service = DummyService()
    mapping = ChannelMapping([])
    visitors = VisitorRegistry(
        [
            VisitorProfile(
                discord_user_id="user-init",
                persona_id="persona-init",
                owner_user_id="user-init",
                current_city_id="CityI",
                current_building_id="RoomI",
            )
        ]
    )

    adapter = RecordingAdapter()
    orchestrator = DiscordGatewayOrchestrator(
        service, mapping=mapping, visitors=visitors, host_adapter=adapter
    )

    await orchestrator.start()
    await service.incoming_queue.put(
        GatewayEvent(
            type="memory_sync_initiate",
            payload={
                "event_id": "init-evt",
                "transfer_id": "transfer-init",
                "visitor": {
                    "discord_user_id": "user-init",
                    "persona_id": "persona-init",
                    "owner_user_id": "user-init",
                },
                "total_size": 0,
                "total_chunks": 0,
                "checksum": "noop",
            },
        )
    )
    await asyncio.sleep(0.05)
    await orchestrator.stop()

    assert "transfer-init" in adapter.handshake_transfers

    ack_commands = []
    while not service.outgoing_queue.empty():
        ack_commands.append(await service.outgoing_queue.get())

    assert any(
        cmd.type == "ack" and cmd.payload.get("event_id") == "init-evt" for cmd in ack_commands
    )
    handshake_ack = next(cmd for cmd in ack_commands if cmd.type == "memory_sync_ack")
    assert handshake_ack.payload["transfer_id"] == "transfer-init"
    assert handshake_ack.payload["status"] == "ok"
