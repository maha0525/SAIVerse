from __future__ import annotations

import logging


def configure_logging(level: str) -> None:
    """Configure standard logging for the bot service."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
