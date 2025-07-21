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

        if len(occupants) < 2:
            return

        if self._current_speaker_index >= len(occupants):
            self.current_speaker_index = 0
        
        speaker_id = occupants[self._current_speaker_index]
        speaker_router = self.saiverse_manager.routers.get(speaker_id)
        
        if not speaker_router:
            self._current_speaker_index = (self._current_speaker_index + 1) % len(occupants)
            return
        
        # Buildingの会話履歴を確認し、AIの発言がまだなければ最初のターンと判断
        building_history = self.saiverse_manager.building_histories.get(self.building_id, [])
        is_first_turn = not any(msg.get("role") == "assistant" for msg in building_history)

        if is_first_turn:
            # 最初のターンなら、ENTRY_PROMPTを使うrun_auto_conversationを呼び出す
            logging.info(f"[ConvManager] Triggering initial turn for '{speaker_router.persona_name}' in '{self.building_id}' using ENTRY_PROMPT.")
            if hasattr(speaker_router, 'run_auto_conversation'):
                # このメソッドはリストを返すが、自律会話では戻り値は不要
                speaker_router.run_auto_conversation(initial=True)
            else:
                logging.warning(f"Router for {speaker_id} is missing 'run_auto_conversation' method.")
        else:
            # 2ターン目以降は、AUTO_PROMPTを使って会話の継続を促す
            building = self.saiverse_manager.building_map.get(self.building_id)
            other_persona_names = [self.saiverse_manager.routers[p_id].persona_name for p_id in occupants if p_id != speaker_id and p_id in self.saiverse_manager.routers]
            
            # AUTO_PROMPTが設定されていればそれを使用し、なければ固定プロンプトにフォールバック
            if building and building.auto_prompt:
                # DBのAUTO_PROMPTで使えるプレースホルダーを増やす
                conversation_prompt = building.auto_prompt.format(
                    persona_name=speaker_router.persona_name,
                    other_persona_names=", ".join(other_persona_names)
                )
            else:
                conversation_prompt = f"あなたは今、「{', '.join(other_persona_names)}」と会話しています。以下の会話履歴に続いて、自然な形で発言してください。"
            
            logging.info(f"[ConvManager] Triggering '{speaker_router.persona_name}' to continue conversation in '{self.building_id}'")
            
            if hasattr(speaker_router, 'trigger_conversation_turn'):
                speaker_router.trigger_conversation_turn(conversation_prompt)
            else:
                logging.warning(f"Router for {speaker_id} is missing 'trigger_conversation_turn' method.")

        self._current_speaker_index = (self._current_speaker_index + 1) % len(occupants)