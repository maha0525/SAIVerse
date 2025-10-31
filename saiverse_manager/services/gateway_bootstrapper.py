"""Helper for wiring Discord gateway integration based on environment variables."""
from __future__ import annotations

import os
from typing import Callable


class GatewayBootstrapper:
    """Determine whether the Discord gateway should be initialised."""

    def __init__(self, initializer: Callable[[], None], env: dict[str, str] | None = None) -> None:
        self._initializer = initializer
        self._env = env if env is not None else os.environ

    def is_enabled(self) -> bool:
        value = self._env.get("SAIVERSE_GATEWAY_ENABLED", "0")
        return value.lower() in {"1", "true", "yes"}

    def start_if_enabled(self) -> bool:
        """Invoke the initializer if the gateway integration is enabled."""
        if not self.is_enabled():
            return False
        self._initializer()
        return True
