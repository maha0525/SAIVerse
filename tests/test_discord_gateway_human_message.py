"""Discord の人間の発話の受け口のテスト。

docs/issues/discord_gateway_human_message_signature_mismatch.md:
v0.4 段 1-3 で受け口の形 (引数 1 個、封筒の欄) は呼び手に合わせて直したが、
本体側の取り込み口 ``handle_user_input`` は発話を Web ユーザーの現在地に
**オーナー本人の発話として**流し、Discord チャンネルの建物も送信者の素性も
運べない (2026-10-10 Codex 敵対レビュー)。建物と送信者を運ぶ取り込み経路が
できるまで、受け口は fail-closed で閉じる — 保存しない・起動しない・応答
コマンドを返さない・``handle_user_input`` を呼ばない。

ここでは本物のアダプタ → ホスト → 受け口を通し、取り込み口 (RuntimeService の
非ストリーム版) と building_messages まで何も届かないことを確かめる。
ペルソナ・LLM には触れない (dispatcher はフェイク)。
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
        self.handle_user_input_calls = []

    def handle_user_input(self, message, metadata=None, *, client_message_id=None):
        self.handle_user_input_calls.append((message, client_message_id))
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


def test_Discordの人間の発話は取り込み口へ流さずERRORで拒否する(session_factory, caplog):
    runtime = _runtime(session_factory)
    host = _Host(runtime)
    adapter = SAIVerseGatewayAdapter(GatewayHost(host))

    with caplog.at_level("ERROR"):
        commands = asyncio.run(
            adapter.handle_human_message(_context(), _event("1234567890"))
        )

    # 応答コマンドを出さない (チャンネルに何も書き戻さない)
    assert commands == []
    # Web 現在地・オーナー名義で処理する取り込み口を呼ばない
    assert host.handle_user_input_calls == []
    # 保存も起動もしない
    assert _rows(session_factory) == []
    runtime.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()
    assert any(
        "fail-closed" in r.getMessage()
        and "discord_gateway_human_message_signature_mismatch.md" in r.getMessage()
        for r in caplog.records
    )


def test_Discordの同じ発言の再送も拒否され何も起動しない(session_factory):
    runtime = _runtime(session_factory)
    host = _Host(runtime)
    adapter = SAIVerseGatewayAdapter(GatewayHost(host))

    asyncio.run(adapter.handle_human_message(_context(), _event("777")))
    asyncio.run(adapter.handle_human_message(_context(), _event("777")))

    assert host.handle_user_input_calls == []
    assert _rows(session_factory) == []
    runtime.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()


def test_DiscordのメッセージIDが無い発言はERRORで落とされる(session_factory, caplog):
    runtime = _runtime(session_factory)
    host = _Host(runtime)
    adapter = SAIVerseGatewayAdapter(GatewayHost(host))

    with caplog.at_level("ERROR"):
        commands = asyncio.run(adapter.handle_human_message(_context(), _event(None)))

    assert commands == []
    assert host.handle_user_input_calls == []
    assert _rows(session_factory) == []
    runtime.manager.pulse_dispatcher.dispatch_user_utterance.assert_not_called()
    assert any("message_id" in r.message for r in caplog.records)


def test_封筒はDiscordのメッセージIDと表示名を運ぶ():
    adapter = SAIVerseGatewayAdapter(GatewayHost(SimpleNamespace()))
    message = adapter._build_message(_context(), _event("99"), role="human")
    assert message.message_id == "99"
    assert message.author_name == "まはー"
    assert message.context.building_id == "hall"
