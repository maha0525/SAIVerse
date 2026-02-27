"""
phenomena.manager ― PhenomenonManager

トリガーイベントを受信し、条件に一致するルールを検索して、
フェノメノンを非同期で発火させる。
"""
import json
import logging
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from phenomena import PHENOMENON_REGISTRY
from phenomena.triggers import TriggerEvent, TriggerType
from database.models import PhenomenonRule

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

LOGGER = logging.getLogger(__name__)


class PhenomenonManager:
    """
    フェノメノンの発火を管理するマネージャー。

    トリガーを受信し、条件に一致するルールを検索して、フェノメノンを発火させる。
    フェノメノンは非同期実行キューで処理され、メインの処理をブロックしない。
    """

    def __init__(
        self,
        session_factory: Callable[[], "Session"],
        async_execution: bool = True,
        saiverse_manager: Optional[Any] = None,
    ):
        """
        Args:
            session_factory: データベースセッションを生成するファクトリ関数
            async_execution: Trueの場合、フェノメノンを非同期で実行する
            saiverse_manager: SAIVerseManager参照（フェノメノンからPulseController等にアクセス用）
        """
        self.SessionLocal = session_factory
        self.async_execution = async_execution
        self.saiverse_manager = saiverse_manager
        self._execution_queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        LOGGER.info("[PhenomenonManager] Initialized (async_execution=%s)", async_execution)

    def start(self) -> None:
        """バックグラウンドワーカーを開始"""
        if not self.async_execution:
            LOGGER.info("[PhenomenonManager] Synchronous mode, no worker thread needed")
            return

        if self._worker_thread and self._worker_thread.is_alive():
            LOGGER.warning("[PhenomenonManager] Worker thread is already running")
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        LOGGER.info("[PhenomenonManager] Background worker started")

    def stop(self) -> None:
        """バックグラウンドワーカーを停止"""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
            self._worker_thread = None
        LOGGER.info("[PhenomenonManager] Background worker stopped")

    def emit(self, event: TriggerEvent) -> None:
        """トリガーイベントを発火

        Args:
            event: 発火するトリガーイベント
        """
        LOGGER.debug("[PhenomenonManager] Received trigger: %s", event)

        try:
            matching_rules = self._find_matching_rules(event)
            LOGGER.debug("[PhenomenonManager] Found %d matching rules", len(matching_rules))

            for rule in matching_rules:
                args = self._resolve_arguments(rule, event)
                if self.async_execution:
                    self._execution_queue.put((rule.PHENOMENON_NAME, args, rule.RULE_ID))
                else:
                    self._execute_phenomenon(rule.PHENOMENON_NAME, args)
        except Exception as e:
            LOGGER.error("[PhenomenonManager] Error processing trigger: %s", e, exc_info=True)

    def invoke(self, phenomenon_name: str, **kwargs: Any) -> Any:
        """フェノメノンを直接呼び出す（同期実行）

        Args:
            phenomenon_name: 実行するフェノメノンの名前
            **kwargs: フェノメノンに渡す引数

        Returns:
            フェノメノンの戻り値
        """
        return self._execute_phenomenon(phenomenon_name, kwargs)

    def _find_matching_rules(self, event: TriggerEvent) -> List[PhenomenonRule]:
        """イベントに一致するルールを検索"""
        session = self.SessionLocal()
        try:
            # トリガータイプが一致し、有効なルールを取得
            rules = (
                session.query(PhenomenonRule)
                .filter(
                    PhenomenonRule.TRIGGER_TYPE == event.type.value,
                    PhenomenonRule.ENABLED == True,
                )
                .order_by(PhenomenonRule.PRIORITY.desc())
                .all()
            )

            # 条件マッチングでフィルタリング
            return [r for r in rules if self._matches_condition(r, event)]
        finally:
            session.close()

    def _matches_condition(self, rule: PhenomenonRule, event: TriggerEvent) -> bool:
        """ルールの条件がイベントに一致するか確認"""
        if not rule.CONDITION_JSON:
            return True  # 条件がなければ常に一致

        try:
            conditions = json.loads(rule.CONDITION_JSON)
        except json.JSONDecodeError:
            LOGGER.warning("[PhenomenonManager] Invalid JSON in rule %d condition", rule.RULE_ID)
            return False

        for key, expected in conditions.items():
            if expected is None:
                continue  # nullは「どんな値でもOK」を意味
            actual = event.get(key)
            if actual != expected:
                return False
        return True

    def _resolve_arguments(self, rule: PhenomenonRule, event: TriggerEvent) -> Dict[str, Any]:
        """引数マッピングを解決"""
        if not rule.ARGUMENT_MAPPING_JSON:
            return {}

        try:
            mapping = json.loads(rule.ARGUMENT_MAPPING_JSON)
        except json.JSONDecodeError:
            LOGGER.warning("[PhenomenonManager] Invalid JSON in rule %d argument mapping", rule.RULE_ID)
            return {}

        resolved: Dict[str, Any] = {}
        for arg_name, value_spec in mapping.items():
            if isinstance(value_spec, str) and value_spec.startswith("$trigger."):
                # $trigger.persona_id -> event.data["persona_id"]
                field_name = value_spec[9:]  # "$trigger." を除去
                resolved[arg_name] = event.get(field_name)
            else:
                # リテラル値
                resolved[arg_name] = value_spec

        return resolved

    def _execute_phenomenon(self, phenomenon_name: str, args: Dict[str, Any]) -> Any:
        """フェノメノンを実行"""
        impl = PHENOMENON_REGISTRY.get(phenomenon_name)
        if not impl:
            LOGGER.error("[PhenomenonManager] Phenomenon '%s' not found in registry", phenomenon_name)
            return None

        try:
            # Inject _manager reference so phenomena can access PulseController etc.
            if self.saiverse_manager is not None:
                args["_manager"] = self.saiverse_manager

            LOGGER.info("[PhenomenonManager] Executing phenomenon '%s' with args: %s", phenomenon_name, args)
            result = impl(**args)
            LOGGER.info("[PhenomenonManager] Phenomenon '%s' completed successfully", phenomenon_name)
            return result
        except Exception as e:
            LOGGER.error("[PhenomenonManager] Failed to execute phenomenon '%s': %s", phenomenon_name, e, exc_info=True)
            return None

    def _worker_loop(self) -> None:
        """非同期実行ワーカーのメインループ"""
        LOGGER.info("[PhenomenonManager] Worker loop started")
        while not self._stop_event.is_set():
            try:
                phenomenon_name, args, rule_id = self._execution_queue.get(timeout=1.0)
                LOGGER.debug("[PhenomenonManager] Worker processing phenomenon '%s' (rule %d)", phenomenon_name, rule_id)
                self._execute_phenomenon(phenomenon_name, args)
            except queue.Empty:
                continue
            except Exception as e:
                LOGGER.error("[PhenomenonManager] Error in phenomenon worker: %s", e, exc_info=True)
        LOGGER.info("[PhenomenonManager] Worker loop ended")
