"""W7/柱5 回帰: chat 境界 (分離監査 P1-3)。

- raw /chat/send はサーバ現在地専用 — 別 Building 指定は 409 で拒否
- runtime 層の多層防御 (HTTP を通らない呼び出し元も塞ぐ)
- /chat/utter のコマンド意味論: 入室済みの再送は CAS 衝突にならず、
  発言だけが冪等に載る (「入室成功 → insert 失敗」の自己回復経路)

設計: docs/handoff/2026-07-21_w7_location_occupancy_handoff.md D3
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routes.chat import SendMessageRequest, UtterRequest, send_message, utter_message
from manager.runtime import RuntimeService


# ---- runtime 層 (多層防御) -------------------------------------------------


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


def test_stream_refuses_non_current_building() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard"
    ) as insert:
        chunks = list(
            service.handle_user_input_stream("hello", building_id="other_room")
        )

    # 拒否は NDJSON 契約に従う JSON イベント (生 HTML はフロントの JSON.parse で
    # 破棄されユーザーに見えない — Codex 第五巡 P2)
    import json as _json
    events = [_json.loads(chunk) for chunk in chunks]
    assert any(e.get("error_code") == "not_in_building" for e in events)
    insert.assert_not_called()
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_stream_accepts_current_building() -> None:
    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])
    saved = {"message_id": "room:1", "_was_inserted": True}

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value=saved,
    ):
        list(service.handle_user_input_stream("hello", building_id="room"))

    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_persist_guard_conflict_emits_error_without_dispatch() -> None:
    """永続化 tx 内の現在地検証が競合を検出 (Codex 第六巡 P1): 旧 Building へ
    発言が残らず、Pulse も起動せず、確定現在地つきのエラーが返る。"""
    import json as _json

    persona = SimpleNamespace(persona_id="p1")
    service = _runtime([persona])

    with patch(
        "database.building_messages.insert_building_message_with_location_guard",
        return_value={"_location_conflict": True, "current_building_id": "hall"},
    ):
        chunks = list(
            service.handle_user_input_stream("hello", building_id="room")
        )

    events = [_json.loads(c) for c in chunks if c.strip()]
    conflict = next(
        e for e in events if e.get("error_code") == "not_in_building"
    )
    assert conflict["current_building_id"] == "hall"
    service.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_persist_guard_refuses_when_db_location_moved() -> None:
    """guard 関数の実 DB 検証: 検証と INSERT が同一 tx — DB 現在地が期待と
    違えば何も書かない。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database.building_messages import (
        insert_building_message_with_location_guard,
    )
    from database.models import Base, BuildingMessage, User

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        db.add(User(USERID=1, PASSWORD="x", USERNAME="u", CURRENT_BUILDINGID="room"))
        db.commit()
    finally:
        db.close()

    msg = {"role": "user", "content": "hi", "timestamp": "2026-07-21T00:00:00"}

    # 一致 → 保存される
    saved = insert_building_message_with_location_guard(
        SessionLocal, "room", dict(msg), user_id=1, expected_building_id="room",
    )
    assert saved is not None and saved.get("message_id")

    # 別デバイスの移動が確定した後 → 競合 dict + 何も書かない
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(USERID=1).first()
        user.CURRENT_BUILDINGID = "hall"
        db.commit()
    finally:
        db.close()
    result = insert_building_message_with_location_guard(
        SessionLocal, "room", dict(msg), user_id=1, expected_building_id="room",
    )
    assert result == {"_location_conflict": True, "current_building_id": "hall"}
    db = SessionLocal()
    try:
        count = db.query(BuildingMessage).filter_by(building_id="room").count()
        assert count == 1  # 2 通目は書かれていない
    finally:
        db.close()
    engine.dispose()


# ---- /chat/send (route 層) -------------------------------------------------


def test_send_rejects_non_current_building_with_409() -> None:
    # 境界照合は canonical な manager.state を読む (mirror の
    # manager.user_current_building_id は遅延更新のため使わない — 第三巡 P2)
    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id="room"),
        user_current_building_id="stale_mirror",
    )
    req = SendMessageRequest(message="hi", building_id="other_room")
    with pytest.raises(HTTPException) as excinfo:
        send_message(req, manager)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "not_in_building"
    assert excinfo.value.detail["current_building_id"] == "room"


