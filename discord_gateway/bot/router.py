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

        owner_id = self.database.find_city_owner(message.channel.id)
        if not owner_id:
            logger.debug(
                "No city binding found for channel_id=%s. Ignoring message.",
                message.channel.id,
            )
            return

        payload = {
            "type": "discord_message",
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "content": sanitize_message_content(
                message.content, max_length=self.settings.max_message_length
            ),
            "message_id": str(message.id),
            "created_at": message.created_at.isoformat(),
        }
        dispatched = await self.connections.send_to_owner(owner_id, payload)
        if dispatched:
            logger.debug(
                "Forwarded message_id=%s to owner_discord_id=%s",
                message.id,
                owner_id,
            )
