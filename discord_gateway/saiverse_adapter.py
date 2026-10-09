from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

from discord_gateway.mapping import ChannelContext
from discord_gateway.orchestrator import (
    GatewayHostAdapter,
    MemorySyncCompletionResult,
    MemorySyncHandshakeResult,
)
from discord_gateway.translator import GatewayCommand, GatewayEvent
from discord_gateway.visitors import VisitorProfile

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscordMessage:
    """Discord の一発言を SAIVerse 本体の受け口 (manager/gateway.py) へ運ぶ封筒。

    受け口は ``context`` (どのチャンネル = どの建物か) をこの封筒から読む —
    別引数では受け取らない。``message_id`` は Discord のメッセージ ID
    (relay bot の ``payload.message_id``) で、人間の発話の再送を止める冪等キー
    (``client_message_id = "discord:<message_id>"``) の元になる。
    """

    context: ChannelContext
    author_discord_id: str
    author_role: str
    content: str
    raw_event: GatewayEvent
    visitor: VisitorProfile | None = None
    message_id: str | None = None
    author_name: str = ""


class SAIVerseGatewayAdapter(GatewayHostAdapter):
    def __init__(self, host: GatewayHost):
        self.host = host

    async def on_visitor_registered(
        self, visitor: VisitorProfile, context: ChannelContext | None
    ) -> None:
        await self.host.handle_visitor_registered(visitor, context)

    async def on_visitor_departed(self, visitor: VisitorProfile) -> None:
        await self.host.handle_visitor_departed(visitor)

    async def handle_human_message(
        self, context: ChannelContext, event: GatewayEvent
    ) -> Sequence[GatewayCommand] | None:
        message = self._build_message(context, event, role="human")
        return await self.host.handle_human_message(message)

    async def handle_remote_persona_message(
        self, context: ChannelContext, event: GatewayEvent, visitor: VisitorProfile
    ) -> Sequence[GatewayCommand] | None:
        message = self._build_message(context, event, role="persona_remote", visitor=visitor)
        return await self.host.handle_remote_persona_message(message)

    async def handle_memory_sync_initiate(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncHandshakeResult:
        return await self.host.handle_memory_sync_initiate(visitor, payload)

    async def handle_memory_sync_chunk(
        self, visitor: VisitorProfile, payload: dict
    ) -> Sequence[GatewayCommand] | None:
        return await self.host.handle_memory_sync_chunk(visitor, payload)

    async def handle_memory_sync_complete(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncCompletionResult:
        return await self.host.handle_memory_sync_complete(visitor, payload)

    def _build_message(
        self,
        context: ChannelContext,
        event: GatewayEvent,
        *,
        role: str,
        visitor: VisitorProfile | None = None,
    ) -> DiscordMessage:
        author = event.payload.get("author") or {}
        content = event.payload.get("content", "")
        raw_message_id = event.payload.get("message_id")
        return DiscordMessage(
            context=context,
            author_discord_id=str(author.get("discord_user_id", "")),
            author_role=role,
            content=content,
            raw_event=event,
            visitor=visitor,
            message_id=str(raw_message_id) if raw_message_id else None,
            author_name=str(author.get("display_name") or ""),
        )


class GatewayHost:
    def __init__(self, manager: SAIVerseManager):
        self.manager = manager

    async def handle_visitor_registered(
        self, visitor: VisitorProfile, context: ChannelContext | None
    ) -> None:
        if not context:
            logger.debug("Visitor registered without channel context: %s", visitor)
            return
        await self._run_blocking(self.manager.gateway_on_visitor_registered, visitor, context)

    async def handle_visitor_departed(self, visitor: VisitorProfile) -> None:
        await self._run_blocking(self.manager.gateway_on_visitor_departed, visitor)

    async def handle_human_message(
        self, message: DiscordMessage
    ) -> Sequence[GatewayCommand] | None:
        return await self._run_blocking(self.manager.gateway_handle_human_message, message)

    async def handle_remote_persona_message(
        self, message: DiscordMessage
    ) -> Sequence[GatewayCommand] | None:
        return await self._run_blocking(self.manager.gateway_handle_remote_persona_message, message)

    async def handle_memory_sync_initiate(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncHandshakeResult:
        return await self._run_blocking(
            self.manager.gateway_handle_memory_sync_initiate, visitor, payload
        )

    async def handle_memory_sync_chunk(
        self, visitor: VisitorProfile, payload: dict
    ) -> Sequence[GatewayCommand] | None:
        return await self._run_blocking(
            self.manager.gateway_handle_memory_sync_chunk, visitor, payload
        )

    async def handle_memory_sync_complete(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncCompletionResult:
        return await self._run_blocking(
            self.manager.gateway_handle_memory_sync_complete, visitor, payload
        )

    async def handle_resync_required(self, payload: dict) -> None:
        handler = getattr(self.manager, "gateway_handle_resync_required", None)
        if not handler:
            return
        await self._run_blocking(handler, payload)

    async def _run_blocking(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: func(*args))
