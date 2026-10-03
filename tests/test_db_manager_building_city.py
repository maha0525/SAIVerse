"""汎用 DB 更新も既存 Building の City を変えない (W7 D5 / City intent §4-1)。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import db_manager
from database.models import Base, Building, City, User


@pytest.fixture
def building_api():
    """実ルートを合成 SQLite だけに接続し、トランザクションも観測する。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    rollbacks = []
    event.listen(sessions, "after_rollback", lambda session: rollbacks.append(True))
    with sessions() as db:
        db.add(User(USERID=1, USERNAME="synthetic", PASSWORD="not-a-credential"))
        db.flush()
        db.add_all([
            City(CITYID=1, USERID=1, CITY_SLUG="synthetic_a", UI_PORT=3000, API_PORT=8000),
            City(CITYID=2, USERID=1, CITY_SLUG="synthetic_b", UI_PORT=3001, API_PORT=8001),
        ])
        db.flush()
        db.add(Building(BUILDINGID="synthetic", CITYID=1, BUILDINGNAME="Original", CAPACITY=3))
        db.commit()

    app = FastAPI()
    app.include_router(db_manager.router, prefix="/api/db")

    def get_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[db_manager.get_db] = get_db
    try:
        with TestClient(app) as client:
            yield client, sessions, rollbacks
    finally:
        engine.dispose()


@pytest.mark.parametrize("city_fields", [
    {}, {"CITYID": 1}, {"CITYID": "1"}, {"CITYID": "01"},
    {"CITYID": "1.0"}, {"CITYID": 1.0}, {"CITYID": "1e0"},
])
def test_ordinary_update_preserves_city(building_api, city_fields):
    client, sessions, rollbacks = building_api
    response = client.post("/api/db/tables/building", json={"data": {
        "BUILDINGID": "synthetic", "BUILDINGNAME": "Renamed", "CAPACITY": 5,
        **city_fields,
    }})

    assert response.status_code == 200
    assert response.json()["success"] is True
    with sessions() as db:
        building = db.get(Building, "synthetic")
        assert building.CITYID == 1
        assert building.BUILDINGNAME == "Renamed"
        assert building.CAPACITY == 5
    assert rollbacks == []


@pytest.mark.parametrize("city_id", [2, "2"])
def test_new_building_can_choose_city(building_api, city_id):
    client, sessions, _ = building_api
    response = client.post("/api/db/tables/building", json={"data": {
        "BUILDINGID": "new_synthetic", "CITYID": city_id, "BUILDINGNAME": "New", "CAPACITY": 4,
    }})

    assert response.status_code == 200
    with sessions() as db:
        building = db.get(Building, "new_synthetic")
        assert building.CITYID == 2
        assert building.BUILDINGNAME == "New"
        assert building.CAPACITY == 4


@pytest.mark.parametrize("city_id", [2, "2", " 2 ", None, "", 1.5, "1.5", "not-a-city"])
def test_city_change_rejected_and_entire_update_rolled_back(building_api, city_id):
    client, sessions, rollbacks = building_api
    response = client.post("/api/db/tables/building", json={"data": {
        "BUILDINGID": "synthetic", "CITYID": city_id,
        "BUILDINGNAME": "Must not be saved", "CAPACITY": 99,
    }})

    assert response.status_code == 400
    assert "The city of 'Original' cannot be changed" in response.json()["detail"]
    assert "dedicated migration" in response.json()["detail"]
    assert rollbacks == [True]
    with sessions() as db:
        building = db.get(Building, "synthetic")
        assert building.CITYID == 1
        assert building.BUILDINGNAME == "Original"
        assert building.CAPACITY == 3

    # 拒否した後も、別リクエストで通常項目を保存できる。
    response = client.post("/api/db/tables/building", json={"data": {
        "BUILDINGID": "synthetic", "DESCRIPTION": "Still editable",
    }})
    assert response.status_code == 200
    with sessions() as db:
        assert db.get(Building, "synthetic").DESCRIPTION == "Still editable"
