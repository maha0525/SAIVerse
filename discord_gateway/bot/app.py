from __future__ import annotations

import asyncio
import logging
import signal

from .command_processor import CommandProcessor
from .config import BotSettings, get_settings
from .connection_manager import ConnectionManager
from .database import BotDatabase
from .discord_client import SAIVerseDiscordClient
from .logging import configure_logging
from .router import MessageRouter
from .ws_server import GatewayWebSocketServer

logger = logging.getLogger(__name__)


class BotApplication:
    """High level orchestrator for the SAIVerse Discord bot service."""

    def __init__(self, settings: BotSettings | None = None):
        self.settings = settings or get_settings()
        configure_logging(self.settings.log_level)
        self.database = BotDatabase(self.settings.database_url)
        self.database.migrate()
        self._is_shutting_down = False

        self.connection_manager = ConnectionManager(self.settings, self.database)
        self.router = MessageRouter(self.database, self.connection_manager, self.settings)
        self.command_processor = CommandProcessor(
            router=self.router,
            max_message_length=self.settings.max_message_length,
        )
        self.websocket_server = GatewayWebSocketServer(
            self.settings,
            self.connection_manager,
            command_processor=self.command_processor,
        )
        self.discord_client = SAIVerseDiscordClient(self.settings, self.router)

    async def run(self) -> None:
        """Start WebSocket server and Discord client concurrently."""

        await self.websocket_server.start()
        self._install_signal_handlers()

        try:
            await self.discord_client.start(self.settings.discord_bot_token)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        self._is_shutting_down = True
        logger.info("Shutting down bot application.")
        await self.websocket_server.stop()
        if self.discord_client.is_closed():
            return
        await self.discord_client.close()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(self._stop_from_signal(s))
                )
            except NotImplementedError:
                # Windows event loop does not support signal handlers in Proactor loop.
                logger.debug("Signal handler installation skipped for %s", sig)

    async def _stop_from_signal(self, sig: signal.Signals) -> None:
        logger.info("Received signal %s, shutting down.", sig.name)
        await self.shutdown()


def main() -> None:
    app = BotApplication()
    asyncio.run(app.run())
