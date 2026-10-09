"""Movement visibility adds UI hints without changing stored/delivered history."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.deps import get_manager
from api.routes import chat, config, world
from database.building_messages import fetch_building_messages, insert_building_message
from database.models import Base, Building, City, User, UserSettings
from saiverse.occupancy_manager import OccupancyManager


def _manager():
    return SimpleNamespace(
        user_display_name="Reader", personas={},
        state=SimpleNamespace(user_avatar_data=None),
    )


@pytest.mark.parametrize("role", ["host", "system", "user", "assistant"])
@pytest.mark.parametrize("event,expected", [
    ({"type": "occupancy", "action": "enter"}, True),
    ({"type": "occupancy", "action": "leave"}, True),
    ({"type": "occupancy", "action": "capacity_warning"}, False),
    ({"type": "item", "action": "enter"}, False),
    ({"type": "warning", "action": "leave"}, False),
    ({"type": "occupancy"}, False),
    (None, False),
    ("occupancy", False),
])
def test_only_structured_host_movement_is_classified(role, event, expected):
    message = {
        "role": role,
        "content": '<div class="note-box">🚶 User Action:<br><b>AがBから入室しました</b></div>',
        "metadata": {"event": event},
    }
    before = deepcopy(message)
    serialized = chat.serialize_history_message(_manager(), message, "legacy:1", "room")
    assert serialized.is_movement_notice is (expected and role in ("host", "system"))
    assert serialized.building_id == "room"
    assert serialized.content == message["content"]
    assert message == before


def test_unstructured_legacy_notice_stays_visible_and_non_japanese_event_is_classified():
    """Do not hide conversation by matching wording, nor depend on notice language."""
    legacy = {"role": "host", "content": '<div class="note-box">User Action: AがBへ移動しました</div>'}
    assert not chat.serialize_history_message(_manager(), legacy, "legacy").is_movement_notice
    structured = {
        "role": "host", "content": "A entered the room.",
        "metadata": {"event": {"type": "occupancy", "action": "enter"}},
        "building_id": "original-room",
    }
    serialized = chat.serialize_history_message(_manager(), structured, "original-room:1")
    assert serialized.is_movement_notice
    assert serialized.building_id == "original-room"


@pytest.fixture
def history_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr("database.session.SessionLocal", sessions)
    with sessions() as db:
        db.add(User(USERID=1, PASSWORD="synthetic", USERNAME="Reader"))
        db.flush()
        db.add(City(CITYID=1, USERID=1, CITY_SLUG="synthetic", UI_PORT=18010, API_PORT=18000))
        db.flush()
        db.add_all([
            Building(BUILDINGID="room-a", BUILDINGNAME="A", CITYID=1),
            Building(BUILDINGID="room-b", BUILDINGNAME="B", CITYID=1),
            UserSettings(USERID=1),
        ])
        db.commit()
    manager = _manager()
    manager.SessionLocal = sessions
    manager.user_id = 1
    manager.user_current_building_id = "room-a"
    manager.building_histories = {}
    manager.get_building_history = lambda bid: fetch_building_messages(sessions, bid)
    manager.get_region = lambda rid: SimpleNamespace(state={"phase": "playing"})
    manager.get_region_buildings = lambda rid: [SimpleNamespace(building_id=bid) for bid in ("room-a", "room-b")]
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/chat")
    app.include_router(config.router, prefix="/api/config")
    app.include_router(world.router, prefix="/api/world")
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as client:
        yield client, manager
    engine.dispose()


@pytest.mark.parametrize("entity_type,entity_id", [("user", "2"), ("ai", "synthetic-persona")])
def test_producer_storage_api_history_and_new_arrivals_keep_all_records(history_client, entity_type, entity_id):
    client, manager = history_client
    occupancy = OccupancyManager.__new__(OccupancyManager)
    occupancy.occupants = {"room-a": [entity_id, "1", "witness"], "room-b": ["1", "witness"]}
    occupancy.building_map = {"room-a": SimpleNamespace(name="A"), "room-b": SimpleNamespace(name="B")}
    occupancy._build_building_info = lambda bid: {"name": "B", "system_instruction": "synthetic room"}
    events = occupancy._build_occupancy_events(
        entity_id, entity_type, "Synthetic", "room-a", "room-b",
        datetime(2026, 10, 5, tzinfo=timezone.utc), move_key="synthetic-move",
    )
    initial = insert_building_message(manager.SessionLocal, "room-a", {
        "role": "user", "content": "Before moving", "heard_by": ["1", "witness"],
    })
    for bid, message in events:
        assert insert_building_message(manager.SessionLocal, bid, message) is not None
    raw_before = {bid: manager.get_building_history(bid) for bid in ("room-a", "room-b")}

    for visible in (True, False, True, False):
        response = client.put("/api/config/movement-notices", json={"show_movement_notices": visible})
        assert response.status_code == 200
        history = client.get("/api/chat/history", params={"building_id": "room-a", "limit": 1}).json()
        assert history["has_more"] is True
        movement = history["history"][0]
        assert movement["is_movement_notice"] is True
        assert movement["building_id"] == "room-a"
        # Hidden events still advance the raw pagination cursor, with no mutation.
        previous = client.get("/api/chat/history", params={
            "building_id": "room-a", "before": movement["id"], "limit": 1,
        }).json()
        assert previous["history"][0]["id"] == initial["message_id"]
        assert previous["history"][0]["is_movement_notice"] is False
        arrivals = client.get("/api/chat/history", params={
            "building_id": "room-a", "after": initial["message_id"],
        }).json()
        assert arrivals["history"] == history["history"]
        game = client.get("/api/world/regions/synthetic/game/log").json()
        # The viewer witnessed both rooms' events; each retains its source room.
        assert {m["building_id"] for m in game["history"] if m["is_movement_notice"]} == {"room-a", "room-b"}
        for bid in raw_before:
            assert manager.get_building_history(bid) == raw_before[bid]

    for bid, produced in events:
        stored = raw_before[bid][-1]
        # The exact metadata consumed by persona history and perception survives.
        assert stored["metadata"] == produced["metadata"]
        assert stored["heard_by"] == produced["heard_by"]
        assert stored["ingested_by"] == produced["ingested_by"]
        assert stored["content"] == produced["content"]
