from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .config import GatewaySettings, get_gateway_settings
from .gateway_service import DiscordGatewayService
from .mapping import ChannelMapping
from .orchestrator import DiscordGatewayOrchestrator
from .runtime import GatewayRuntime
from .saiverse_adapter import GatewayHost, SAIVerseGatewayAdapter
from .translator import GatewayCommand

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GatewayBridge:
    """Convenience wrapper exposing queues and lifecycle helpers."""

    runtime: GatewayRuntime
    service: DiscordGatewayService
    orchestrator: DiscordGatewayOrchestrator
    mapping: ChannelMapping

    @property
    def incoming_queue(self):
        return self.service.incoming_queue

    @property
    def outgoing_queue(self):
        return self.service.outgoing_queue

    def submit_command(self, command: GatewayCommand) -> None:
        async def enqueue() -> None:
            await self.service.outgoing_queue.put(command)

        self.runtime.submit(enqueue())

    def stop(self) -> None:
        self.runtime.stop()


def _gateway_enabled() -> bool:
    return os.getenv("SAIVERSE_GATEWAY_ENABLED", "0").lower() in {"1", "true", "yes"}


def ensure_gateway_runtime(
    manager,
    *,
    settings: GatewaySettings | None = None,
    mapping: ChannelMapping | None = None,
) -> GatewayBridge | None:
    """
    Ensure the Discord gateway runtime is running for the given SAIVerseManager.

    Returns a GatewayBridge when enabled, otherwise None.
    """

    existing_runtime: GatewayRuntime | None = getattr(manager, "gateway_runtime", None)
    existing_mapping: ChannelMapping | None = getattr(manager, "gateway_mapping", None)
    existing_bridge: GatewayBridge | None = getattr(manager, "gateway_bridge", None)

    if existing_runtime and existing_bridge:
        logger.debug("Reusing existing gateway runtime.")
        return existing_bridge

    if existing_runtime and getattr(existing_runtime, "orchestrator", None):
        orchestrator = existing_runtime.orchestrator
        service = orchestrator.service
        bridge = GatewayBridge(
            runtime=existing_runtime,
            service=service,
            orchestrator=orchestrator,
            mapping=existing_mapping or ChannelMapping([]),
        )
        manager.gateway_bridge = bridge
        logger.debug("Recovered gateway bridge from existing runtime.")
        return bridge

    if not _gateway_enabled():
        logger.info("SAIVERSE Gateway is disabled (SAIVERSE_GATEWAY_ENABLED=0).")
        return None

    settings = settings or get_gateway_settings()
    mapping = mapping or ChannelMapping.from_environment()
    service = DiscordGatewayService(settings=settings)
    host = GatewayHost(manager)
    adapter = SAIVerseGatewayAdapter(host)
    orchestrator = DiscordGatewayOrchestrator(service, mapping=mapping, host_adapter=adapter)
    runtime = GatewayRuntime(orchestrator)
    runtime.start()

    bridge = GatewayBridge(
        runtime=runtime, service=service, orchestrator=orchestrator, mapping=mapping
    )

    manager.gateway_runtime = runtime
    manager.gateway_mapping = mapping
    manager.gateway_bridge = bridge

    logger.info(
        "Discord gateway runtime started (ws_url=%s).",
        settings.bot_ws_url,
    )

    return bridge
