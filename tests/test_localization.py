"""Test suite for localization, city/persona language inheritance, and prompt generation."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from api.deps import get_manager
from api.routes.people.config import router as people_config_router
from api.ui_messages import ui_message
from database.migrate import try_additive_migration
from database.models import AI, Base, City
from manager.admin import AdminService
from sai_memory.arasuji.generator import generate_text_with_empty_retry
from saiverse.persona_language import (
    get_city_language,
    get_persona_language,
    language_instruction,
    validate_language,
)
from sea.head_pipeline.sections.persona_self import (
    PersonaSelfSection,
    PersonaSelfSnapshot,
)
from sea.head_pipeline.types import LineHeadInput


@pytest.fixture
def isolated_world(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIVERSE_HOME", str(tmp_path))
    path = tmp_path / "world.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as session:
        session.add(
            City(
                CITYID=1,
                CITY_SLUG="city_a",
                CITYNAME="Test City",
                USERID=1,
                UI_PORT=8000,
                API_PORT=8001,
                LANGUAGE="ja",
            )
        )
        session.add(
            AI(
                AIID="synthetic_persona",
                HOME_CITYID=1,
                AINAME="Synthetic",
                SYSTEMPROMPT="Original identity instruction",
                LANGUAGE=None,
            )
        )
        session.commit()
    monkeypatch.setattr("database.paths.default_db_path", lambda: path)
    yield path, engine, session_factory
    engine.dispose()


def test_additive_migration_adds_language_columns_safely(isolated_world):
    path, engine, factory = isolated_world
    # Drop columns to simulate older database schema
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE city DROP COLUMN LANGUAGE"))
        conn.execute(text("ALTER TABLE ai DROP COLUMN LANGUAGE"))

    # Apply migration
    assert try_additive_migration(str(path)) is True

    # Verify both tables have LANGUAGE columns with safe defaults
    with factory() as session:
        city = session.get(City, 1)
        assert city.LANGUAGE == "ja"
        persona = session.get(AI, "synthetic_persona")
        assert persona.LANGUAGE is None or persona.LANGUAGE == "ja"
        assert persona.SYSTEMPROMPT == "Original identity instruction"


def test_city_and_persona_language_inheritance(isolated_world):
    path, engine, factory = isolated_world

    # 1. Persona with LANGUAGE=None inherits from Home City
    assert get_persona_language("synthetic_persona", path) == "ja"

    # Update City to 'en'
    with factory() as session:
        city = session.get(City, 1)
        city.LANGUAGE = "en"
        session.commit()

    # Now persona with LANGUAGE=None should inherit 'en' from City
    assert get_city_language(1, path) == "en"
    assert get_persona_language("synthetic_persona", path) == "en"

    # 2. Persona with explicit LANGUAGE overrides City setting
    with factory() as session:
        persona = session.get(AI, "synthetic_persona")
        persona.LANGUAGE = "ja"
        session.commit()

    assert get_persona_language("synthetic_persona", path) == "ja"


def test_persona_config_api_language_endpoints(isolated_world):
    path, _, factory = isolated_world
    persona = SimpleNamespace(
        persona_id="synthetic_persona",
        persona_name="Synthetic",
        persona_system_instruction="Original identity instruction",
        language="ja",
        model=None,
    )
    admin = AdminService.__new__(AdminService)
    admin.SessionLocal = factory
    admin.personas = {"synthetic_persona": persona}
    admin.state = SimpleNamespace(model=None)
    admin._set_persona_avatar = Mock()
    manager = SimpleNamespace(
        get_ai_details=admin.get_ai_details,
        update_ai=admin.update_ai,
        SessionLocal=factory,
        personas=admin.personas,
    )

    app = FastAPI()
    app.include_router(people_config_router, prefix="/people")
    app.dependency_overrides[get_manager] = lambda: manager

    with TestClient(app) as client:
        # GET returns None for unconfigured persona language, and home_city_language
        res = client.get("/people/synthetic_persona/config")
        assert res.status_code == 200
        assert res.json()["language"] is None
        assert res.json()["home_city_language"] == "ja"

        # PATCH updates language to 'en'
        patch_res = client.patch(
            "/people/synthetic_persona/config", json={"language": "en"}
        )
        assert patch_res.status_code == 200

        # Verify updated
        res = client.get("/people/synthetic_persona/config")
        assert res.json()["language"] == "en"
        assert get_persona_language("synthetic_persona", path) == "en"

        # PATCH with empty string resets language to None (follow city)
        reset_res = client.patch(
            "/people/synthetic_persona/config", json={"language": ""}
        )
        assert reset_res.status_code == 200
        res = client.get("/people/synthetic_persona/config")
        assert res.json()["language"] is None

        # PATCH invalid language fails with 422
        bad_res = client.patch(
            "/people/synthetic_persona/config", json={"language": "invalid_lang"}
        )
        assert bad_res.status_code == 422


def test_head_pipeline_persona_self_renders_language_instruction():
    persona = SimpleNamespace(
        persona_id="p1",
        persona_name="Persona 1",
        persona_system_instruction="You are a helpful assistant.",
        language="en",
    )
    section = PersonaSelfSection()
    ctx = LineHeadInput(persona_id="p1", model_key="fake", persona=persona)
    snapshot = section.capture(ctx)

    assert snapshot.language == "en"
    rendered = section.render(snapshot)
    assert rendered is not None
    assert "## あなたについて" in rendered.text
    assert "Language of your life: English" in rendered.text

    # Verify Japanese (default) does NOT append language instruction to preserve prompt cache
    snapshot_ja = PersonaSelfSnapshot(
        persona_id="p1",
        persona_name="Persona 1",
        persona_system_instruction="You are a helpful assistant.",
        language="ja",
    )
    rendered_ja = section.render(snapshot_ja)
    assert rendered_ja is not None
    assert "## あなたについて\nYou are a helpful assistant." == rendered_ja.text
    assert "Language of your life" not in rendered_ja.text

    # Test notification on language change
    new_snapshot = PersonaSelfSnapshot(
        persona_id="p1",
        persona_name="Persona 1",
        persona_system_instruction="You are a helpful assistant.",
        language="ja",
    )
    diff = section.diff_to_notifications(snapshot, new_snapshot)
    assert len(diff) == 1
    assert diff[0].kind == "persona_language_changed"
    assert "日本語" in diff[0].label

    # Backward compatibility of serialized snapshots
    old_json = json.dumps({
        "persona_id": "p1",
        "persona_name": "Old",
        "persona_system_instruction": "Old sys",
    })
    deserialized = section.deserialize_snapshot(old_json)
    assert deserialized.language == "ja"


def test_chronicle_generation_injects_language_instruction(monkeypatch):
    source_messages = [{"role": "user", "content": "Hello in original text"}]
    fake_client = Mock()
    fake_client.generate.return_value = "Generated summary response"

    # 1. Default language (ja) does NOT inject to preserve prompt cache and original prompt
    result_ja = generate_text_with_empty_retry(
        fake_client,
        source_messages,
        purpose="chronicle_summary",
        persona_id="persona_ja",
    )
    assert result_ja == "Generated summary response"
    called_messages_ja = fake_client.generate.call_args.kwargs["messages"]
    assert len(called_messages_ja) == 1
    assert called_messages_ja == source_messages

    # 2. Non-default language (en) injects instruction
    import saiverse.persona_language
    monkeypatch.setattr(saiverse.persona_language, "get_persona_language", lambda pid, db_path=None: "en")

    result_en = generate_text_with_empty_retry(
        fake_client,
        source_messages,
        purpose="chronicle_summary",
        persona_id="persona_en",
    )
    assert result_en == "Generated summary response"

    called_messages_en = fake_client.generate.call_args.kwargs["messages"]
    assert len(called_messages_en) == 2
    assert called_messages_en[0]["role"] == "system"
    assert "Language of your life" in called_messages_en[0]["content"]
    assert called_messages_en[1] == source_messages[0]

    # Source messages not mutated
    assert source_messages == [{"role": "user", "content": "Hello in original text"}]


def test_memopedia_language_instruction_preserves_prompt_for_ja(monkeypatch):
    import saiverse.persona_language
    from sai_memory.memopedia.generator import _build_system_message

    base_system_message = _build_system_message("test_keyword", "test_directions", "test_chronicle", "test_pages")

    # 1. ja persona does not append extra newlines or instructions
    monkeypatch.setattr(saiverse.persona_language, "get_persona_language", lambda pid, db_path=None: "ja")
    lang_inst_ja = saiverse.persona_language.language_instruction("ja")
    assert lang_inst_ja == ""

    system_message_ja = base_system_message
    if lang_inst_ja:
        system_message_ja += "\n\n" + lang_inst_ja
    assert system_message_ja == base_system_message
    assert not system_message_ja.endswith("\n\n")

    # 2. en persona appends language instruction
    monkeypatch.setattr(saiverse.persona_language, "get_persona_language", lambda pid, db_path=None: "en")
    lang_inst_en = saiverse.persona_language.language_instruction("en")
    assert "Language of your life: English" in lang_inst_en

    system_message_en = base_system_message
    if lang_inst_en:
        system_message_en += "\n\n" + lang_inst_en
    assert "Language of your life: English" in system_message_en



def test_ui_message_envelope():
    msg = ui_message("city.saved", name="Neo Tokyo", count=42)
    assert msg == {
        "$ui": "city.saved",
        "params": {"name": "Neo Tokyo", "count": 42},
    }
