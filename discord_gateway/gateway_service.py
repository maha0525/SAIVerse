from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable

import websockets

from .auth import StaticTokenProvider, TokenProvider
from .client import HandshakeError, WebSocketGatewayClient
from .config import GatewaySettings, get_gateway_settings
from .translator import GatewayCommand, GatewayEvent, GatewayTranslator

logger = logging.getLogger(__name__)


class DiscordGatewayService:
    """Discord Gateway とのWebSocket通信を管理し、キュー経由でアプリにイベントを届ける。"""

    def __init__(
        self,
        settings: GatewaySettings | None = None,
        *,
        translator: GatewayTranslator | None = None,
        token_provider: TokenProvider | None = None,
        client_factory: Callable[
            [GatewaySettings], WebSocketGatewayClient
        ] = WebSocketGatewayClient,
    ):
        self.settings = settings or get_gateway_settings()
        self.translator = translator or GatewayTranslator()
        self.token_provider = token_provider or StaticTokenProvider(settings=self.settings)
        self._client_factory = client_factory

        self._incoming_queue: asyncio.Queue[GatewayEvent] | None = None
        self._outgoing_queue: asyncio.Queue[GatewayCommand] | None = None

        self._stop_event = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def incoming_queue(self) -> asyncio.Queue[GatewayEvent]:
        if self._incoming_queue is None:
            self._incoming_queue = asyncio.Queue(maxsize=self.settings.incoming_queue_maxsize)
        return self._incoming_queue

    @property
    def outgoing_queue(self) -> asyncio.Queue[GatewayCommand]:
        if self._outgoing_queue is None:
            self._outgoing_queue = asyncio.Queue(maxsize=self.settings.outgoing_queue_maxsize)
        return self._outgoing_queue

    async def start(self) -> None:
        if self._worker_task and not self._worker_task.done():
            raise RuntimeError("Gateway service is already running")
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._run(), name="discord-gateway-service")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._worker_task:
            await self._worker_task
        self._worker_task = None

    async def _run(self) -> None:
        backoff = self.settings.reconnect_initial_delay
        while not self._stop_event.is_set():
            try:
                async with self._client_factory(self.settings) as client:
                    token = self.token_provider.get_token()
                    await client.handshake(token)
                    logger.info("Gateway handshake succeeded")
                    await self._connection_loop(client)
                    backoff = self.settings.reconnect_initial_delay
            except HandshakeError as exc:
                logger.error("Gateway handshake failed: %s", exc)
                await self._sleep_with_backoff(backoff)
                backoff = self._next_backoff(backoff)
            except websockets.ConnectionClosed:
                logger.warning("Gateway connection closed, retrying...")
                await self._sleep_with_backoff(backoff)
                backoff = self._next_backoff(backoff)
            except Exception as exc:
                logger.exception("Unexpected gateway error: %s", exc)
                await self._sleep_with_backoff(backoff)
                backoff = self._next_backoff(backoff)

    async def _connection_loop(self, client: WebSocketGatewayClient) -> None:
        receiver_task = asyncio.create_task(self._receiver_loop(client))
        sender_task = asyncio.create_task(self._sender_loop(client))
        stop_task = asyncio.create_task(self._stop_event.wait())

        done, pending = await asyncio.wait(
            {receiver_task, sender_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        for task in done:
            if task is stop_task:
                continue
            if task.cancelled():
                continue
            if exception := task.exception():
                raise exception

    async def _receiver_loop(self, client: WebSocketGatewayClient) -> None:
        while not self._stop_event.is_set():
            message = await client.recv_json()
            event = self.translator.decode_event(message)
            await self.incoming_queue.put(event)

    async def _sender_loop(self, client: WebSocketGatewayClient) -> None:
        while not self._stop_event.is_set():
            try:
                command = await asyncio.wait_for(self.outgoing_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            payload = self.translator.encode_command(command)
            await client.send_json(payload)

    async def _sleep_with_backoff(self, delay: float) -> None:
        jitter = delay * self.settings.reconnect_jitter
        sleep_time = delay + random.uniform(-jitter, jitter)
        sleep_time = max(0.1, sleep_time)
        await asyncio.sleep(sleep_time)

    def _next_backoff(self, current: float) -> float:
        return min(current * 2, self.settings.reconnect_max_delay)
