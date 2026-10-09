"""Discord の人間の発話の受け口 (v0.4 段 1-3 で形を修正) のテスト。

docs/issues/discord_gateway_human_message_signature_mismatch.md:
呼び手 (``GatewayHost.handle_human_message``) は受け口を引数 1 個で呼ぶのに、
受け口 (``GatewayMixin.gateway_handle_human_message``) は 2 引数で定義され、
しかも封筒 (``DiscordMessage``) に無い欄を読んでいた — この経路は呼ばれた瞬間に
TypeError で落ちていた。

ここでは本物のアダプタ → ホスト → 受け口 → 発話の取り込み (RuntimeService の
非ストリーム版) → building_messages の永続化までを通し、Discord のメッセージ ID が
``client_message_id = "discord:<id>"`` として刻まれ、再送が何も起動しないことを
確かめる。ペルソナ・LLM には触れない (dispatcher はフェイク)。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, BuildingMessage
from discord_gateway.mapping import ChannelContext
from discord_gateway.saiverse_adapter import GatewayHost, SAIVerseGatewayAdapter
from discord_gateway.translator import GatewayEvent
from manager.gateway import GatewayMixin
from manager.runtime import RuntimeService


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _runtime(session_factory) -> RuntimeService:
    service = RuntimeService.__new__(RuntimeService)
    service.state = SimpleNamespace(user_id="user_1", user_current_building_id="hall")
    service.SessionLocal = session_factory
    service.personas = {}
    service.occupants = {}
    service.building_map = {}
    service._canonical_building_id = lambda building_id: building_id
    service._build_responding_personas = lambda building_id: [
        SimpleNamespace(persona_id="p1"),
    ]
    service._save_modified_buildings = lambda: None
    service.manager = SimpleNamespace(pulse_dispatcher=MagicMock())
    return service


class _Host(GatewayMixin):
    """SAIVerseManager の代役: 受け口 (GatewayMixin) + 発話の取り込みの窓口。"""

    def __init__(self, runtime: RuntimeService):
        self.runtime = runtime
        self.SessionLocal = runtime.SessionLocal

    def handle_user_input(self, message, metadata=None, *, client_message_id=None):
        return self.runtime.handle_user_input(
            message, metadata=metadata, client_message_id=client_message_id,
        )


def _context() -> ChannelContext:
    return ChannelContext(
        channel_id="555", city_id="city_a", building_id="hall", host_user_id="host",
    )


def _event(message_id) -> GatewayEvent:
    payload = {
        "channel_id": "555",
        "author": {"discord_user_id": "42", "display_name": "まはー"},
        "content": "こんにちは",
    }
    if message_id is not None:
        payload["message_id"] = message_id
    return GatewayEvent(type="discord_message", payload=payload, raw={})


def _rows(session_factory):
    db = session_factory()
    try:
        return [
            (r.building_id, r.client_message_id, r.content)
            for r in db.query(BuildingMessage).all()
        ]
    finally:
        db.close()


def test_Discordの受け口が修正後の形で呼べてclient_message_idが刻まれる(session_factory):
    runtime = _runtime(session_factory)
    adapter = SAIVerseGatewayAdapter(GatewayHost(_Host(runtime)))

    commands = asyncio.run(adapter.handle_human_message(_context(), _event("1234567890")))

    assert commands == []  # 返事は Pulse 側が別経路で届ける (ここでは起動だけ)
    # 発話は一行だけ (以前の二重書き込み _append_gateway_history は外した)
    assert _rows(session_factory) == [("hall", "discord:1234567890", "こんにちは")]
    dispatch = runtime.manager.pulse_dispatcher.dispatch_user_utterance
    dispatch.assert_called_once()
    assert dispatch.call_args.kwargs["event"]["message_id"] == "hall:1"


def test_Discordの同じ発言の再送は何も起動しない(session_factory):
    runtime = _runtime(session_factory)
    adapter = SAIVerseGatewayAdapter(GatewayHost(_Host(runtime)))

    asyncio.run(adapter.handle_human_message(_context(), _event("777")))
    asyncio.run(adapter.handle_human_message(_context(), _event("777")))

    assert _rows(session_factory) == [("hall", "discord:777", "こんにちは")]
    runtime.manager.pulse_dispatcher.dispatch_user_utterance.assert_called_once()


def test_DiscordのメッセージIDが無い発言はERRORで落とされる(session_factory, caplog):
    runtime = _runtime(session_factory)
    adapter = SAIVerseGatewayAdapter(GatewayHost(_Host(runtime)))

    with caplog.at_level("ERROR"):
        commands = asyncio.run(adapter.handle_human_message(_context(), _event(None)))

    assert commands == []
    assert _rows(session_factory) == []
    runtime.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()
    assert any("message_id" in r.message for r in caplog.records)


def test_封筒はDiscordのメッセージIDと表示名を運ぶ():
    adapter = SAIVerseGatewayAdapter(GatewayHost(SimpleNamespace()))
    message = adapter._build_message(_context(), _event("99"), role="human")
    assert message.message_id == "99"
    assert message.author_name == "まはー"
    assert message.context.building_id == "hall"
