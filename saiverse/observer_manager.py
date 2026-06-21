"""Observer Manager — Fixture/Observer の登録・定期実行・push 受信・通知を管理する。

Observer は Building に固定設置された Fixture の一種で、定期的にツール/Playbook を
実行 (pull 型) するか、外部アプリから HTTP push でデータを受信 (push 型) する。
観測値は observer_metrics テーブルに時系列蓄積し、最新値を fixture.STATE_JSON に
キャッシュする。閾値超過 / 大変動を検知して Building 内に通知する。

詳細: docs/intent/observer.md
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy import desc
from sqlalchemy.orm import Session

from database.models import Fixture, ObserverConfig, ObserverMetric

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)


class ObserverManager:
    """Observer のライフサイクルと実行を管理する。"""

    def __init__(self, manager: "SAIVerseManager") -> None:
        self.manager = manager

    # ------------------------------------------------------------------
    # Fixture CRUD
    # ------------------------------------------------------------------

    def create_fixture(
        self,
        fixture_id: str,
        building_id: str,
        name: str,
        *,
        fixture_type: str = "object",
        description: str = "",
        state_json: Optional[str] = None,
        file_path: Optional[str] = None,
        creator_id: Optional[str] = None,
        source_context: Optional[str] = None,
    ) -> Fixture:
        db: Session = self.manager.SessionLocal()
        try:
            fixture = Fixture(
                FIXTURE_ID=fixture_id,
                BUILDING_ID=building_id,
                NAME=name,
                TYPE=fixture_type,
                DESCRIPTION=description,
                STATE_JSON=state_json,
                FILE_PATH=file_path,
                CREATOR_ID=creator_id,
                SOURCE_CONTEXT=source_context,
            )
            db.merge(fixture)
            db.commit()
            LOGGER.info("[observer] fixture created: %s (%s) in %s", name, fixture_id, building_id)
            return fixture
        finally:
            db.close()

    def get_fixture(self, fixture_id: str) -> Optional[Fixture]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(Fixture).filter(Fixture.FIXTURE_ID == fixture_id).first()
        finally:
            db.close()

    def get_building_fixtures(self, building_id: str) -> List[Fixture]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(Fixture).filter(Fixture.BUILDING_ID == building_id).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Observer CRUD
    # ------------------------------------------------------------------

    def create_observer(
        self,
        observer_id: str,
        fixture_id: str,
        exec_kind: str,
        *,
        exec_target: Optional[str] = None,
        exec_args_json: Optional[str] = None,
        interval_sec: Optional[int] = None,
        metric_keys_json: Optional[str] = None,
        notify_rules_json: Optional[str] = None,
        enabled: bool = True,
    ) -> ObserverConfig:
        db: Session = self.manager.SessionLocal()
        try:
            config = ObserverConfig(
                OBSERVER_ID=observer_id,
                FIXTURE_ID=fixture_id,
                ENABLED=enabled,
                EXEC_KIND=exec_kind,
                EXEC_TARGET=exec_target,
                EXEC_ARGS_JSON=exec_args_json,
                INTERVAL_SEC=interval_sec,
                METRIC_KEYS_JSON=metric_keys_json,
                NOTIFY_RULES_JSON=notify_rules_json,
            )
            db.merge(config)
            db.commit()
            LOGGER.info("[observer] config created: %s (kind=%s)", observer_id, exec_kind)
            return config
        finally:
            db.close()

    def get_observer(self, observer_id: str) -> Optional[ObserverConfig]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
        finally:
            db.close()

    def get_fixture_observers(self, fixture_id: str) -> List[ObserverConfig]:
        db: Session = self.manager.SessionLocal()
        try:
            return db.query(ObserverConfig).filter(
                ObserverConfig.FIXTURE_ID == fixture_id
            ).all()
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Metrics — Push / Record
    # ------------------------------------------------------------------

    def record_metrics(
        self,
        observer_id: str,
        metrics: Dict[str, Dict[str, Any]],
        recorded_at: Optional[datetime] = None,
    ) -> List[ObserverMetric]:
        """observer_metrics にデータを記録し、STATE_JSON を更新し、閾値判定を行う。

        Args:
            observer_id: Observer の ID
            metrics: {metric_name: {"value_num": float|None, "value_text": str|None}}
            recorded_at: 記録時刻 (省略時は UTC now)

        Returns:
            記録された ObserverMetric のリスト
        """
        if recorded_at is None:
            recorded_at = datetime.now(timezone.utc)

        db: Session = self.manager.SessionLocal()
        try:
            config = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
            if not config:
                LOGGER.warning("[observer] record_metrics: unknown observer %s", observer_id)
                return []
            if not config.ENABLED:
                LOGGER.debug("[observer] record_metrics: observer %s is disabled", observer_id)
                return []

            recorded: List[ObserverMetric] = []
            for metric_name, values in metrics.items():
                metric = ObserverMetric(
                    OBSERVER_ID=observer_id,
                    METRIC_NAME=metric_name,
                    VALUE_NUM=values.get("value_num"),
                    VALUE_TEXT=values.get("value_text"),
                    RECORDED_AT=recorded_at,
                )
                db.add(metric)
                recorded.append(metric)

            # STATE_JSON の更新 (最新値キャッシュ)
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == config.FIXTURE_ID
            ).first()
            if fixture:
                state = json.loads(fixture.STATE_JSON) if fixture.STATE_JSON else {}
                for metric_name, values in metrics.items():
                    state[metric_name] = {
                        "value_num": values.get("value_num"),
                        "value_text": values.get("value_text"),
                        "recorded_at": recorded_at.isoformat(),
                    }
                fixture.STATE_JSON = json.dumps(state, ensure_ascii=False)

            db.commit()
            LOGGER.debug(
                "[observer] recorded %d metrics for %s", len(recorded), observer_id
            )

            # 閾値判定 (commit 後に実行)
            self._evaluate_notify_rules(config, metrics, recorded_at)

            return recorded
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Metrics — Query
    # ------------------------------------------------------------------

    def get_latest_metrics(self, observer_id: str) -> Dict[str, Dict[str, Any]]:
        """Observer の最新メトリクスを取得する (STATE_JSON キャッシュから)。"""
        db: Session = self.manager.SessionLocal()
        try:
            config = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
            if not config:
                return {}
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == config.FIXTURE_ID
            ).first()
            if not fixture or not fixture.STATE_JSON:
                return {}
            return json.loads(fixture.STATE_JSON)
        finally:
            db.close()

    def get_metric_history(
        self,
        observer_id: str,
        metric_name: str,
        *,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """observer_metrics から指定メトリクスの履歴を取得する。"""
        db: Session = self.manager.SessionLocal()
        try:
            rows = (
                db.query(ObserverMetric)
                .filter(
                    ObserverMetric.OBSERVER_ID == observer_id,
                    ObserverMetric.METRIC_NAME == metric_name,
                )
                .order_by(desc(ObserverMetric.RECORDED_AT))
                .limit(limit)
                .all()
            )
            return [
                {
                    "value_num": r.VALUE_NUM,
                    "value_text": r.VALUE_TEXT,
                    "recorded_at": r.RECORDED_AT.isoformat() if r.RECORDED_AT else None,
                }
                for r in rows
            ]
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Pull 型 — EventScheduler 連携
    # ------------------------------------------------------------------

    def start_pull_observers(self) -> None:
        """起動時に ENABLED な pull 型 Observer を EventScheduler に登録する。"""
        db: Session = self.manager.SessionLocal()
        try:
            configs = db.query(ObserverConfig).filter(
                ObserverConfig.ENABLED == True,  # noqa: E712
                ObserverConfig.EXEC_KIND.in_(["tool", "playbook"]),
            ).all()
            for config in configs:
                self._schedule_pull(config)
            if configs:
                LOGGER.info("[observer] started %d pull observers", len(configs))
        finally:
            db.close()

    def _schedule_pull(self, config: ObserverConfig) -> None:
        """単一の pull 型 Observer を EventScheduler に登録する。"""
        if not config.INTERVAL_SEC or config.INTERVAL_SEC <= 0:
            LOGGER.warning(
                "[observer] skip pull observer %s: invalid interval", config.OBSERVER_ID
            )
            return

        scheduler = self.manager.event_scheduler
        key = f"observer:{config.OBSERVER_ID}"

        def callback() -> None:
            self._execute_pull(config.OBSERVER_ID)

        scheduler.schedule_periodic(
            interval_seconds=config.INTERVAL_SEC,
            callback=callback,
            key=key,
            first_fire_immediate=False,
        )
        LOGGER.debug(
            "[observer] scheduled pull: %s every %ds", config.OBSERVER_ID, config.INTERVAL_SEC
        )

    def _execute_pull(self, observer_id: str) -> None:
        """pull 型 Observer のツール/Playbook を実行し、結果を record_metrics に流す。"""
        db: Session = self.manager.SessionLocal()
        try:
            config = db.query(ObserverConfig).filter(
                ObserverConfig.OBSERVER_ID == observer_id
            ).first()
            if not config or not config.ENABLED:
                return

            if config.EXEC_KIND == "tool":
                self._execute_tool(config)
            elif config.EXEC_KIND == "playbook":
                LOGGER.debug("[observer] playbook execution not yet implemented for %s", observer_id)
        finally:
            db.close()

    def _execute_tool(self, config: ObserverConfig) -> None:
        """TOOL_REGISTRY から指定ツールを実行し、結果を metrics に記録する。"""
        from tools import TOOL_REGISTRY

        tool_name = config.EXEC_TARGET
        if not tool_name or tool_name not in TOOL_REGISTRY:
            LOGGER.warning("[observer] tool not found: %s", tool_name)
            return

        tool_entry = TOOL_REGISTRY[tool_name]
        tool_func = tool_entry.get("function")
        if not tool_func:
            LOGGER.warning("[observer] tool has no function: %s", tool_name)
            return

        args = json.loads(config.EXEC_ARGS_JSON) if config.EXEC_ARGS_JSON else {}
        try:
            result = tool_func(**args)
        except Exception:
            LOGGER.exception("[observer] tool execution failed: %s", tool_name)
            return

        metrics = self._parse_tool_result(config, result)
        if metrics:
            self.record_metrics(config.OBSERVER_ID, metrics)

    def _parse_tool_result(
        self, config: ObserverConfig, result: Any
    ) -> Dict[str, Dict[str, Any]]:
        """ツールの戻り値を metric_keys_json に従って metrics dict に展開する。"""
        if not config.METRIC_KEYS_JSON:
            if isinstance(result, (int, float)):
                return {"value": {"value_num": float(result)}}
            if isinstance(result, str):
                return {"value": {"value_text": result}}
            return {}

        keys_config = json.loads(config.METRIC_KEYS_JSON)
        metrics: Dict[str, Dict[str, Any]] = {}

        if isinstance(result, dict):
            for metric_name, source_key in keys_config.items():
                val = result.get(source_key)
                if val is None:
                    continue
                if isinstance(val, (int, float)):
                    metrics[metric_name] = {"value_num": float(val)}
                else:
                    metrics[metric_name] = {"value_text": str(val)}
        elif isinstance(result, str):
            try:
                result_dict = json.loads(result)
                return self._parse_tool_result(config, result_dict)
            except (json.JSONDecodeError, TypeError):
                pass

        return metrics

    # ------------------------------------------------------------------
    # Notify Rules — 閾値判定
    # ------------------------------------------------------------------

    def _evaluate_notify_rules(
        self,
        config: ObserverConfig,
        metrics: Dict[str, Dict[str, Any]],
        recorded_at: datetime,
    ) -> None:
        """NOTIFY_RULES_JSON を評価し、条件ヒットで Building に通知する。"""
        if not config.NOTIFY_RULES_JSON:
            return

        try:
            rules = json.loads(config.NOTIFY_RULES_JSON)
        except (json.JSONDecodeError, TypeError):
            return

        db: Session = self.manager.SessionLocal()
        try:
            fixture = db.query(Fixture).filter(
                Fixture.FIXTURE_ID == config.FIXTURE_ID
            ).first()
            if not fixture:
                return
            building_id = fixture.BUILDING_ID
        finally:
            db.close()

        for rule in rules:
            metric_name = rule.get("metric")
            if metric_name not in metrics:
                continue

            value = metrics[metric_name].get("value_num")
            if value is None:
                continue

            triggered = False
            message = ""

            threshold_above = rule.get("above")
            if threshold_above is not None and value > threshold_above:
                triggered = True
                message = rule.get("message_above", f"{metric_name} が {value} に上昇 (閾値: {threshold_above})")

            threshold_below = rule.get("below")
            if threshold_below is not None and value < threshold_below:
                triggered = True
                message = rule.get("message_below", f"{metric_name} が {value} に低下 (閾値: {threshold_below})")

            if triggered:
                self._notify_building(building_id, config, message, recorded_at)

    def _notify_building(
        self,
        building_id: str,
        config: ObserverConfig,
        message: str,
        recorded_at: datetime,
    ) -> None:
        """Building event として通知を注入する。"""
        self.manager.add_building_event(
            building_id,
            {
                "role": "host",
                "content": message,
                "event_type": "observer_alert",
                "metadata": {
                    "observer_id": config.OBSERVER_ID,
                    "fixture_id": config.FIXTURE_ID,
                    "recorded_at": recorded_at.isoformat(),
                },
            },
        )
        LOGGER.info("[observer] notification sent to %s: %s", building_id, message)
