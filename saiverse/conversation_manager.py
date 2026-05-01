import threading
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .saiverse_manager import SAIVerseManager


class ConversationManager:
    """旧自律会話駆動プロトタイプ (run_meta_auto + meta_auto Playbook 経路) 用。

    2026-05-01 の認知モデル移行に伴い無効化。Building 内 AI 自律会話は
    PulseScheduler + track_autonomous 経由で駆動する設計に置き換わったため、
    本クラスのループはすべて no-op となる。クラス自体の削除は saiverse_manager
    などの参照整理を伴うので別タスクで対応予定。
    """

    def __init__(self, building_id: str, saiverse_manager: 'SAIVerseManager', interval: int = 10):
        self.building_id = building_id
        self.saiverse_manager = saiverse_manager
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        logging.info(f"[ConvManager] Initialized (no-op) for Building: {self.building_id}")

    def start(self):
        return

    def stop(self):
        return

    def trigger_next_turn(self):
        return
