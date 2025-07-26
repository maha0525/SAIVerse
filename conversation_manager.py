import threading
import time
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # SAIVerseManagerとRouterの循環参照を避けるための型チェック用インポート
    from saiverse_manager import SAIVerseManager

class ConversationManager:
    """
    特定のBuilding内の自律的な会話を管理するクラス。
    Buildingごとに1インスタンスが生成され、バックグラウンドで動作する。
    """
    def __init__(self, building_id: str, saiverse_manager: 'SAIVerseManager', interval: int = 10):
        """
        コンストラクタ
        :param building_id: 担当するBuildingのID
        :param saiverse_manager: 全体を管理するSAIVerseManagerのインスタンス
        :param interval: 発話間隔（秒）
        """
        self.building_id = building_id
        self.saiverse_manager = saiverse_manager
        self.interval = interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_speaker_index = 0
        logging.info(f"[ConvManager] Initialized for Building: {self.building_id}")

    def start(self):
        """会話ループをバックグラウンドスレッドで開始する。"""
        if self._thread and self._thread.is_alive():
            logging.warning(f"[ConvManager] Thread for {self.building_id} is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._conversation_loop, daemon=True)
        self._thread.start()
        logging.info(f"[ConvManager] Started background thread for Building: {self.building_id}")

    def stop(self):
        """会話ループを安全に停止する。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logging.info(f"[ConvManager] Stopped background thread for Building: {self.building_id}")

    def _conversation_loop(self):
        """
        会話を進行させるメインループ。
        指定されたintervalごとに次の発話者を決定し、発話を促す。
        """
        while not self._stop_event.is_set():
            # interval秒待機。waitを使うと停止イベントに即時反応できる。
            self._stop_event.wait(self.interval)
            if self._stop_event.is_set():
                break

            try:
                self._trigger_next_speaker()
            except Exception as e:
                logging.error(f"[ConvManager] Error in conversation loop for {self.building_id}: {e}", exc_info=True)

    def _trigger_next_speaker(self):
        """ラウンドロビンで次の発話者を決定し、発話をトリガーする。"""
        occupants = self.saiverse_manager.occupants.get(self.building_id, [])

        # 誰もいなければ何もしない
        if not occupants:
            return

        # インデックスが範囲外ならリセット
        if self._current_speaker_index >= len(occupants):
            self._current_speaker_index = 0
        
        speaker_id = occupants[self._current_speaker_index]
        # 居住者と訪問者を区別せず、統一されたリストからペルソナを取得
        speaker_persona = self.saiverse_manager.all_personas.get(speaker_id)
        
        if not speaker_persona:
            # ペルソナが見つからない場合（例：移動直後など）はスキップ
            logging.warning(f"[ConvManager] Persona with ID '{speaker_id}' not found in all_personas. Skipping turn.")
            self._current_speaker_index = (self._current_speaker_index + 1) % len(occupants)
            return
        
        # 派遣中のペルソナは、派遣元のCityでは自律会話を行わない
        if getattr(speaker_persona, 'is_proxy', False) is False and getattr(speaker_persona, 'is_dispatched', False) is True:
            logging.debug(f"[ConvManager] Persona '{speaker_persona.persona_name}' is dispatched. Skipping turn.")
            self._current_speaker_index = (self._current_speaker_index + 1) % len(occupants)
            return

        # PersonaCoreのrun_pulseを呼び出す
        # これにより、ペルソナは自ら状況を判断して発話するかどうかを決める
        logging.info(f"[ConvManager] Triggering pulse for '{speaker_persona.persona_name}' in '{self.building_id}'.")
        replies = speaker_persona.run_pulse(occupants=occupants, user_online=self.saiverse_manager.user_is_online)

        # RemotePersonaProxyからの返信の場合、ここで履歴を保存する
        # (PersonaCoreはrun_pulse内部で履歴を保存するため、二重保存を防ぐ)
        if getattr(speaker_persona, 'is_proxy', False) and replies:
            for say in replies:
                self.saiverse_manager.building_histories.setdefault(self.building_id, []).append({
                    "role": "assistant",
                    "persona_id": speaker_id,
                    "content": say
                })
            # 履歴をファイルに保存
            self.saiverse_manager._save_building_histories()
            logging.info(f"[ConvManager] Proxy '{speaker_persona.persona_name}' spoke in '{self.building_id}'.")

        # 次の発話者のためにインデックスを進める
        self._current_speaker_index = (self._current_speaker_index + 1) % len(occupants)