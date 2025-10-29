from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

from .config import BotSettings
from .connection_manager import ConnectionManager
from .database import BotDatabase
from .security import sanitize_message_content

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MessageRouter:
    """Routes Discord events to the appropriate local application."""

    database: BotDatabase
    connections: ConnectionManager
    settings: BotSettings

    async def handle_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        owner_id = self.get_owner_id(message.channel.id)
        if not owner_id:
            logger.debug(
                "No city binding found for channel_id=%s. Ignoring message.",
                message.channel.id,
            )
            return

        roles_payload = self._extract_role_tokens(message.author)
        author_payload = {
            "discord_user_id": str(message.author.id),
            "display_name": message.author.display_name,
            "roles": roles_payload,
        }

        payload = {
            "type": "discord_message",
            "payload": {
                "channel_id": str(message.channel.id),
                "guild_id": str(message.guild.id) if message.guild else None,
                "author": author_payload,
                "content": sanitize_message_content(
                    message.content, max_length=self.settings.max_message_length
                ),
                "message_id": str(message.id),
                "created_at": message.created_at.isoformat(),
            },
        }
        dispatched = await self.connections.send_to_owner(owner_id, payload)
        if dispatched:
            logger.debug(
                "Forwarded message_id=%s to owner_discord_id=%s",
                message.id,
                owner_id,
            )

    def get_owner_id(self, channel_id: int | str) -> str | None:
        return self.database.find_city_owner(channel_id)

    async def emit_invite_event(
        self,
        *,
        owner_discord_id: str,
        channel_id: int | str,
        action: str,
        target_discord_id: str | None = None,
    ) -> bool:
        payload: dict[str, object] = {
            "type": "invite_state",
            "payload": {
                "action": action,
                "channel_id": str(channel_id),
            },
        }
        if target_discord_id is not None:
            payload["payload"]["discord_user_id"] = str(target_discord_id)
        return await self.connections.send_to_owner(owner_discord_id, payload)

    @staticmethod
    def _extract_role_tokens(member: discord.abc.User | discord.Member) -> dict:
        role_ids: set[str] = set()
        role_names: set[str] = set()

        if isinstance(member, discord.Member):
            for role in member.roles:
                if getattr(role, "is_default", lambda: False)():
                    continue
                if getattr(role, "name", None) == "@everyone":
                    continue
                role_ids.add(str(role.id))
                if role.name:
                    role_names.add(role.name)

        return {
            "ids": sorted(role_ids),
            "names": sorted(role_names),
        }