def test_send_requires_some_building() -> None:
    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id=None),
        user_current_building_id=None,
    )
    req = SendMessageRequest(message="hi")
    with pytest.raises(HTTPException) as excinfo:
        send_message(req, manager)
    assert excinfo.value.status_code == 400


def test_send_recheck_after_attachments_cleans_up_and_409s() -> None:
    """添付処理中に別デバイスが移動した場合、作成済み Item を片付けて 409
    (Codex 第四巡 P2 — 旧 Building に孤立した添付を残さない)。"""
    from api.routes.chat import AttachmentData

    manager = SimpleNamespace(
        state=SimpleNamespace(
            user_current_building_id="room", media_recall_enabled=False,
        ),
        user_current_building_id="room",
        get_building_history=MagicMock(return_value=[]),
        _append_building_history_note=MagicMock(),
        delete_item=MagicMock(),
    )

    def _store_and_move(att, mgr, building_id, **kwargs):
        # 添付処理 (概要生成) の最中に別デバイスの移動が確定した状況
        manager.state.user_current_building_id = "hall"
        return {
            "type": "image", "uri": "saiverse://image/x.png",
            "path": "x.png", "mime_type": "image/png", "item_id": "item-1",
        }

    req = SendMessageRequest(
        message="hi", building_id="room",
        attachments=[AttachmentData(
            filename="x.png", type="image", mime_type="image/png", data="aGk=",
        )],
    )
    with patch(
        "api.routes.chat._store_uploaded_attachment_v2",
        side_effect=_store_and_move,
    ):
        with pytest.raises(HTTPException) as excinfo:
            send_message(req, manager)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "not_in_building"
    assert excinfo.value.detail["current_building_id"] == "hall"
    manager.delete_item.assert_called_once_with("item-1")


def test_cleanup_helper_logs_error_string_results(caplog) -> None:
    """delete_item の "Error: ..." 戻り値 (例外でない失敗契約) を検査して記録する
    (Codex 第七巡 P2)。失敗した Item には撤去補記も書かない。"""
    import logging as _logging
    from api.routes.chat import _cleanup_attachment_items

    manager = SimpleNamespace(
        delete_item=MagicMock(return_value="Error: item is locked"),
        _append_building_history_note=MagicMock(),
    )
    with caplog.at_level(_logging.WARNING):
        _cleanup_attachment_items(manager, [("item-1", "x.png")], "room", "test")
    assert any("cleanup reported failure" in r.message for r in caplog.records)
    manager._append_building_history_note.assert_not_called()


def test_cleanup_helper_appends_withdrawal_note() -> None:
    """撤去成功時は旧 Building へ補記 note を追記する (Codex 第八巡 P1 —
    「User uploaded ...」の host 履歴が削除済み Item を指したまま残らない)。"""
    from api.routes.chat import _cleanup_attachment_items

    manager = SimpleNamespace(
        delete_item=MagicMock(return_value="Item 'x.png' deleted successfully."),
        _append_building_history_note=MagicMock(),
    )
    _cleanup_attachment_items(manager, [("item-1", "x.png")], "room", "test")
    manager.delete_item.assert_called_once_with("item-1")
    bid, note = manager._append_building_history_note.call_args.args
    assert bid == "room"
    assert "x.png" in note and "withdrawn" in note


def test_send_recheck_cleanup_failure_still_409s() -> None:
    """cleanup (delete_item) の例外でも 409 が 500 に化けない —
    関数ローカル import による logging の UnboundLocalError の回帰
    (Codex 第五巡 P2)。"""
    from api.routes.chat import AttachmentData

    manager = SimpleNamespace(
        state=SimpleNamespace(
            user_current_building_id="room", media_recall_enabled=False,
        ),
        user_current_building_id="room",
        get_building_history=MagicMock(return_value=[]),
        _append_building_history_note=MagicMock(),
        delete_item=MagicMock(side_effect=RuntimeError("delete boom")),
    )

    def _store_and_move(att, mgr, building_id, **kwargs):
        manager.state.user_current_building_id = "hall"
        return {
            "type": "image", "uri": "saiverse://image/x.png",
            "path": "x.png", "mime_type": "image/png", "item_id": "item-1",
        }

    req = SendMessageRequest(
        message="hi", building_id="room",
        attachments=[AttachmentData(
            filename="x.png", type="image", mime_type="image/png", data="aGk=",
        )],
    )
    with patch(
        "api.routes.chat._store_uploaded_attachment_v2",
        side_effect=_store_and_move,
    ):
        with pytest.raises(HTTPException) as excinfo:
            send_message(req, manager)
    assert excinfo.value.status_code == 409


