"""Internal alert poller (intent B v0.7 §"内部 alert ポーラ機構").

Track 自身が条件超過で ``set_alert`` を発火する仕組みを駆動するバックグラウンド
スレッド。intent では Handler 側に ``tick(persona_id)`` を持たせる方針が示されて
いるため、ここでは:

1. 各 Track Handler に ``tick(persona_id)`` があれば呼ぶ (将来の身体的欲求 /
   スケジュール / 知覚起因 Handler の拡張点)。
2. 加えて、汎用の **パラメータ閾値判定** を全 Track に対して実施する:
   ``metadata.parameters[name]`` が ``metadata.thresholds[name]`` を超えたら
   ``track_manager.set_alert`` を発火し、context に トリガ種別 + 値を載せる。

頻度は ``SAIVERSE_INTERNAL_ALERT_INTERVAL_SECONDS`` (デフォルト 60 秒) で
制御。intent 通り、身体的欲求は 1 分、知覚起因は 5 分のような種別ごとの
頻度差は将来の Handler 単位の細分化で対応する (現状は単一頻度)。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 60

# パラメータ閾値超過 alert を発火する対象 Track 状態。
# completed/aborted/forgotten 等は対象外。
_ELIGIBLE_STATUSES = {"running", "pending", "waiting", "unstarted", "alert"}

# Handler 探索順 (sea/pulse_root_context.py の _HANDLER_ATTR_BY_TYPE と一致)
_HANDLER_ATTRS = (
    "user_conversation_handler",
    "social_track_handler",
    "autonomous_track_handler",
)


class InternalAlertPoller:
    """Background thread that polls Track parameters and fires internal alerts."""

    def __init__(self, manager: "SAIVerseManager", interval_seconds: Optional[int] = None):
        self.manager = manager
        if interval_seconds is None:
            env_val = os.environ.get("SAIVERSE_INTERNAL_ALERT_INTERVAL_SECONDS")
            if env_val:
                try:
                    interval_seconds = max(5, int(env_val))
                except ValueError:
                    LOGGER.warning(
                        "Invalid SAIVERSE_INTERNAL_ALERT_INTERVAL_SECONDS=%r; using default",
                        env_val,
                    )
                    interval_seconds = DEFAULT_INTERVAL_SECONDS
            else:
                interval_seconds = DEFAULT_INTERVAL_SECONDS
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            LOGGER.debug("[internal-alert-poller] Already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="internal-alert-poller",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info(
            "[internal-alert-poller] Started (interval=%d sec)", self.interval_seconds,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        LOGGER.info("[internal-alert-poller] Stopped")

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        # 起動時即発火しない (他の起動処理と競合しないよう interval 待ってから)
        self._stop_event.wait(self.interval_seconds)
        while not self._stop_event.is_set():
            try:
                self._tick_once()
            except Exception:
                LOGGER.exception("[internal-alert-poller] tick failed")
            self._stop_event.wait(self.interval_seconds)

    def _tick_once(self) -> None:
        personas = list(getattr(self.manager, "personas", {}).keys())

        # 1. Handler tick (intent B v0.7 §"内部 alert ポーラ機構")
        for handler in self._iter_handlers():
            tick = getattr(handler, "tick", None)
            if not callable(tick):
                continue
            for persona_id in personas:
                try:
                    tick(persona_id)
                except Exception:
                    LOGGER.exception(
                        "[internal-alert-poller] handler tick failed: handler=%s persona=%s",
                        type(handler).__name__, persona_id,
                    )

        # 2. 汎用パラメータ閾値判定
        track_manager = getattr(self.manager, "track_manager", None)
        if track_manager is None:
            return
        for persona_id in personas:
            self._poll_parameters_for_persona(persona_id, track_manager)

    def _iter_handlers(self) -> Iterable[Any]:
        for attr in _HANDLER_ATTRS:
            handler = getattr(self.manager, attr, None)
            if handler is not None:
                yield handler

    def _poll_parameters_for_persona(self, persona_id: str, track_manager: Any) -> None:
        from database.models import ActionTrack

        try:
            db = self.manager.SessionLocal()
        except Exception:
            LOGGER.exception(
                "[internal-alert-poller] Failed to open DB session for %s", persona_id,
            )
            return
        try:
            tracks = (
                db.query(ActionTrack)
                .filter(
                    ActionTrack.persona_id == persona_id,
                    ActionTrack.is_forgotten == False,  # noqa: E712
                    ActionTrack.status.in_(list(_ELIGIBLE_STATUSES)),
                )
                .all()
            )
        finally:
            db.close()

        for track in tracks:
            try:
                self._evaluate_track(track, track_manager)
            except Exception:
                LOGGER.exception(
                    "[internal-alert-poller] evaluate failed: track=%s",
                    getattr(track, "track_id", "?"),
                )

    def _evaluate_track(self, track: Any, track_manager: Any) -> None:
        raw = getattr(track, "track_metadata", None)
        if not raw:
            return
        try:
            metadata = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(metadata, dict):
            return
        params = metadata.get("parameters")
        thresholds = metadata.get("thresholds")
        if not isinstance(params, dict) or not isinstance(thresholds, dict):
            return

        for name, threshold in thresholds.items():
            try:
                value = params.get(name)
                if value is None:
                    continue
                if float(value) >= float(threshold):
                    track_manager.set_alert(
                        track.track_id,
                        context={
                            "trigger": "internal_alert",
                            "param": name,
                            "value": float(value),
                            "threshold": float(threshold),
                        },
                    )
                    LOGGER.info(
                        "[internal-alert-poller] Internal alert fired: track=%s param=%s value=%s threshold=%s",
                        track.track_id, name, value, threshold,
                    )
                    # 1 Track につき 1 alert を発火したら他のパラメータ判定はスキップ
                    # (alert は冪等な遷移なので、複数発火しても害はないが過剰ログ抑止)
                    return
            except (TypeError, ValueError):
                continue
            except Exception:
                LOGGER.exception(
                    "[internal-alert-poller] set_alert failed: track=%s param=%s",
                    track.track_id, name,
                )
