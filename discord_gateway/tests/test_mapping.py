import os

import pytest

from discord_gateway.mapping import ChannelContext, ChannelMapping


def test_channel_mapping_from_iterable():
    mapping = ChannelMapping.from_iterable(
        [
            {
                "channel_id": "123",
                "city_id": "city_a",
                "building_id": "main_hall",
                "host_user_id": "host-1",
            }
        ]
    )
    context = mapping.get("123")
    assert isinstance(context, ChannelContext)
    assert context.city_id == "city_a"


def test_channel_mapping_from_env(monkeypatch):
    data = [
        {
            "channel_id": "A",
            "city_id": "CityA",
            "building_id": "Building1",
            "host_user_id": "user-1",
        }
    ]
    monkeypatch.setenv(
        "SAIVERSE_GATEWAY_CHANNEL_MAP",
        os.environ.get("TEST_JSON", str(data).replace("'", '"')),
    )
    mapping = ChannelMapping.from_environment()
    assert mapping.get("A").building_id == "Building1"
    assert mapping.find_by_location("CityA", "Building1").channel_id == "A"


def test_channel_mapping_invalid_json():
    with pytest.raises(ValueError):
        ChannelMapping.from_json("not json")