def test_send_stream_start_recheck_cleans_up_late_conflict() -> None:
    """route 照合通過後〜generator 開始までの競合窓 (Codex 第五巡 P2):
    generator 内の最終照合が Item を片付けて JSON エラーで返す。"""
    import json as _json
    from api.routes.chat import AttachmentData

    manager = SimpleNamespace(
        state=SimpleNamespace(
            user_current_building_id="room", media_recall_enabled=False,
        ),
        user_current_building_id="room",
        get_building_history=MagicMock(return_value=[]),
        _append_building_history_note=MagicMock(),
        delete_item=MagicMock(),
        handle_user_input_stream=MagicMock(),
    )

    req = SendMessageRequest(
        message="hi", building_id="room",
        attachments=[AttachmentData(
            filename="x.png", type="image", mime_type="image/png", data="aGk=",
        )],
    )
    with patch(
        "api.routes.chat._store_uploaded_attachment_v2",
        return_value={
            "type": "image", "uri": "saiverse://image/x.png",
            "path": "x.png", "mime_type": "image/png", "item_id": "item-1",
        },
    ):
        response = send_message(req, manager)

    # route 照合は通過済み。generator 開始前に別デバイスが移動した状況
    manager.state.user_current_building_id = "hall"
    import asyncio

    async def _collect():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(_collect())
    events = [
        _json.loads(c) for c in chunks
        if isinstance(c, str) and c.strip()
    ]
    assert any(e.get("error_code") == "not_in_building" for e in events)
    manager.delete_item.assert_called_once_with("item-1")
    manager.handle_user_input_stream.assert_not_called()


def test_send_runtime_refusal_triggers_item_cleanup() -> None:
    """runtime 層が not_in_building で拒否した場合も route が Item を片付ける
    (Codex 第六巡 P2)。"""
    import json as _json
    from api.routes.chat import AttachmentData

    refusal_chunk = _json.dumps({
        "type": "error", "error_code": "not_in_building",
        "content": "x", "current_building_id": "hall",
    }, ensure_ascii=False) + "\n"

    manager = SimpleNamespace(
        state=SimpleNamespace(
            user_current_building_id="room", media_recall_enabled=False,
        ),
        user_current_building_id="room",
        get_building_history=MagicMock(return_value=[]),
        _append_building_history_note=MagicMock(),
        delete_item=MagicMock(),
        handle_user_input_stream=MagicMock(return_value=iter([refusal_chunk])),
    )
    req = SendMessageRequest(
        message="hi", building_id="room",
        attachments=[AttachmentData(
            filename="x.png", type="image", mime_type="image/png", data="aGk=",
        )],
    )
    with patch(
        "api.routes.chat._store_uploaded_attachment_v2",
        return_value={
            "type": "image", "uri": "saiverse://image/x.png",
            "path": "x.png", "mime_type": "image/png", "item_id": "item-1",
        },
    ):
        response = send_message(req, manager)

    import asyncio

    async def _collect():
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(_collect())
    assert any("not_in_building" in str(c) for c in chunks)
    manager.delete_item.assert_called_once_with("item-1")


# ---- /chat/utter (コマンド意味論) ------------------------------------------


def _utter_manager(current="room"):
    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id=current),
        move_user=MagicMock(return_value=(True, None)),
    )
    return manager


def test_utter_cas_conflict_on_parallel_device() -> None:
    manager = _utter_manager(current="room")
    req = UtterRequest(
        message="hi", target_building_id="hall",
        expected_from_building_id="library",  # 別デバイスが先に移動した想定
    )
    with pytest.raises(HTTPException) as excinfo:
        utter_message(req, manager)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "cas_conflict"
    manager.move_user.assert_not_called()


