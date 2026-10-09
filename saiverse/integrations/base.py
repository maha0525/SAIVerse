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

        **刺激の ID の契約 (v0.4 から義務)**: 返す封筒にはそれぞれ
        ``stimulus_id`` を必ず入れる。値は**供給源の永続 ID から作る**
        (X のメンションなら tweet_id、フィードなら記事の ID など)。同じ出来事を
        次の poll でもう一度返してしまった場合 (カーソルが進まなかった・再試行
        した等) に同じ ID になることが要件で、ペルソナ側はこの ID で二度目を
        照合して弾く。poll のたびに uuid4 を振ると再送が「別の刺激」に数えられ、
        本文のハッシュは同じ文面の別の出来事を黙殺するので、どちらも使わない。
        ``stimulus_id`` が空の封筒は :class:`IntegrationManager` が ERROR を
        出して落とす (fail-closed)。
        """
        ...
