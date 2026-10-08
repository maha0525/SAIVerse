"""入退室通知の画面設定: 保存・未送信/解除・再接続・旧 DB 移行の契約。

FastAPI → SAIVerseManager → AdminService → 隔離 SQLite を通す。
本番 manager / persona / LLM は起動せず、履歴の行は保存前後で照合する。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base, Building, BuildingMessage, City, User, UserSettings


@pytest.fixture
def settings_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path / "home"))
    path = tmp_path / "settings.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([
            User(USERID=1, USERNAME="test-owner", PASSWORD="unused"),
            User(USERID=2, USERNAME="other-owner", PASSWORD="unused"),
            City(CITYID=1, USERID=1, CITY_SLUG="test_city", UI_PORT=18010, API_PORT=18000),
            Building(CITYID=1, BUILDINGID="b1", BUILDINGNAME="Room 1"),
            Building(CITYID=1, BUILDINGID="b2", BUILDINGNAME="Room 2"),
            Building(CITYID=1, BUILDINGID="b3", BUILDINGNAME="Room 3"),
            UserSettings(USERID=2, SHOW_MOVEMENT_NOTICES=False),
            BuildingMessage(
                building_id="b1", seq=1, role="host", content="Synthetic user entered.",
                timestamp="2026-10-05T00:00:00+00:00", event_type="occupancy",
                event_data=json.dumps({"action": "enter", "entity_type": "user", "entity_id": "1"}),
                heard_by='["synthetic-persona"]', ingested_by='["synthetic-persona"]',
            ),
        ])
        db.commit()
    import database.session as session_module

    monkeypatch.setattr(session_module, "SessionLocal", factory)
    yield SimpleNamespace(path=path, engine=engine, factory=factory)
    engine.dispose()


def _client(factory):
    from api.deps import get_manager
    from api.routes import config, world
    from manager.admin import AdminService
    from saiverse.saiverse_manager import SAIVerseManager

    admin = SimpleNamespace(SessionLocal=factory)
    admin.update_building = MethodType(AdminService.update_building, admin)
    manager = SimpleNamespace(admin=admin, building_map={})
    manager.update_building = MethodType(SAIVerseManager.update_building, manager)
    app = FastAPI()
    app.include_router(config.router, prefix="/api/config")
    app.include_router(world.router, prefix="/api/world")
    app.dependency_overrides[get_manager] = lambda: manager
    return TestClient(app)


@pytest.fixture
def client(settings_db):
    with _client(settings_db.factory) as client:
        yield client


def _building_payload(building_id="b1", **extra):
    return {
        "name": f"Room {building_id[1:]}", "description": "", "capacity": 1,
        "system_instruction": "", "city_id": 1, "tool_ids": [], "auto_interval": 10,
        **extra,
    }


def _read_settings(client):
    response = client.get("/api/config/movement-notices")
    assert response.status_code == 200
    return response.json()


def _save_global(client, value):
    response = client.put("/api/config/movement-notices", json={"show_movement_notices": value})
    assert response.status_code == 200
    return response.json()


def _save_building(client, building_id="b1", **extra):
    return client.put(f"/api/world/buildings/{building_id}", json=_building_payload(building_id, **extra))


def _history_rows(factory):
    with factory() as db:
        return [dict(row) for row in db.execute(select(BuildingMessage.__table__)).mappings()]


def test_absent_global_defaults_to_shown_without_creating_a_row(client, settings_db):
    assert _read_settings(client) == {"show_movement_notices": True, "building_overrides": {}}
    with settings_db.factory() as db:
        assert db.get(UserSettings, 1) is None
        assert db.get(UserSettings, 2).SHOW_MOVEMENT_NOTICES is False
        assert db.get(Building, "b1").SHOW_MOVEMENT_NOTICES is None


def test_new_settings_row_uses_shown_default(client, settings_db):
    with settings_db.factory() as db:
        db.add(UserSettings(USERID=1, FAVORITE_MODELS='["kept-model"]'))
        db.commit()
    assert _read_settings(client)["show_movement_notices"] is True
    _save_global(client, False)
    with settings_db.factory() as db:
        assert db.get(UserSettings, 1).FAVORITE_MODELS == '["kept-model"]'
        assert db.get(UserSettings, 2).SHOW_MOVEMENT_NOTICES is False


def test_raw_sql_insert_keeps_the_display_default(settings_db):
    with sqlite3.connect(settings_db.path) as db:
        db.execute(
            "INSERT INTO user_settings (USERID, TUTORIAL_COMPLETED, LAST_TUTORIAL_VERSION) "
            "VALUES (1, 0, 1)"
        )
        assert db.execute(
            "SELECT SHOW_MOVEMENT_NOTICES FROM user_settings WHERE USERID=1"
        ).fetchone() == (1,)


@pytest.mark.parametrize("global_value", [True, False])
@pytest.mark.parametrize("override", [None, True, False])
def test_all_global_and_building_states_round_trip(client, settings_db, global_value, override):
    assert _save_building(client, show_movement_notices=override).status_code == 200
    expected = {
        "show_movement_notices": global_value,
        "building_overrides": {} if override is None else {"b1": override},
    }
    assert _save_global(client, global_value) == expected
    assert _read_settings(client) == expected
    with settings_db.factory() as db:
        assert db.get(Building, "b1").SHOW_MOVEMENT_NOTICES is override
        assert db.get(Building, "b2").SHOW_MOVEMENT_NOTICES is None


def test_explicit_overrides_survive_global_changes_and_omitted_building_field(client):
    assert _save_building(client, "b1", show_movement_notices=False).status_code == 200
    assert _save_building(client, "b2", show_movement_notices=True).status_code == 200
    for global_value in (False, True, False):
        result = _save_global(client, global_value)
        assert result["building_overrides"] == {"b1": False, "b2": True}
    assert _save_building(client, "b1").status_code == 200
    assert _read_settings(client)["building_overrides"] == {"b1": False, "b2": True}
    assert _save_building(client, "b1", show_movement_notices=None).status_code == 200
    assert _read_settings(client)["building_overrides"] == {"b2": True}
    assert _read_settings(client)["show_movement_notices"] is False


@pytest.mark.parametrize("bad_value", [None, 0, 1, "true", "false", "inherit", [], {}])
def test_global_rejects_non_boolean_without_changing_state(client, bad_value):
    _save_global(client, False)
    response = client.put("/api/config/movement-notices", json={"show_movement_notices": bad_value})
    assert response.status_code == 422
    assert _read_settings(client)["show_movement_notices"] is False


def test_global_field_is_required(client):
    assert client.put("/api/config/movement-notices", json={}).status_code == 422
    assert _read_settings(client)["show_movement_notices"] is True


@pytest.mark.parametrize("bad_value", [0, 1, "true", "false", "inherit", [], {}])
def test_building_rejects_non_boolean_without_changing_state(client, bad_value):
    assert _save_building(client, show_movement_notices=False).status_code == 200
    assert _save_building(client, show_movement_notices=bad_value).status_code == 422
    assert _read_settings(client)["building_overrides"] == {"b1": False}


@pytest.mark.parametrize("bad_value", [0, 1, "true", "false", [], {}])
def test_admin_rejects_non_boolean_from_non_http_callers(settings_db, bad_value):
    from manager.admin import AdminService

    admin = SimpleNamespace(SessionLocal=settings_db.factory)
    result = AdminService.update_building(
        admin, "b1", "Room 1", 1, "", "", 1, [], 10,
        show_movement_notices=bad_value,
    )
    assert result.startswith("Error:")
    with settings_db.factory() as db:
        assert db.get(Building, "b1").SHOW_MOVEMENT_NOTICES is None


def test_setting_changes_never_modify_history(client, settings_db):
    before = _history_rows(settings_db.factory)
    assert len(before) == 1
    for global_value in (False, True):
        _save_global(client, global_value)
        for override in (False, True, None):
            assert _save_building(client, show_movement_notices=override).status_code == 200
            assert _history_rows(settings_db.factory) == before


def test_saved_settings_survive_new_engine_and_app(client, settings_db, monkeypatch):
    import database.session as session_module

    _save_global(client, False)
    assert _save_building(client, "b1", show_movement_notices=True).status_code == 200
    assert _save_building(client, "b2", show_movement_notices=False).status_code == 200
    settings_db.engine.dispose()
    restarted_engine = create_engine(
        f"sqlite:///{settings_db.path}", connect_args={"check_same_thread": False},
    )
    restarted_factory = sessionmaker(bind=restarted_engine)
    monkeypatch.setattr(session_module, "SessionLocal", restarted_factory)
    try:
        with _client(restarted_factory) as restarted_client:
            assert _read_settings(restarted_client) == {
                "show_movement_notices": False, "building_overrides": {"b1": True, "b2": False},
            }
            assert _save_building(restarted_client, "b1", show_movement_notices=None).status_code == 200
            assert _read_settings(restarted_client)["building_overrides"] == {"b2": False}
    finally:
        restarted_engine.dispose()


def test_global_commit_failure_does_not_report_success_or_change_value(client, settings_db, monkeypatch):
    import database.session as session_module

    _save_global(client, False)

    class FailingSession(Session):
        def commit(self):
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(session_module, "SessionLocal", sessionmaker(bind=settings_db.engine, class_=FailingSession))
    response = client.put("/api/config/movement-notices", json={"show_movement_notices": True})
    assert response.status_code == 500
    monkeypatch.setattr(session_module, "SessionLocal", settings_db.factory)
    assert _read_settings(client)["show_movement_notices"] is False


def test_global_response_read_failure_rolls_back_the_save(client, monkeypatch):
    from api.routes import config

    _save_global(client, False)

    def fail_payload(db):
        raise RuntimeError("synthetic response read failure")

    with monkeypatch.context() as patch:
        patch.setattr(config, "_movement_notices_payload", fail_payload)
        response = client.put("/api/config/movement-notices", json={"show_movement_notices": True})
        assert response.status_code == 500
    assert _read_settings(client)["show_movement_notices"] is False


@pytest.mark.parametrize("migration_mode", ["additive", "rewrite-cli"])
def test_migration_on_copied_old_database_keeps_history_and_defaults(settings_db, tmp_path, migration_mode):
    from database.migrate import needs_migration, try_additive_migration

    with settings_db.factory() as db:
        db.add(UserSettings(USERID=1, FAVORITE_MODELS='["preserved"]'))
        db.commit()
    before_history = _history_rows(settings_db.factory)
    settings_db.engine.dispose()
    with sqlite3.connect(settings_db.path) as db:
        db.execute('ALTER TABLE user_settings DROP COLUMN "SHOW_MOVEMENT_NOTICES"')
        db.execute('ALTER TABLE building DROP COLUMN "SHOW_MOVEMENT_NOTICES"')
    copied_path = tmp_path / "old-schema-copy.db"
    shutil.copy2(settings_db.path, copied_path)
    assert needs_migration(str(copied_path)) is True
    if migration_mode == "additive":
        assert try_additive_migration(str(copied_path)) is True
    else:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "database" / "migrate.py"), "--db", str(copied_path)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert needs_migration(str(copied_path)) is False
    assert try_additive_migration(str(copied_path)) is True  # repeat is harmless
    with sqlite3.connect(copied_path) as db:
        assert db.execute('SELECT "SHOW_MOVEMENT_NOTICES" FROM user_settings ORDER BY USERID').fetchall() == [(1,), (1,)]
        assert db.execute('SELECT "SHOW_MOVEMENT_NOTICES" FROM building').fetchall() == [(None,), (None,), (None,)]
        assert db.execute('SELECT FAVORITE_MODELS FROM user_settings WHERE USERID=1').fetchone() == ('["preserved"]',)
        global_column = next(row for row in db.execute('PRAGMA table_info(user_settings)') if row[1] == "SHOW_MOVEMENT_NOTICES")
        assert global_column[3] == 1  # NOT NULL
        assert global_column[4] == "1"  # Both migration paths preserve the database default.
    copied_engine = create_engine(f"sqlite:///{copied_path}")
    try:
        assert _history_rows(sessionmaker(bind=copied_engine)) == before_history
    finally:
        copied_engine.dispose()
    # The synthetic source was never migrated, proving this was a copy-only migration.
    assert needs_migration(str(settings_db.path)) is True
