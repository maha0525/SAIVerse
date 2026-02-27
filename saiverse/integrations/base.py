"""Base class for external service integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

from phenomena.triggers import TriggerEvent

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager


class BaseIntegration(ABC):
    """Abstract base for polling-based external integrations.

    Subclasses implement :meth:`poll` which is called periodically by
    :class:`IntegrationManager`.  Each call should return zero or more
    :class:`TriggerEvent` instances representing newly detected state
    changes.
    """

    name: str = "base"
    poll_interval_seconds: int = 300  # default 5 min

    @abstractmethod
    def poll(self, manager: "SAIVerseManager") -> List[TriggerEvent]:
        """Poll the external service and return new trigger events.

        Args:
            manager: The SAIVerseManager instance for DB/persona access.

        Returns:
            List of TriggerEvent to be emitted via PhenomenonManager.
        """
        ...
