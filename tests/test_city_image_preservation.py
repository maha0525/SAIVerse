"""City の画像設定は未送信なら保持し、明示的な null / 空文字だけ解除する。

合成 City の作成・初回保存・再保存を実 API → SAIVerseManager → AdminService →
一時 SQLite DB → DB 一覧 / CityMap API まで通す。実世界・ペルソナ・LLM は使わない。
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.deps import get_db, get_manager
from api.routes import db_manager, info, world
from database.models import Base, City, User
from manager.admin import AdminService
from saiverse.saiverse_manager import SAIVerseManager


AVATAR = "/api/static/user_icons/synthetic-host.png"
BACKGROUND = "/api/media/images/synthetic-city.png"
BASE_UPDATE = {
    "name": "合成テストの街",
    "description": "画像保持の隔離検証用",
    "online_mode": False,
    "ui_port": 18010,
    "api_port": 18000,
    "timezone": "UTC",
    "language": "ja",
}


@pytest.fixture
def city_api(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'city.db'}", connect_args={"check_same_thread": False}
    )
    try:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        with sessions() as db:
            db.add(User(USERID=1, USERNAME="synthetic-user", PASSWORD="test-only"))
            db.commit()

        # Use the real manager forwarding methods without starting a world.
        manager = SAIVerseManager.__new__(SAIVerseManager)
        manager.SessionLocal = sessions
        manager.city_id = 1
        manager.buildings = []
        manager.regions = {}
        manager.personas = {}
        manager.state = SimpleNamespace(
            user_id=1, city_id=1, personas={}, timezone_name="UTC", timezone_info=None,
            user_current_building_id=None,
        )
        manager.reload_host_avatar = Mock()
        admin = AdminService.__new__(AdminService)
        admin.SessionLocal = sessions
        admin.manager = manager
        admin.state = manager.state
        # No file reads, runtime startup, or persona callbacks are needed here.
        admin._update_timezone_cache = Mock()
        admin._load_cities_from_db = Mock()
        manager.admin = admin

        def isolated_db():
            with sessions() as db:
                yield db

        app = FastAPI()
        app.include_router(world.router, prefix="/api/world")
        app.include_router(db_manager.router, prefix="/api/db")
        app.include_router(info.router, prefix="/api/info")
        app.dependency_overrides[get_manager] = lambda: manager
        app.dependency_overrides[get_db] = isolated_db
        with TestClient(app) as client:
            create = {key: value for key, value in BASE_UPDATE.items() if key != "online_mode"}
            response = client.post("/api/world/cities", json={"slug": "synthetic_city", **create})
            assert response.status_code == 200, response.text
            yield SimpleNamespace(client=client, manager=manager, sessions=sessions)
    finally:
        engine.dispose()


def _save(api, **changes):
    response = api.client.put("/api/world/cities/1", json={**BASE_UPDATE, **changes})
    assert response.status_code == 200, response.text


def _read_back(api, avatar, background, *, name=BASE_UPDATE["name"]):
    # A fresh session proves persistence, not merely an in-memory assignment.
    with api.sessions() as db:
        city = db.get(City, 1)
        assert city.HOST_AVATAR_IMAGE == avatar
        assert city.MAP_BACKGROUND_IMAGE == background
        assert city.CITYNAME == name
        assert city.CITY_SLUG == "synthetic_city"
    response = api.client.get("/api/db/tables/city")
    assert response.status_code == 200, response.text
    row = response.json()[0]
    assert row["HOST_AVATAR_IMAGE"] == avatar
    assert row["MAP_BACKGROUND_IMAGE"] == background
    assert row["CITYNAME"] == name
    response = api.client.get("/api/info/city-map")
    assert response.status_code == 200, response.text
    assert response.json()["map_background_image"] == background
    assert response.json()["city_name"] == name


def _save_initial_images(api):
    _save(api, host_avatar_path=AVATAR, map_background_image=BACKGROUND)
    _read_back(api, AVATAR, BACKGROUND)
    api.manager.reload_host_avatar.assert_called_once_with(AVATAR)
    api.manager.reload_host_avatar.reset_mock()


def test_tutorial_resave_keeps_images_after_initial_save(city_api):
    _save_initial_images(city_api)
    # The tutorial obtains the existing row, but only sends these seven fields.
    row = city_api.client.get("/api/db/tables/city").json()[0]
    payload = {
        "name": "チュートリアルで改名",
        "description": row["DESCRIPTION"],
        "online_mode": row["START_IN_ONLINE_MODE"],
        "ui_port": row["UI_PORT"],
        "api_port": row["API_PORT"],
        "timezone": "Asia/Tokyo",
        "language": "en",
    }
    for _ in range(2):
        response = city_api.client.put("/api/world/cities/1", json=payload)
        assert response.status_code == 200, response.text
        _read_back(city_api, AVATAR, BACKGROUND, name=payload["name"])
    with city_api.sessions() as db:
        city = db.get(City, 1)
        assert city.TIMEZONE == "Asia/Tokyo"
        assert city.LANGUAGE == "en"
    city_api.manager.reload_host_avatar.assert_not_called()


def test_image_free_city_creation_and_first_resave(city_api):
    _read_back(city_api, None, None)
    _save(city_api)
    _read_back(city_api, None, None)
    city_api.manager.reload_host_avatar.assert_not_called()


@pytest.mark.parametrize("field", ["host_avatar_path", "map_background_image"])
def test_only_the_supplied_image_is_replaced(city_api, field):
    _save_initial_images(city_api)
    replacement = "/api/media/images/replacement.png"
    _save(city_api, **{field: f"  {replacement}  "})
    avatar = replacement if field == "host_avatar_path" else AVATAR
    background = replacement if field == "map_background_image" else BACKGROUND
    _read_back(city_api, avatar, background)
    if field == "host_avatar_path":
        city_api.manager.reload_host_avatar.assert_called_once_with(replacement)
    else:
        city_api.manager.reload_host_avatar.assert_not_called()


@pytest.mark.parametrize("field", ["host_avatar_path", "map_background_image", "both"])
@pytest.mark.parametrize("clear", [None, "", "   "])
def test_explicit_clear_only_removes_selected_images(city_api, field, clear):
    _save_initial_images(city_api)
    fields = ["host_avatar_path", "map_background_image"] if field == "both" else [field]
    _save(city_api, **{key: clear for key in fields})
    _read_back(
        city_api,
        None if "host_avatar_path" in fields else AVATAR,
        None if "map_background_image" in fields else BACKGROUND,
    )
    if "host_avatar_path" in fields:
        city_api.manager.reload_host_avatar.assert_called_once_with(None)
    else:
        city_api.manager.reload_host_avatar.assert_not_called()


def test_resave_does_not_restore_background_from_an_earlier_read(city_api):
    _save_initial_images(city_api)
    old_row = city_api.client.get("/api/db/tables/city").json()[0]
    new_background = "/api/media/images/new-map.png"
    response = city_api.client.patch(
        "/api/world/cities/1/map-background", json={"map_background_image": new_background}
    )
    assert response.status_code == 200, response.text
    _save(city_api, name=old_row["CITYNAME"])
    _read_back(city_api, AVATAR, new_background)


@pytest.mark.parametrize("entrypoint", ["manager", "admin"])
def test_direct_caller_omission_also_preserves_images(city_api, entrypoint):
    _save_initial_images(city_api)
    target = city_api.manager if entrypoint == "manager" else city_api.manager.admin
    result = target.update_city(
        1, BASE_UPDATE["name"], BASE_UPDATE["description"], False, 18010, 18000, "UTC",
    )
    assert not result.startswith("Error:"), result
    _read_back(city_api, AVATAR, BACKGROUND)
    city_api.manager.reload_host_avatar.assert_not_called()


def test_legacy_host_upload_still_replaces_only_the_avatar(city_api, tmp_path):
    _save_initial_images(city_api)
    upload = tmp_path / "synthetic-upload.png"
    replacement = "/api/static/user_icons/uploaded-host.png"
    process_upload = Mock(return_value=replacement)
    city_api.manager.admin._process_avatar_upload = process_upload
    result = city_api.manager.update_city(
        1, BASE_UPDATE["name"], BASE_UPDATE["description"], False, 18010, 18000, "UTC",
        host_avatar_upload=str(upload),
    )
    assert not result.startswith("Error:"), result
    process_upload.assert_called_once_with("host_1", upload)
    _read_back(city_api, replacement, BACKGROUND)
    city_api.manager.reload_host_avatar.assert_called_once_with(replacement)


@pytest.mark.parametrize("field", ["host_avatar_path", "map_background_image"])
def test_image_request_fields_keep_string_or_null_validation(city_api, field):
    _save_initial_images(city_api)
    response = city_api.client.put("/api/world/cities/1", json={**BASE_UPDATE, field: 123})
    assert response.status_code == 422, response.text
    _read_back(city_api, AVATAR, BACKGROUND)


def test_openapi_schema_does_not_expose_the_internal_unset_sentinel(city_api):
    response = city_api.client.get("/openapi.json")
    assert response.status_code == 200, response.text
    schema = response.json()["components"]["schemas"]["CityUpdate"]
    for field in ("host_avatar_path", "map_background_image"):
        assert schema["properties"][field]["anyOf"] == [{"type": "string"}, {"type": "null"}]
        assert field not in schema["required"]
