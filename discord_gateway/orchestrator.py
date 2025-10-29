from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .gateway_service import DiscordGatewayService
from .mapping import ChannelContext, ChannelMapping
from .permissions import InvitationRegistry, PermissionPolicy
from .translator import GatewayCommand, GatewayEvent
from .visitors import VisitorProfile, VisitorRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MemorySyncHandshakeResult:
    accepted: bool
    reason: str | None = None
    commands: Sequence[GatewayCommand] | None = None


@dataclass(slots=True)
class MemorySyncCompletionResult:
    success: bool
    reason: str | None = None
    commands: Sequence[GatewayCommand] | None = None


class GatewayHostAdapter(ABC):
    """SAIVerse 本体と Gateway を橋渡しするためのアダプタ。"""

    @abstractmethod
    async def on_visitor_registered(
        self, visitor: VisitorProfile, context: ChannelContext | None
    ) -> None:
        """訪問者が登録された際に呼び出される。"""

    @abstractmethod
    async def on_visitor_departed(self, visitor: VisitorProfile) -> None:
        """訪問者が退室した際に呼び出される。"""

    @abstractmethod
    async def handle_human_message(
        self, context: ChannelContext, event: GatewayEvent
    ) -> Sequence[GatewayCommand] | None:
        """人間ユーザーからのメッセージを処理する。"""

    @abstractmethod
    async def handle_remote_persona_message(
        self, context: ChannelContext, event: GatewayEvent, visitor: VisitorProfile
    ) -> Sequence[GatewayCommand] | None:
        """訪問者ペルソナのメッセージを処理する。"""

    async def handle_memory_sync_initiate(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncHandshakeResult:
        """記憶同期転送の事前ハンドシェイクを処理する。"""
        return MemorySyncHandshakeResult(accepted=True)

    async def handle_memory_sync_chunk(
        self, visitor: VisitorProfile, payload: dict
    ) -> Sequence[GatewayCommand] | None:
        """記憶同期チャンクを受け取った際の処理。"""
        return None

    async def handle_memory_sync_complete(
        self, visitor: VisitorProfile, payload: dict
    ) -> MemorySyncCompletionResult:
        """記憶同期完了時の処理。"""
        return MemorySyncCompletionResult(success=True)


class DiscordGatewayOrchestrator:
    """GatewayService と SAIVerse 本体の橋渡しを行うハイレベルオーケストレータ。"""

    def __init__(
        self,
        service: DiscordGatewayService,
        *,
        mapping: ChannelMapping,
        visitors: VisitorRegistry | None = None,
        host_adapter: GatewayHostAdapter,
        invitations: InvitationRegistry | None = None,
        permissions: PermissionPolicy | None = None,
    ):
        self.service = service
        self.mapping = mapping
        self.visitors = visitors or VisitorRegistry()
        self.permissions = permissions or PermissionPolicy(mapping, invitations)
        self.host = host_adapter

        self._consumer_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._recent_event_ids: deque[str] = deque()
        self._recent_event_set: set[str] = set()
        self._dedupe_max = 2048

    async def start(self) -> None:
        await self.service.start()
        self._stopping = False
        self._consumer_task = asyncio.create_task(
            self._consume_loop(), name="discord-gateway-consumer"
        )

    async def stop(self) -> None:
        self._stopping = True
        if self._consumer_task:
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        await self.service.stop()

    async def _consume_loop(self) -> None:
        while not self._stopping:
            event = await self.service.incoming_queue.get()
            try:
                await self._handle_event(event)
            except Exception:  # pragma: no cover - defensive logging
                logger.exception("Failed to handle gateway event: %s", event)

    async def _handle_event(self, event: GatewayEvent) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_id = payload.get("event_id")
        if event_id and self._is_duplicate(event_id):
            await self._ack_event(event)
            return

        handler_map = {
            "discord_message": self._handle_discord_message,
            "visitor_state": self._handle_visitor_state,
            "invite_state": self._handle_invite_state,
            "memory_sync_initiate": self._handle_memory_initiate,
            "memory_sync_chunk": self._handle_memory_chunk,
            "memory_sync_complete": self._handle_memory_complete,
            "resync_required": self._handle_resync_required,
            "state_sync_ack": self._handle_state_sync_ack,
        }
        handler = handler_map.get(event.type)
        if handler:
            await handler(event)
        else:
            logger.debug("Ignored gateway event type=%s", event.type)

        await self._ack_event(event)
        if event_id:
            self._remember_event(event_id)

    async def _handle_discord_message(self, event: GatewayEvent) -> None:
        channel_id = event.raw.get("channel_id") or event.payload.get("channel_id")
        if not channel_id:
            logger.warning("Received message without channel_id: %s", event.raw)
            return

        context = self.mapping.get(channel_id)
        if not context:
            logger.debug("Channel %s is not mapped; dropping event.", channel_id)
            return

        author_info = event.payload.get("author") or {}
        discord_user_id = str(author_info.get("discord_user_id", ""))
        visitor = (
            self.visitors.get_by_discord(discord_user_id) if discord_user_id else None
        )

        roles = self._normalize_roles(author_info.get("roles"))
        if not visitor and discord_user_id:
            decision = self.permissions.evaluate(
                context,
                discord_user_id=discord_user_id,
                roles=roles,
            )
            if not decision.allowed:
                logger.info(
                    "Permission denied for user %s on channel %s",
                    discord_user_id,
                    context.channel_id,
                )
                await self._dispatch_commands(
                    [
                        GatewayCommand(
                            type="permission_denied",
                            payload={
                                "channel_id": context.channel_id,
                                "reason": decision.reason or "permission_denied",
                                "discord_user_id": discord_user_id,
                            },
                        )
                    ]
                )
                return

        if visitor:
            commands = await self.host.handle_remote_persona_message(
                context, event, visitor
            )
        else:
            commands = await self.host.handle_human_message(context, event)

        await self._dispatch_commands(commands)

    async def _handle_visitor_state(self, event: GatewayEvent) -> None:
        payload = event.payload
        action = payload.get("action")
        visitor_data = payload.get("visitor") or {}
        profile = VisitorProfile(
            discord_user_id=str(visitor_data["discord_user_id"]),
            persona_id=str(visitor_data["persona_id"]),
            owner_user_id=str(visitor_data["owner_user_id"]),
            current_city_id=str(visitor_data.get("current_city_id", "")),
            current_building_id=str(visitor_data.get("current_building_id", "")),
            metadata=visitor_data.get("metadata", {}),
        )

        if action == "register":
            self.visitors.register(profile)
            context = self.mapping.get(visitor_data.get("channel_id", ""))
            await self.host.on_visitor_registered(profile, context)
        elif action == "update":
            self.visitors.register(profile)
            if context := self.mapping.get(visitor_data.get("channel_id", "")):
                await self.host.on_visitor_registered(profile, context)
        elif action == "relocate":
            self.visitors.update_location(
                profile.persona_id,
                city_id=profile.current_city_id,
                building_id=profile.current_building_id,
            )
        elif action == "remove":
            removed = self.visitors.unregister_by_persona(profile.persona_id)
            if removed:
                await self.host.on_visitor_departed(removed)
        else:
            logger.warning("Unknown visitor action '%s'", action)

    async def _handle_invite_state(self, event: GatewayEvent) -> None:
        payload = event.payload
        action = payload.get("action")
        channel_id = str(payload.get("channel_id", ""))
        discord_user_id_raw = payload.get("discord_user_id")
        if not channel_id:
            logger.warning("Invite event missing identifiers: %s", payload)
            return

        match action:
            case "grant":
                if not discord_user_id_raw:
                    logger.warning("Invite grant missing discord_user_id: %s", payload)
                    return
                self.permissions.register_invite(
                    channel_id, str(discord_user_id_raw)
                )
            case "revoke":
                if not discord_user_id_raw:
                    logger.warning("Invite revoke missing discord_user_id: %s", payload)
                    return
                self.permissions.revoke_invite(channel_id, str(discord_user_id_raw))
            case "clear":
                self.permissions.clear_invites(channel_id)
            case _:
                logger.warning("Unknown invite action '%s'", action)

    async def _handle_resync_required(self, event: GatewayEvent) -> None:
        payload = event.payload or {}
        if hasattr(self.host, "handle_resync_required"):
            try:
                await self.host.handle_resync_required(payload)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Host failed to process resync notification.")
        await self._dispatch_commands(
            [
                GatewayCommand(
                    type="state_sync_request",
                    payload={"mode": "full"},
                )
            ]
        )

    async def _handle_state_sync_ack(self, event: GatewayEvent) -> None:
        logger.info(
            "Gateway state sync acknowledged: %s", event.payload or {"status": "ok"}
        )

    async def _handle_memory_initiate(self, event: GatewayEvent) -> None:
        visitor = self._visitor_from_event(event)
        if not visitor:
            return

        transfer_id = event.payload.get("transfer_id")
        if not transfer_id:
            logger.warning("Memory sync initiate missing transfer_id: %s", event.payload)
            return

        try:
            result = await self.host.handle_memory_sync_initiate(visitor, event.payload)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Host failed during memory sync initiate.")
            result = MemorySyncHandshakeResult(accepted=False, reason="host_error")

        await self._dispatch_commands(getattr(result, "commands", None))

        status_payload = {"transfer_id": transfer_id}
        if getattr(result, "accepted", False):
            status_payload["status"] = "ok"
        else:
            status_payload["status"] = "error"
            reason = getattr(result, "reason", None)
            if reason:
                status_payload["reason"] = reason

        await self.service.outgoing_queue.put(
            GatewayCommand(type="memory_sync_ack", payload=status_payload)
        )

    async def _handle_memory_chunk(self, event: GatewayEvent) -> None:
        visitor = self._visitor_from_event(event)
        if not visitor:
            return
        commands = await self.host.handle_memory_sync_chunk(visitor, event.payload)
        await self._dispatch_commands(commands)

    async def _handle_memory_complete(self, event: GatewayEvent) -> None:
        visitor = self._visitor_from_event(event)
        if not visitor:
            return

        transfer_id = event.payload.get("transfer_id")
        if not transfer_id:
            logger.warning(
                "Memory sync complete missing transfer_id: %s", event.payload
            )
            return

        try:
            result = await self.host.handle_memory_sync_complete(visitor, event.payload)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Host failed to finalize memory sync transfer.")
            result = MemorySyncCompletionResult(success=False, reason="host_error")

        await self._dispatch_commands(getattr(result, "commands", None))

        status_payload = {"transfer_id": transfer_id}
        if getattr(result, "success", False):
            status_payload["status"] = "ok"
        else:
            status_payload["status"] = "error"
            reason = getattr(result, "reason", None)
            if reason:
                status_payload["reason"] = reason

        await self.service.outgoing_queue.put(
            GatewayCommand(type="memory_sync_complete", payload=status_payload)
        )

    def _visitor_from_event(self, event: GatewayEvent) -> VisitorProfile | None:
        visitor_data = event.payload.get("visitor")
        if not visitor_data:
            logger.warning("Memory event missing visitor payload: %s", event.payload)
            return None
        persona_id = str(visitor_data.get("persona_id", ""))
        visitor = self.visitors.get_by_persona(persona_id)
        if not visitor:
            logger.warning("Memory event for unknown persona '%s'", persona_id)
        return visitor

    async def _ack_event(self, event: GatewayEvent) -> None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        event_id = payload.get("event_id")
        if not event_id:
            return
        ack_payload = {"event_id": event_id}
        channel_id = payload.get("channel_id")
        if channel_id is not None:
            ack_payload["channel_id"] = channel_id
        channel_seq = payload.get("channel_seq")
        if channel_seq is not None:
            ack_payload["channel_seq"] = channel_seq
        await self.service.outgoing_queue.put(
            GatewayCommand(type="ack", payload=ack_payload)
        )

    def _remember_event(self, event_id: str) -> None:
        if event_id in self._recent_event_set:
            return
        if len(self._recent_event_ids) >= self._dedupe_max:
            expired = self._recent_event_ids.popleft()
            self._recent_event_set.discard(expired)
        self._recent_event_ids.append(event_id)
        self._recent_event_set.add(event_id)

    def _is_duplicate(self, event_id: str) -> bool:
        return event_id in self._recent_event_set

    async def _dispatch_commands(
        self, commands: Iterable[GatewayCommand] | None
    ) -> None:
        if not commands:
            return
        for command in commands:
            await self.service.outgoing_queue.put(command)

    @staticmethod
    def _normalize_roles(raw_roles) -> list[str]:
        tokens: list[str] = []

        def collect(value) -> None:
            if value is None:
                return
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list | tuple | set):
                for item in value:
                    collect(item)
            else:
                tokens.append(str(value))

        collect(raw_roles)
        return tokens


# Lazy import to avoid circular dependency when stopping the orchestrator
import contextlib  # noqa: E402  # isort:skip
