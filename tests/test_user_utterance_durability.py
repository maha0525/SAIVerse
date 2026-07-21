from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from manager.runtime import RuntimeService


def _runtime(responders=None) -> RuntimeService:
    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(
        user_id="user_1",
        user_current_building_id="room",
    )
    service.SessionLocal = MagicMock(name="SessionLocal")
    service.personas = {}
    service.occupants = {}
    service.building_map = {}
    service._canonical_building_id = lambda building_id: building_id
    service._build_responding_personas = lambda building_id: list(responders or [])
    service._save_modified_buildings = lambda: None
    service.manager = SimpleNamespace(
        _active_stop_events={},
        _active_sse_callbacks={},
        pulse_dispatcher=MagicMock(),
    )
    return service


def _events(lines) -> list[dict]:
    return [json.loads(line) for line in lines if line.strip()]


def test_stream_does_not_dispatch_when_user_insert_fails() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch("database.building_messages.insert_building_message_with_location_guard", return_value=None):
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    assert any(event.get("error_code") == "persistence_failed" for event in events)
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_stream_persists_user_message_even_when_room_is_empty() -> None:
    service = _runtime([])
    saved = {
        "message_id": "room:1",
        "client_message_id": "cmd-1",
        "_was_inserted": True,
    }

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ) as insert:
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    assert any(event.get("message_id") == "room:1" for event in events)
    assert insert.call_count == 1
    inserted_entry = insert.call_args.args[2]
    assert inserted_entry["content"] == "hello"
    assert inserted_entry["heard_by"] == ["user_1"]


def test_duplicate_command_returns_canonical_id_without_redispatch() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    existing = {
        "message_id": "room:7",
        "client_message_id": "cmd-1",
        "_was_inserted": False,
    }

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=existing,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    duplicate = next(event for event in events if event["type"] == "duplicate_command")
    assert duplicate["message_id"] == "room:7"
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_new_durable_command_dispatches_once() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        list(
            service.handle_user_input_stream(
                "hello",
                building_id="room",
                client_message_id="cmd-1",
            )
        )

    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_non_streaming_insert_failure_is_terminal() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch("database.building_messages.insert_building_message_with_location_guard", return_value=None):
        replies = service.handle_user_input("hello")

    assert "処理を開始しませんでした" in replies[0]
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()
