"""move_entity の呼び出し元の、入口で止まる直行 (docs/intent/region.md §2.5) への追従。

- 意思の主がその場で読む移動 (ペルソナの移動ツール) は案内文を本人に届ける
- 機構がペルソナの意図を代行する移動 (召喚・会話終了後の帰宅) は境界を一段ずつ
  通過して目的地まで進み、止められたらそこで正直に止まる
- 移動を報せる機構 (現象トリガー・管理画面の応答) は実際の到着地を運ぶ

世界: 'inn' は Region『霧の谷』の内部で、入口は 'gate'。他は City 直属。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from manager.admin import AdminService
from manager.runtime import RuntimeService, TriggerType
from saiverse.occupancy_manager import MoveRedirectedNotice

PERSONA_ID = "air"
NOTICE_TEXT = (
    "'宿屋' は『霧の谷』の内部です。入口 '霧の谷: 入口' まで移動しました。"
    "中へ入るには入口からもう一度移動してください。"
)
LOCK_TEXT = "移動失敗: 『霧の谷』には鍵がかかっています。"


class _RegionOccupancyStub:
    """move_entity の §2.5 契約を再現する最小の移動 service。

    外から 'inn' への直行は入口 'gate' まで移動して案内文つきの成功を返し、
    'gate' からの一歩は locked なら鍵の文で拒否する。成功時は canonical
    location (persona.current_building_id / state.user_current_building_id) を
    service 側で更新する (本物の move_entity と同じ W7 柱5 の契約)。
    """

    def __init__(self, personas, state, locked=False):
        self.personas = personas
        self.state = state
        self.locked = locked
        self.calls = []

    def _sync(self, entity_id, entity_type, to_id):
        if entity_type == "ai":
            self.personas[entity_id].current_building_id = to_id
        else:
            self.state.user_current_building_id = to_id

    def move_entity(self, entity_id, entity_type, from_id, to_id):
        self.calls.append((entity_id, from_id, to_id))
        if to_id == "inn" and from_id != "gate":
            self._sync(entity_id, entity_type, "gate")
            return True, MoveRedirectedNotice(NOTICE_TEXT, current_building_id="gate")
        if to_id == "inn" and self.locked:
            return False, LOCK_TEXT
        self._sync(entity_id, entity_type, to_id)
        return True, None


def _building_map():
    return {
        bid: SimpleNamespace(name=name)
        for bid, name in (
            ("room", "まはーの部屋"),
            ("plaza", "広場"),
            ("gate", "霧の谷: 入口"),
            ("inn", "宿屋"),
            ("air_home", "エアの部屋"),
        )
    }


def _persona(current="plaza"):
    return SimpleNamespace(
        persona_id=PERSONA_ID,
        persona_name="エア",
        current_building_id=current,
        private_room_id="air_home",
        history_manager=SimpleNamespace(add_to_building_only=MagicMock()),
        _save_session_metadata=MagicMock(),
    )


def _runtime(persona, locked=False) -> RuntimeService:
    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(user_id=1, user_current_building_id="room")
    service.personas = {PERSONA_ID: persona}
    service.occupants = {}
    service.building_map = _building_map()
    service._canonical_building_id = lambda building_id: building_id
    service.manager = SimpleNamespace(_emit_trigger=MagicMock())
    service.occupancy_manager = _RegionOccupancyStub(
        service.personas, service.state, locked=locked,
    )
    return service


# ---- 移動を報せる機構: 現象トリガーは実際の到着地を運ぶ ---------------------


def test_user_move_trigger_carries_actual_arrival() -> None:
    service = _runtime(_persona())
    ok, msg = service.move_user("inn")
    assert ok is True
    assert str(msg) == NOTICE_TEXT
    service.manager._emit_trigger.assert_called_once_with(
        TriggerType.USER_MOVE, {"from_building": "room", "to_building": "gate"},
    )


def test_persona_move_trigger_carries_actual_arrival() -> None:
    service = _runtime(_persona())
    ok, _msg = service._move_persona(PERSONA_ID, "plaza", "inn")
    assert ok is True
    service.manager._emit_trigger.assert_called_once_with(
        TriggerType.PERSONA_MOVE,
        {"persona_id": PERSONA_ID, "from_building": "plaza", "to_building": "gate"},
    )


# ---- 機構代行の移動: 召喚 ---------------------------------------------------


def test_summon_into_region_passes_entrance_and_arrives() -> None:
    persona = _persona()
    service = _runtime(persona)
    ok, reason = service.summon_persona(PERSONA_ID, "inn")
    assert (ok, reason) == (True, None)
    assert service.occupancy_manager.calls == [
        (PERSONA_ID, "plaza", "inn"), (PERSONA_ID, "gate", "inn"),
    ]
    assert persona.current_building_id == "inn"
    # 1 手ごとに PERSONA_MOVE が実際の到着地で報される
    to_buildings = [
        c.args[1]["to_building"] for c in service.manager._emit_trigger.call_args_list
    ]
    assert to_buildings == ["gate", "inn"]


def test_summon_blocked_at_boundary_fails_with_lock_reason() -> None:
    persona = _persona()
    service = _runtime(persona, locked=True)
    ok, reason = service.summon_persona(PERSONA_ID, "inn")
    assert ok is False
    assert reason == LOCK_TEXT
    # 入口までは来ている
    assert persona.current_building_id == "gate"
    note = persona.history_manager.add_to_building_only.call_args.args[1]
    assert LOCK_TEXT in note["content"]


# ---- 機構代行の移動: 会話終了後の帰宅 ---------------------------------------


def test_end_conversation_return_home_through_region() -> None:
    persona = _persona(current="room")
    persona.private_room_id = "inn"  # 自室が Region の内部にある
    service = _runtime(persona)
    result = service.end_conversation(PERSONA_ID, "room")
    assert result == "Conversation with 'エア' ended."
    assert persona.current_building_id == "inn"


def test_end_conversation_stopped_midway_warns_and_keeps_reply(caplog) -> None:
    persona = _persona(current="room")
    persona.private_room_id = "inn"
    service = _runtime(persona, locked=True)
    with caplog.at_level(logging.WARNING):
        result = service.end_conversation(PERSONA_ID, "room")
    # 部屋は出ている (会話は終わっている) ので応答は従来のまま
    assert result == "Conversation with 'エア' ended."
    assert persona.current_building_id == "gate"
    assert any("stopped midway" in r.getMessage() for r in caplog.records)


def test_end_conversation_first_step_failure_is_error() -> None:
    persona = _persona(current="room")
    service = _runtime(persona)
    service.occupancy_manager.move_entity = MagicMock(return_value=(False, "定員オーバー"))
    result = service.end_conversation(PERSONA_ID, "room")
    assert result == "Error: Failed to move: 定員オーバー"
    assert persona.current_building_id == "room"


# ---- 意思の主が読む移動: ペルソナの移動ツール --------------------------------


def _tool_manager(persona):
    service = _runtime(persona)
    return SimpleNamespace(
        building_map=service.building_map,
        personas=service.personas,
        _move_persona=service._move_persona,
    )


def test_move_persona_tool_returns_notice_when_stopped_at_entrance() -> None:
    from builtin_data.tools.move_persona import move_persona
    from tools.context import persona_context

    persona = _persona()
    manager = _tool_manager(persona)
    with persona_context(PERSONA_ID, "/tmp", manager=manager):
        result = move_persona("inn")
    # 「宿屋に移動しました」ではなく、入口に着いた案内文そのもの
    assert result == NOTICE_TEXT
    assert persona.current_building_id == "gate"


def test_move_persona_tool_plain_move_wording_unchanged() -> None:
    from builtin_data.tools.move_persona import move_persona
    from tools.context import persona_context

    persona = _persona()
    manager = _tool_manager(persona)
    with persona_context(PERSONA_ID, "/tmp", manager=manager):
        result = move_persona("gate")
    assert result == "エア を 広場 から 霧の谷: 入口 に移動しました。"


def test_move_persona_tool_failure_still_raises() -> None:
    from builtin_data.tools.move_persona import move_persona
    from tools.context import persona_context

    persona = _persona(current="gate")
    service = _runtime(persona, locked=True)
    manager = SimpleNamespace(
        building_map=service.building_map,
        personas=service.personas,
        _move_persona=service._move_persona,
    )
    with persona_context(PERSONA_ID, "/tmp", manager=manager):
        with pytest.raises(RuntimeError, match="鍵がかかっています"):
            move_persona("inn")


# ---- 移動を報せる機構: ワールドエディタの応答 --------------------------------


def _admin(persona):
    service = _runtime(persona)
    admin = AdminService.__new__(AdminService)
    admin.personas = service.personas
    admin.visiting_personas = {}
    admin.building_map = service.building_map
    admin.user_room_id = "room"
    admin._move_persona = service._move_persona
    return admin


def test_editor_move_reports_actual_arrival_and_notice() -> None:
    persona = _persona()
    result = _admin(persona).move_ai_from_editor(PERSONA_ID, "inn")
    assert result == f"Successfully moved 'エア' to '霧の谷: 入口'. {NOTICE_TEXT}"
    assert persona.current_building_id == "gate"


def test_editor_plain_move_wording_unchanged() -> None:
    persona = _persona()
    result = _admin(persona).move_ai_from_editor(PERSONA_ID, "gate")
    assert result == "Successfully moved 'エア' to '霧の谷: 入口'."
