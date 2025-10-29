import types

import pytest

from discord_gateway.bot.command_processor import CommandProcessor


class StubRouter:
    def __init__(self, owner_map):
        self._owner_map = {str(k): v for k, v in owner_map.items()}
        self.sent = []

    def get_owner_id(self, channel_id):
        return self._owner_map.get(str(channel_id))

    async def send_post_message(
        self,
        channel_id,
        *,
        content,
        persona_id=None,
        metadata=None,
    ):
        self.sent.append(
            {
                "channel_id": str(channel_id),
                "content": content,
                "persona_id": persona_id,
                "metadata": metadata or {},
            }
        )


def make_client(owner_id: str):
    session = types.SimpleNamespace(discord_user_id=owner_id)
    return types.SimpleNamespace(session=session)


@pytest.mark.asyncio
async def test_post_message_dispatch_success():
    router = StubRouter({"123": "owner-1"})
    processor = CommandProcessor(router=router, max_message_length=11)
    client = make_client("owner-1")

    handled = await processor.handle(
        client,
        {
            "type": "post_message",
            "payload": {
                "channel_id": "123",
                "content": "hello world!",
                "persona_id": "persona-9",
                "building_id": "bld-1",
                "city_id": "CityA",
            },
        },
    )

    assert handled is True
    assert router.sent == [
        {
            "channel_id": "123",
            "content": "hello world",
            "persona_id": "persona-9",
            "metadata": {"building_id": "bld-1", "city_id": "CityA"},
        }
    ]


@pytest.mark.asyncio
async def test_post_message_rejects_owner_mismatch():
    router = StubRouter({"123": "owner-expected"})
    processor = CommandProcessor(router=router, max_message_length=50)
    client = make_client("other-owner")

    handled = await processor.handle(
        client,
        {
            "type": "post_message",
            "payload": {"channel_id": "123", "content": "hi"},
        },
    )

    assert handled is True
    assert router.sent == []


@pytest.mark.asyncio
async def test_unknown_command_returns_false():
    router = StubRouter({})
    processor = CommandProcessor(router=router, max_message_length=50)
    client = make_client("owner")

    handled = await processor.handle(client, {"type": "noop"})

    assert handled is False
