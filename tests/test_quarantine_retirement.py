"""DB history stays usable with unreadable legacy files, without any LLM calls."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from api.routes import chat, system
from database.models import Base
from manager.gateway import GatewayMixin
from manager.history import HistoryMixin
from manager.initialization import InitializationMixin
from persona.history_manager import HistoryManager
from saiverse import app_state


class _Manager(InitializationMixin, HistoryMixin, GatewayMixin):
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'world.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    manager = _Manager()
    manager.SessionLocal = sessionmaker(bind=engine)
    manager.saiverse_home = tmp_path
    manager.city_name = "city_a"
    manager.buildings = [SimpleNamespace(building_id="room")]
    manager.building_map = {"room": SimpleNamespace(name="Room")}
    manager.user_current_building_id = "room"
    manager.user_display_name = "Fixture user"
    manager.state = SimpleNamespace(user_avatar_data=None)
    manager.personas = {}
    unrelated = {
        "id": "other_startup_failure", "level": "critical",
        "title": "Unrelated startup check", "message": "Keep this alert",
        "details": {"building_id": "room", "kind": "check_failed"},
    }
    manager.startup_alerts = [unrelated]
    log = tmp_path / "cities" / "city_a" / "buildings" / "room" / "log.json"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"{broken legacy log")
    manager._init_building_histories()
    monkeypatch.setattr(app_state, "manager", manager)
    app = FastAPI()
    app.include_router(system.router, prefix="/api/system")
    app.include_router(chat.router, prefix="/api/chat")
    app.dependency_overrides[get_manager] = lambda: manager
    with TestClient(app) as client:
        yield manager, client, log, unrelated
    engine.dispose()


def test_unreadable_legacy_log_does_not_block_db_writes_or_chat(world):
    manager, client, log, unrelated = world
    assert not hasattr(manager, "quarantined_buildings")
    assert not hasattr(manager, "_quarantine_building")
    assert log.read_bytes() == b"{broken legacy log"
    assert any(a.get("details", {}).get("kind") == "unreadable" for a in manager.startup_alerts)
    manager.add_building_event("room", {"role": "host", "content": "World event"})
    manager._append_gateway_history("room", {"role": "user", "content": "Gateway message"})
    history = HistoryManager(
        persona_id="synthetic",
        persona_log_path=manager.saiverse_home / "personas" / "synthetic" / "log.json",
        building_memory_paths={"room": log},
        db_session_factory=manager.SessionLocal,
    )
    history.add_message({"role": "assistant", "content": "Persona message"}, "room")
    history.add_to_building_only("room", {"role": "assistant", "content": "Building only"})
    rows = manager.get_building_history("room")
    assert [row["seq"] for row in rows] == [1, 2, 3, 4]
    response = client.get("/api/chat/history")
    assert response.status_code == 200
    assert response.json().keys() == {"history", "has_more"}
    assert [m["content"] for m in response.json()["history"]] == [row["content"] for row in rows]
    assert log.read_bytes() == b"{broken legacy log"
    assert unrelated in client.get("/api/system/alerts").json()["alerts"]


def test_archiving_unreadable_log_preserves_db_and_other_alerts(world):
    manager, client, log, unrelated = world
    manager.add_building_event("room", {"role": "host", "content": "DB history"})
    before = manager.get_building_history("room")
    response = client.post("/api/system/legacy-log/room/archive")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert manager.get_building_history("room") == before
    assert client.get("/api/system/alerts").json() == {"alerts": [unrelated]}
    assert not log.exists()
    assert next(log.parent.glob("log.json.unreadable_*")).read_bytes() == b"{broken legacy log"
    manager._init_building_histories()
    assert manager.startup_alerts == [unrelated]
    assert manager.startup_seq_watermark == {"room": 1}


def test_retired_routes_are_absent_and_cannot_mutate_files(world):
    manager, client, log, _ = world
    manager.add_building_event("room", {"role": "host", "content": "Keep me"})
    before = manager.get_building_history("room")
    alerts = list(manager.startup_alerts)
    assert client.get("/api/system/quarantine").status_code == 404
    assert client.post("/api/system/quarantine/room/restore", json={"backup_filename": "anything"}).status_code == 404
    assert client.post("/api/system/quarantine/room/reset").status_code == 404
    schema = client.get("/openapi.json").json()
    assert not any("quarantine" in path for path in schema["paths"])
    assert "quarantined" not in schema["components"]["schemas"]["ChatHistoryResponse"]["properties"]
    assert log.read_bytes() == b"{broken legacy log"
    assert manager.get_building_history("room") == before
    assert manager.startup_alerts == alerts


def test_no_current_building_returns_empty_history_without_legacy_flag(world):
    manager, client, _, _ = world
    manager.user_current_building_id = None
    assert client.get("/api/chat/history").json() == {"history": [], "has_more": False}
