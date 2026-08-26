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


# ---------------------------------------------------------------------------
# 出口 3: 発言は受け取ったのに、画面へ何も出ないまま終わる回を捕まえる
#
# 個々の失敗経路を数え上げるのではなく、結果を作る場所 (backend_worker の
# finally) で一度だけ検査する形を固定する。入口を数え切る守り方は必ず漏れる。
# 設計: docs/issues/user_utterance_path_failure_inventory.md
# ---------------------------------------------------------------------------


def _no_response(events) -> list[dict]:
    return [e for e in events if e.get("error_code") == "no_response"]


def test_an_empty_room_says_so_instead_of_ending_in_silence() -> None:
    """応答できるペルソナが一人もいない部屋。発言は残るが返事は生まれない。"""
    service = _runtime([])
    saved = {"message_id": "room:1", "_was_inserted": True}

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


def test_a_swallowed_dispatch_failure_still_reaches_the_user() -> None:
    """受け口が何も出さずに終えた回も、同じ一箇所の検査が拾う。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    # 応答も、エラーも、何ひとつイベントを出さずに戻る受け口
    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        return_value=None,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


def test_a_reported_failure_is_not_reported_twice() -> None:
    """既にエラーが出た回に no_response を重ねない (二重の説明にしない)。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}
    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=RuntimeError("boom"),
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert [e for e in events if e.get("error_code") == "unknown"]
    assert _no_response(events) == []


def test_a_persistence_failure_is_not_reported_twice() -> None:
    """早期 return の経路も同じ — 既に理由を伝えているので重ねない。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=None,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert [e for e in events if e.get("error_code") == "persistence_failed"]
    assert _no_response(events) == []


def test_an_answered_utterance_carries_no_no_response() -> None:
    """普通に喋れた回に余計な断りを出さない。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    def _speak(**kwargs):
        service.manager._active_sse_callbacks["room"]({
            "type": "say", "content": "hello back", "persona_id": "p1",
        })

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_speak,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert any(e["type"] == "say" for e in events)
    assert _no_response(events) == []


def test_progress_events_alone_do_not_count_as_an_answer() -> None:
    """考えている様子だけ出して黙って終わるのは、ユーザーから見れば無言。"""
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    def _only_think(**kwargs):
        cb = service.manager._active_sse_callbacks["room"]
        cb({"type": "think", "content": "うーん", "persona_id": "p1"})
        cb({"type": "activity", "action": "tool", "name": "search"})

    service.manager.pulse_dispatcher.dispatch_user_utterance = MagicMock(
        side_effect=_only_think,
    )

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        events = _events(
            service.handle_user_input_stream(
                "hello", building_id="room", client_message_id="cmd-1",
            )
        )

    assert len(_no_response(events)) == 1


# --- 2026-08-26 ローカルレビューの消し込み ------------------------------------
# 芯は一つ: 「試みた」を「成功した」と同じ扱いにしない。続きが一言も生まれ
# なかった回に印を降ろすと二度と押せなくなり、履歴が読めなかった回を「無い」
# と言うと送り直しで同じ発言が二度載る。


def _interrupted_message(persona_id: str = "p1") -> dict:
    return {
        "message_id": "room:7",
        "role": "assistant",
        "persona_id": persona_id,
        "content": "途中まで",
        "metadata": {"_interrupted": True},
    }


def test_continue_keeps_the_mark_when_nothing_was_spoken() -> None:
    """続きが一言も生まれなかった回は、中断の印を降ろさない。"""
    history_manager = MagicMock()
    persona = SimpleNamespace(persona_id="p1", history_manager=history_manager)
    service = _runtime([persona])
    service.personas = {"p1": persona}
    service.manager.get_building_history = lambda building_id: [_interrupted_message()]

    def _silent_pulse(building_id, target, user_input, spoke=None):
        # 何も喋らないまま error だけ出して終わる Pulse。
        yield json.dumps({"type": "error", "error_code": "unknown"}) + "\n"

    service._stream_persona_pulse = _silent_pulse
    events = _events(service.continue_persona_message_stream("room:7"))

    assert any(event.get("error_code") == "unknown" for event in events)
    history_manager.update_building_message.assert_not_called()


def test_continue_clears_the_mark_once_the_persona_actually_spoke() -> None:
    """続きが生まれた回は、これまでどおり印を降ろす。"""
    history_manager = MagicMock()
    persona = SimpleNamespace(persona_id="p1", history_manager=history_manager)
    service = _runtime([persona])
    service.personas = {"p1": persona}
    service.manager.get_building_history = lambda building_id: [_interrupted_message()]

    def _speaking_pulse(building_id, target, user_input, spoke=None):
        if spoke is not None:
            spoke["value"] = True
        yield json.dumps({"type": "say", "content": "続きです"}) + "\n"

    service._stream_persona_pulse = _speaking_pulse
    _events(service.continue_persona_message_stream("room:7"))

    history_manager.update_building_message.assert_called_once()
    _, kwargs = history_manager.update_building_message.call_args
    assert kwargs["metadata"]["_interrupted"] is False


def test_retry_does_not_ask_for_a_resend_when_the_history_cannot_be_read() -> None:
    """履歴が読めなかった回に「もう一度送ってください」と言わない。

    言うと送り直しで同じ発言が二度載る — この口が防ごうとしているものそのもの。
    """
    service = _runtime([])

    def _boom(building_id):
        raise RuntimeError("db is down")

    service.manager.get_building_history = _boom
    events = _events(service.retry_user_message_stream("room:7"))

    assert [event.get("error_code") for event in events] == ["history_unavailable"]
    assert not any("もう一度送ってください" in (e.get("content") or "") for e in events)


def test_retry_still_reports_a_genuinely_missing_message() -> None:
    """本当に記録が無い回は、これまでどおり送り直しを勧める。"""
    service = _runtime([])
    service.manager.get_building_history = lambda building_id: []
    events = _events(service.retry_user_message_stream("room:7"))

    assert [event.get("error_code") for event in events] == ["message_not_found"]


def test_continue_separates_an_unreadable_history_from_a_missing_message() -> None:
    """continue 側も同じ区別を持つ (片方だけ直すと隣を忘れた形が残る)。"""
    service = _runtime([])

    def _boom(building_id):
        raise RuntimeError("db is down")

    service.manager.get_building_history = _boom
    unreadable = _events(service.continue_persona_message_stream("room:7"))

    service.manager.get_building_history = lambda building_id: []
    missing = _events(service.continue_persona_message_stream("room:7"))

    assert [e.get("error_code") for e in unreadable] == ["history_unavailable"]
    assert [e.get("error_code") for e in missing] == ["message_not_found"]
