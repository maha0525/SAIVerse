from __future__ import annotations

import os
import logging
from dataclasses import dataclass
from typing import Optional

from .config import GatewaySettings, get_gateway_settings
from .gateway_service import DiscordGatewayService
from .mapping import ChannelContext, ChannelMapping
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
    settings: Optional[GatewaySettings] = None,
    mapping: Optional[ChannelMapping] = None,
) -> Optional[GatewayBridge]:
    """
    Ensure the Discord gateway runtime is running for the given SAIVerseManager.

    Returns a GatewayBridge when enabled, otherwise None.
    """

    existing_runtime: Optional[GatewayRuntime] = getattr(
        manager, "gateway_runtime", None
    )
    existing_mapping: Optional[ChannelMapping] = getattr(
        manager, "gateway_mapping", None
    )
    existing_bridge: Optional[GatewayBridge] = getattr(
        manager, "gateway_bridge", None
    )

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
        setattr(manager, "gateway_bridge", bridge)
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
    orchestrator = DiscordGatewayOrchestrator(
        service, mapping=mapping, host_adapter=adapter
    )
    runtime = GatewayRuntime(orchestrator)
    runtime.start()

    bridge = GatewayBridge(
        runtime=runtime, service=service, orchestrator=orchestrator, mapping=mapping
    )

    setattr(manager, "gateway_runtime", runtime)
    setattr(manager, "gateway_mapping", mapping)
    setattr(manager, "gateway_bridge", bridge)

    logger.info(
        "Discord gateway runtime started (ws_url=%s).",
        settings.bot_ws_url,
    )

    return bridge
