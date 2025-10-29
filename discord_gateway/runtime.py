from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable

logger = logging.getLogger(__name__)


class GatewayRuntime:
    """Run a DiscordGatewayOrchestrator on a dedicated background event loop."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():  # pragma: no cover - defensive
            return

        def runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.orchestrator.start())
                loop.run_forever()
            except Exception:  # pragma: no cover - logging only
                logger.exception("Gateway runtime encountered an error.")
            finally:
                try:
                    loop.run_until_complete(self.orchestrator.stop())
                except Exception:
                    logger.debug("Failed to stop orchestrator cleanly.", exc_info=True)
                loop.close()

        self._thread = threading.Thread(target=runner, daemon=True, name="DiscordGatewayRuntime")
        self._thread.start()

    def stop(self) -> None:
        if not self._loop:
            return
        fut = asyncio.run_coroutine_threadsafe(self.orchestrator.stop(), self._loop)
        fut.result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
        self._loop = None
        self._thread = None

    def submit(self, coro: Awaitable):
        if not self._loop:  # pragma: no cover - defensive
            raise RuntimeError("Gateway runtime is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)
