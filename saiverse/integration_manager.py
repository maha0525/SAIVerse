"""IntegrationManager — polls external services and emits TriggerEvents.

Runs in its own daemon thread, similar to ScheduleManager.
Each registered integration has its own polling interval; the manager
uses a short base tick and checks whether each integration is due.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Dict, List

from saiverse.integrations.base import BaseIntegration

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)


class IntegrationManager:
    """Manages external service polling integrations."""

    def __init__(
        self,
        saiverse_manager: "SAIVerseManager",
        tick_interval: int = 30,
    ):
        """
        Args:
            saiverse_manager: The central SAIVerseManager instance.
            tick_interval: Base tick interval in seconds.  Each tick,
                the manager checks which integrations are due for polling.
        """
        self.manager = saiverse_manager
        self.tick_interval = tick_interval
        self._integrations: List[BaseIntegration] = []
        self._last_poll: Dict[str, float] = {}  # integration.name -> timestamp
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        LOGGER.info(
            "[IntegrationManager] Initialized (tick_interval=%ds)", tick_interval
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, integration: BaseIntegration) -> None:
        """Register an integration to be polled."""
        self._integrations.append(integration)
        LOGGER.info(
            "[IntegrationManager] Registered integration '%s' (poll every %ds)",
            integration.name,
            integration.poll_interval_seconds,
        )

    def start(self) -> None:
        """Start the background polling thread."""
        if self._thread and self._thread.is_alive():
            LOGGER.warning("[IntegrationManager] Thread is already running")
            return

        if not self._integrations:
            LOGGER.info(
                "[IntegrationManager] No integrations registered, not starting thread"
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._main_loop, daemon=True, name="IntegrationManager"
        )
        self._thread.start()
        LOGGER.info("[IntegrationManager] Background polling thread started")

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        LOGGER.info("[IntegrationManager] Stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        """Main polling loop — runs in a daemon thread."""
        LOGGER.info("[IntegrationManager] Polling loop started")
        while not self._stop_event.is_set():
            self._stop_event.wait(self.tick_interval)
            if self._stop_event.is_set():
                break

            for integration in self._integrations:
                try:
                    if self._should_poll(integration):
                        self._poll_integration(integration)
                except Exception:
                    LOGGER.error(
                        "[IntegrationManager] Error polling '%s'",
                        integration.name,
                        exc_info=True,
                    )

        LOGGER.info("[IntegrationManager] Polling loop ended")

    def _should_poll(self, integration: BaseIntegration) -> bool:
        """Check if enough time has elapsed since the last poll."""
        last = self._last_poll.get(integration.name, 0.0)
        return (time.time() - last) >= integration.poll_interval_seconds

    def _is_integration_enabled(self, integration: BaseIntegration) -> bool:
        """Check if the integration is enabled via global settings."""
        state = getattr(self.manager, "state", None)
        if state is None:
            return False

        # Per-integration enable check
        if integration.name == "x_mentions":
            return getattr(state, "x_polling_enabled", False)

        # Future integrations can add checks here
        return True

    def _poll_integration(self, integration: BaseIntegration) -> None:
        """Execute a single poll and emit resulting events."""
        if not self._is_integration_enabled(integration):
            return

        LOGGER.debug("[IntegrationManager] Polling '%s'", integration.name)
        self._last_poll[integration.name] = time.time()

        events = integration.poll(self.manager)
        if not events:
            return

        LOGGER.info(
            "[IntegrationManager] '%s' returned %d events",
            integration.name,
            len(events),
        )

        phenomenon_manager = getattr(self.manager, "phenomenon_manager", None)
        if phenomenon_manager is None:
            LOGGER.warning(
                "[IntegrationManager] No phenomenon_manager on SAIVerseManager, "
                "cannot emit events"
            )
            return

        for event in events:
            try:
                phenomenon_manager.emit(event)
            except Exception:
                LOGGER.error(
                    "[IntegrationManager] Failed to emit event %s", event, exc_info=True
                )