def test_utter_server_cas_conflict_becomes_409() -> None:
    """サーバ側 CAS (move_entity の条件付き UPDATE) の競合は 409 で再同期
    (2026-07-21 Codex レビュー P2)。"""
    from saiverse.occupancy_manager import MoveDenialMessage

    manager = _utter_manager(current="room")
    manager.move_user = MagicMock(return_value=(
        False, MoveDenialMessage("移動失敗: 現在地が変わっています。", code="cas_conflict"),
    ))
    req = UtterRequest(
        message="hi", target_building_id="hall",
        expected_from_building_id="room",
    )
    with pytest.raises(HTTPException) as excinfo:
        utter_message(req, manager)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "cas_conflict"


def test_user_move_route_converts_server_cas_conflict_to_409() -> None:
    from api.routes.user import move_user as move_user_route, MoveRequest
    from saiverse.occupancy_manager import MoveDenialMessage

    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id="room"),
        user_current_building_id="room",
        move_user=MagicMock(return_value=(
            False,
            MoveDenialMessage(
                "移動失敗: 現在地が変わっています。", code="cas_conflict",
                current_building_id="hall2",
            ),
        )),
    )
    with pytest.raises(HTTPException) as excinfo:
        move_user_route(MoveRequest(target_building_id="hall"), manager)
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "cas_conflict"
    # 409 は in-memory mirror でなく拒否メッセージが運ぶ DB 確定値を返す
    # (勝者 commit 後・sync 前の窓で mirror は stale — 第三巡 P2)
    assert excinfo.value.detail["current_building_id"] == "hall2"


def test_user_move_route_normal_failure_stays_200_payload() -> None:
    from api.routes.user import move_user as move_user_route, MoveRequest

    manager = SimpleNamespace(
        state=SimpleNamespace(user_current_building_id="room"),
        user_current_building_id="room",
        move_user=MagicMock(return_value=(False, "定員オーバー")),
    )
    result = move_user_route(MoveRequest(target_building_id="hall"), manager)
    assert result["success"] is False


def test_utter_move_failure_is_reported_honestly() -> None:
    manager = _utter_manager(current="room")
    manager.move_user = MagicMock(return_value=(False, "定員オーバー"))
    req = UtterRequest(
        message="hi", target_building_id="hall",
        expected_from_building_id="room",
    )
    with pytest.raises(HTTPException) as excinfo:
        utter_message(req, manager)
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "move_failed"


def test_utter_retry_after_committed_enter_skips_move_and_cas() -> None:
    """「入室成功 → 発言 insert 失敗」後の再送: サーバ現在地は既に target。

    move は不要になり、stale な expected_from でも CAS 衝突にならず発言だけが
    再実行される (client_message_id の冪等キーが二重発言を防ぐ)。
    """
    manager = _utter_manager(current="hall")  # 初回試行で入室は確定済み
    req = UtterRequest(
        message="hi", target_building_id="hall",
        expected_from_building_id="room",  # クライアントの認識は移動前のまま
        client_message_id="cmd-1",
    )
    sentinel = object()
    with patch("api.routes.chat.send_message", return_value=sentinel) as send:
        result = utter_message(req, manager)
    assert result is sentinel
    manager.move_user.assert_not_called()
    sent_req = send.call_args.args[0]
    assert sent_req.building_id == "hall"
    assert sent_req.client_message_id == "cmd-1"


def test_utter_auto_moves_then_delegates() -> None:
    manager = _utter_manager(current="room")

    def _move(target):
        manager.state.user_current_building_id = target
        return True, None

    manager.move_user = MagicMock(side_effect=_move)
    req = UtterRequest(
        message="hi", target_building_id="hall",
        expected_from_building_id="room",
    )
    sentinel = object()
    with patch("api.routes.chat.send_message", return_value=sentinel) as send:
        result = utter_message(req, manager)
    assert result is sentinel
    manager.move_user.assert_called_once_with("hall")
    assert send.call_args.args[0].building_id == "hall"
