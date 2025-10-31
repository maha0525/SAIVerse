import base64
import sys
from pathlib import Path

# Ensure repository root is importable when running the tests standalone.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, Building as BuildingModel, City as CityModel, User as UserModel
from saiverse_manager.services import (
    AvatarAssets,
    CityConfigService,
    GatewayBootstrapper,
    HistoryLoader,
    SDSClient,
    SDSClientError,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status: {self.status_code}")

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self):
        self.requests = []
        self.next_response = FakeResponse()

    def configure(self, *, status_code=200, json_data=None):
        self.next_response = FakeResponse(status_code=status_code, json_data=json_data)

    def post(self, url, json=None, timeout=None):
        self.requests.append(("POST", url, json, timeout))
        return self.next_response

    def get(self, url, timeout=None):
        self.requests.append(("GET", url, None, timeout))
        return self.next_response


@pytest.fixture
def temporary_database(tmp_path):
    db_path = tmp_path / "test.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as session:
        user = UserModel(USERID=1, PASSWORD="pw", USERNAME="Tester", LOGGED_IN=False)
        session.add(user)
        session.flush()

        city = CityModel(
            USERID=user.USERID,
            CITYNAME="TestCity",
            UI_PORT=9000,
            API_PORT=9100,
            START_IN_ONLINE_MODE=True,
            TIMEZONE="UTC",
        )
        session.add(city)
        session.flush()

        other_city = CityModel(
            USERID=user.USERID,
            CITYNAME="OtherCity",
            UI_PORT=9001,
            API_PORT=9101,
            START_IN_ONLINE_MODE=False,
            TIMEZONE="Asia/Tokyo",
        )
        session.add(other_city)

        building = BuildingModel(
            CITYID=city.CITYID,
            BUILDINGID="bld-1",
            BUILDINGNAME="Lounge",
            CAPACITY=3,
            SYSTEM_INSTRUCTION="",
            ENTRY_PROMPT="",
            AUTO_PROMPT="",
            DESCRIPTION="A quiet place",
            AUTO_INTERVAL_SEC=15,
        )
        session.add(building)
        session.commit()

    yield db_path


def test_city_config_service_loads_city_and_other_cities(temporary_database):
    service = CityConfigService("TestCity", str(temporary_database))
    config, other_cities = service.load_city_configuration()

    assert config.city_name == "TestCity"
    assert config.api_port == 9100
    assert config.timezone_name == "UTC"
    assert set(other_cities.keys()) == {"OtherCity"}
    assert other_cities["OtherCity"]["api_base_url"] == "http://127.0.0.1:9101"
    assert other_cities["OtherCity"]["timezone"] == "Asia/Tokyo"

    reloaded = service.load_other_cities(config.city_id)
    assert list(reloaded.keys()) == ["OtherCity"]


def test_history_loader_handles_paths_and_histories(tmp_path, temporary_database):
    service = CityConfigService("TestCity", str(temporary_database))
    config, _ = service.load_city_configuration()

    history_loader = HistoryLoader(config.city_name, saiverse_home=tmp_path / "saiverse")
    session = service.create_session()
    try:
        buildings = history_loader.load_buildings(session, config.city_id)
    finally:
        session.close()

    assert len(buildings) == 1
    paths = history_loader.build_memory_paths(buildings)
    history_loader.ensure_history_directories(paths.values())
    path = next(iter(paths.values()))
    sample_history = [{"role": "host", "content": "Hello"}]
    path.write_text("[{'role': 'host', 'content': 'Hello'}]".replace("'", '"'), encoding="utf-8")

    histories = history_loader.load_histories(paths)
    assert histories[buildings[0].building_id] == sample_history

    default_file = tmp_path / "default.png"
    host_file = tmp_path / "host.png"
    default_file.write_bytes(b"default")
    host_file.write_bytes(b"host")
    assets = history_loader.load_avatar_assets(default_file, host_file)
    assert isinstance(assets, AvatarAssets)
    assert assets.default_avatar.startswith("data:image/png;base64,")
    assert base64.b64decode(assets.default_avatar.split(",", 1)[1]) == b"default"


def test_sds_client_success_and_failure():
    session = FakeSession()
    client = SDSClient(
        city_name="TestCity",
        city_id=1,
        api_port=9100,
        sds_url="http://example.com",
        session=session,
    )

    session.configure(status_code=200)
    assert client.register_city() is True

    session.configure(status_code=500)
    assert client.register_city() is False

    session.configure(status_code=200)
    assert client.send_heartbeat() is True

    session.configure(status_code=500)
    assert client.send_heartbeat() is False

    session.configure(json_data={"TestCity": {}, "Other": {"city_id": 2}})
    cities = client.fetch_city_directory()
    assert "TestCity" not in cities
    assert "Other" in cities

    session.configure(status_code=500)
    with pytest.raises(SDSClientError):
        client.fetch_city_directory()


def test_gateway_bootstrapper_controls_initialisation():
    calls = []

    def initializer():
        calls.append(True)

    bootstrapper = GatewayBootstrapper(initializer, env={})
    assert bootstrapper.start_if_enabled() is False
    assert calls == []

    bootstrapper = GatewayBootstrapper(initializer, env={"SAIVERSE_GATEWAY_ENABLED": "true"})
    assert bootstrapper.start_if_enabled() is True
    assert calls == [True]
