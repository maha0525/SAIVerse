from discord_gateway.orchestrator import (
    MemorySyncCompletionResult,
    MemorySyncHandshakeResult,
)
from discord_gateway.mapping import ChannelContext
from discord_gateway.saiverse_adapter import (
    DiscordMessage,
    GatewayHost,
    SAIVerseGatewayAdapter,
)
from discord_gateway.translator import GatewayCommand, GatewayEvent
from discord_gateway.visitors import VisitorProfile


class DummyManager:
    def __init__(self):
        self.calls = []

    def gateway_on_visitor_registered(self, visitor, context):
        self.calls.append(("register", visitor.persona_id, context.channel_id))

    def gateway_on_visitor_departed(self, visitor):
        self.calls.append(("depart", visitor.persona_id))

    def gateway_handle_human_message(self, message: DiscordMessage):
        self.calls.append(("human", message.content))
        return [GatewayCommand(type="reply", payload={"message": "ok"})]

    def gateway_handle_remote_persona_message(self, message: DiscordMessage):
        self.calls.append(("remote", message.visitor.persona_id))
        return None

    def gateway_handle_memory_sync_initiate(self, visitor, payload):
        self.calls.append(("init", payload.get("transfer_id")))
        return MemorySyncHandshakeResult(accepted=True)

    def gateway_handle_memory_sync_chunk(self, visitor, payload):
        self.calls.append(("chunk", payload.get("seq")))
        return None

    def gateway_handle_memory_sync_complete(self, visitor, payload):
        self.calls.append(("complete", payload.get("transfer_id")))
        return MemorySyncCompletionResult(success=True)


async def test_adapter_dispatch(monkeypatch):
    manager = DummyManager()
    host = GatewayHost(manager)
    adapter = SAIVerseGatewayAdapter(host)

    visitor = VisitorProfile(
        discord_user_id="999",
        persona_id="persona-1",
        owner_user_id="owner-1",
        current_city_id="CityA",
        current_building_id="Hall",
    )
    context = ChannelContext(
        channel_id="123", city_id="CityA", building_id="Hall", host_user_id="host-1"
    )

    await adapter.on_visitor_registered(visitor, context)
    command_list = await adapter.handle_human_message(
        context,
        GatewayEvent(type="discord_message", payload={"content": "hi"}, raw={}),
    )
    assert command_list and command_list[0].type == "reply"

    await adapter.handle_remote_persona_message(
        context,
        GatewayEvent(
            type="discord_message",
            payload={"content": "visit", "author": {"discord_user_id": "999"}},
            raw={},
        ),
        visitor,
    )

    await adapter.handle_memory_sync_initiate(visitor, {"transfer_id": "abc"})
    await adapter.handle_memory_sync_chunk(visitor, {"transfer_id": "abc", "seq": 1})
    await adapter.handle_memory_sync_complete(visitor, {"transfer_id": "abc"})
    await adapter.on_visitor_departed(visitor)

    assert ("register", "persona-1", "123") in manager.calls
    assert ("human", "hi") in manager.calls
    assert ("remote", "persona-1") in manager.calls
    assert ("init", "abc") in manager.calls
    assert ("chunk", 1) in manager.calls
    assert ("complete", "abc") in manager.calls
    assert ("depart", "persona-1") in manager.calls
