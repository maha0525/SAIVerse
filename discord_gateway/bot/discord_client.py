from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import BotSettings
from .router import MessageRouter

logger = logging.getLogger(__name__)


class SAIVerseDiscordClient(commands.Bot):
    """Discord bot client that forwards events to the gateway."""

    def __init__(self, settings: BotSettings, router: MessageRouter):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.router = router

    async def setup_hook(self) -> None:
        logger.info("SAIVerse Discord bot setup complete.")

    async def on_ready(self) -> None:
        logger.info(
            "SAIVerse Discord bot logged in as %s (id=%s)",
            self.user,
            self.user.id if self.user else "unknown",
        )

    async def on_message(self, message: discord.Message) -> None:
        await super().on_message(message)
        await self.router.handle_message(message)
