from __future__ import annotations

import logging
from typing import Any, Protocol

from .security import sanitize_message_content

logger = logging.getLogger(__name__)


class CommandRouter(Protocol):
    """Interface contract for routing outbound commands to Discord."""

    def get_owner_id(self, channel_id: int | str) -> str | None: ...

    async def send_post_message(
        self,
        channel_id: int | str,
        *,
        content: str,
        persona_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class CommandProcessor:
    """Dispatches commands received from local applications to Discord."""

    def __init__(self, *, router: CommandRouter, max_message_length: int) -> None:
        self._router = router
        self._max_message_length = max_message_length

    async def handle(self, client, message: dict[str, Any]) -> bool:
        """Handle a raw command payload. Returns True when processed."""

        command_type = message.get("type")
        payload = message.get("payload") or {}

        if command_type == "post_message":
            await self._handle_post_message(client, payload)
            return True

        return False

    async def _handle_post_message(self, client, payload: dict[str, Any]) -> None:
        channel_id = payload.get("channel_id")
        content = payload.get("content")
        persona_id = payload.get("persona_id")
        metadata = {
            "building_id": payload.get("building_id"),
            "city_id": payload.get("city_id"),
        }

        if not channel_id:
            logger.warning("post_message command missing channel_id: %s", payload)
            return

        if content is None or str(content).strip() == "":
            logger.warning("post_message command missing content for channel %s", channel_id)
            return

        owner_id = self._router.get_owner_id(channel_id)
        session_owner = getattr(getattr(client, "session", None), "discord_user_id", None)

        if owner_id is None:
            logger.warning(
                "post_message command rejected: channel %s is not registered to any owner.",
                channel_id,
            )
            return

        if session_owner != owner_id:
            logger.warning(
                "post_message command rejected: session owner %s attempted to post to channel %s owned by %s.",
                session_owner,
                channel_id,
                owner_id,
            )
            return

        sanitized = sanitize_message_content(str(content), max_length=self._max_message_length)
        if not sanitized:
            logger.warning(
                "post_message command produced empty content after sanitization for channel %s",
                channel_id,
            )
            return

        try:
            await self._router.send_post_message(
                channel_id,
                content=sanitized,
                persona_id=persona_id,
                metadata={k: v for k, v in metadata.items() if v is not None},
            )
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("Failed to deliver post_message command to channel %s", channel_id)
