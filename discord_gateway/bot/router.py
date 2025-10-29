from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from .config import BotSettings
from .connection_manager import ConnectionManager
from .database import BotDatabase
from .security import sanitize_message_content

if TYPE_CHECKING:  # pragma: no cover
    from .discord_client import SAIVerseDiscordClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MessageRouter:
    """Routes Discord events to the appropriate local application."""

    database: BotDatabase
    connections: ConnectionManager
    settings: BotSettings
    discord_client: SAIVerseDiscordClient | None = field(default=None, repr=False)

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

    def attach_discord_client(self, client: SAIVerseDiscordClient) -> None:
        self.discord_client = client

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

    async def send_post_message(
        self,
        channel_id: int | str,
        *,
        content: str,
        persona_id: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        if not self.discord_client:
            raise RuntimeError("Discord client is not attached to MessageRouter.")

        channel_id_int = int(channel_id)
        channel = self.discord_client.get_channel(channel_id_int)
        if channel is None:
            try:
                channel = await self.discord_client.fetch_channel(channel_id_int)
            except discord.NotFound:
                logger.warning(
                    "Channel %s not found when attempting to post message.",
                    channel_id,
                )
                return
            except discord.Forbidden:
                logger.warning(
                    "Missing permissions to access channel %s when posting message.",
                    channel_id,
                )
                return
            except discord.HTTPException as exc:
                logger.warning("HTTP error while fetching channel %s: %s", channel_id, exc)
                return

        if not hasattr(channel, "send"):
            logger.warning("Channel %s does not support sending messages.", channel_id)
            return

        try:
            await channel.send(content)
        except discord.Forbidden:
            logger.warning("Forbidden to send message to channel %s.", channel_id)
        except discord.HTTPException as exc:
            logger.warning("Failed to send message to channel %s: %s", channel_id, exc)
