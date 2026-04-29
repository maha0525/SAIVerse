"""Pulse スケジューラ群 (Phase C-3b)。

Intent A v0.13 / Intent B v0.10 における Pulse 階層 (メインライン Pulse /
サブライン Pulse) のうち、サブライン Pulse の自動起動を担う SubLineScheduler
を実装する。

責務 (SubLineScheduler):
- ACTIVITY_STATE=Active なペルソナの running な連続実行型 Track を定期的に
  ポーリングし、Track の Pulse 間隔・連続実行回数上限に従って次 Pulse を
  トリガする
- Track 種別ごとの Handler から default_pulse_interval / default_max_consecutive_pulses
  を取得し、Track metadata で上書きされていればそれを優先する
- Pulse 起動経路は SAIVerseManager.run_sea_auto (meta_playbook 指定) 経由

責務外:
- メタ判断ロジック (Playbook 内で行う)
- Track 状態遷移 (TrackManager に委譲)
- メインライン Pulse の起動 (Phase C-3c で MainLineScheduler を別途実装予定)

詳細: docs/intent/persona_action_tracks.md (v0.10 Pulse 階層と 7 制御点)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

from database.models import ActionTrack

from .track_handlers import (
    AutonomousTrackHandler,
)
from .track_manager import STATUS_RUNNING, TrackManager


# Track 種別と Playbook 名のマッピング (Phase C-3b 最小実装)。
# Track 種別ごとに連続実行で起動する Playbook を定義。
# meta_judge 型 (連続実行型) の Track 種別のみが対象。
_TRACK_TYPE_TO_PLAYBOOK: Dict[str, str] = {
    "autonomous": "track_autonomous",
}

# Track 種別と Handler クラスのマッピング (Pulse 制御属性参照用)。
# Phase C-3a で導入した Handler のクラス属性 (default_pulse_interval 等) を
# Track 種別から引けるようにする。
_TRACK_TYPE_TO_HANDLER_CLASS = {
    "autonomous": AutonomousTrackHandler,
}


def _get_subline_scheduler_interval() -> int:
    """SubLineScheduler のポーリング周期 (秒)。環境変数で上書き可能。"""
    try:
        return int(os.environ.get("SAIVERSE_SUBLINE_SCHEDULER_INTERVAL_SECONDS", "5"))
    except ValueError:
        return 5


def is_subline_scheduler_enabled() -> bool:
    """SubLineScheduler の起動を環境変数で制御。

    `SAIVERSE_SUBLINE_SCHEDULER_ENABLED=false` (or 0/no/off) にすると起動しない。
    デフォルトは true (起動する)。

    動き出した自律行動を止めたい時の安全弁。サーバー再起動が必要だが、
    起動段階で確実に止められる。稼働中の緊急停止には scripts/debug_track.py
    の pause-all-autonomous コマンドを使う。
    """
    raw = os.environ.get("SAIVERSE_SUBLINE_SCHEDULER_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off", "")


class SubLineScheduler:
    """連続実行型 Track のサブライン Pulse を定期起動する background loop。

    SAIVerseManager から start() / stop() で起動・停止される。
    内部で daemon thread を 1 本走らせ、定期的にペルソナを巡回する。
    """

    def __init__(self, manager: Any):
        """
        Args:
            manager: SAIVerseManager 参照。
                - manager.personas: ペルソナ辞書
                - manager.track_manager: TrackManager
                - manager.run_sea_auto: Pulse 起動経路
        """
        self.manager = manager
        self.track_manager: TrackManager = manager.track_manager
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._interval_seconds = _get_subline_scheduler_interval()

    # ------------------------------------------------------------------
    # ライフサイクル
    # ------------------------------------------------------------------

    def start(self) -> None:
        """background thread を起動する。多重起動はしない。"""
        if self._thread is not None and self._thread.is_alive():
            logging.warning("[subline-scheduler] start() called but already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="SubLineScheduler", daemon=True
        )
        self._thread.start()
        logging.info(
            "[subline-scheduler] Started (poll interval: %d sec)",
            self._interval_seconds,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """停止シグナルを送り、thread の終了を待つ。"""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logging.warning(
                "[subline-scheduler] thread did not stop within %.1f sec", timeout
            )
        self._thread = None
        logging.info("[subline-scheduler] Stopped")

    # ------------------------------------------------------------------
    # ループ本体
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """定期ポーリングループ。stop_event がセットされるまで回る。"""
        logging.info("[subline-scheduler] Loop started")
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logging.exception("[subline-scheduler] tick failed (continuing)")
            # interval 秒待機 (stop_event でも抜けられる)
            self._stop_event.wait(self._interval_seconds)
        logging.info("[subline-scheduler] Loop exited")

    def _tick(self) -> None:
        """1 サイクル: 全ペルソナを巡回し、対象 Track に対して次 Pulse を判定・起動。"""
        personas = getattr(self.manager, "personas", None) or {}
        for persona_id, persona in list(personas.items()):
            try:
                self._tick_persona(persona_id, persona)
            except Exception:
                logging.exception(
                    "[subline-scheduler] tick failed for persona=%s", persona_id
                )

    def _tick_persona(self, persona_id: str, persona: Any) -> None:
        """1 ペルソナ分の処理: running な連続実行型 Track を見て、Pulse をトリガ。"""
        # Phase C-3b 最小実装では ACTIVITY_STATE フィルタは未実装
        # (Active 化機構が無いため、全ペルソナを対象)。
        # Phase C-3c 以降で ACTIVITY_STATE=Active の制約を追加する。

        running_tracks = self.track_manager.list_for_persona(
            persona_id, statuses=[STATUS_RUNNING]
        )
        for track in running_tracks:
            handler_cls = _TRACK_TYPE_TO_HANDLER_CLASS.get(track.track_type)
            if handler_cls is None:
                # 連続実行対象でない Track 種別 (user_conversation / social 等)
                continue
            if getattr(handler_cls, "post_complete_behavior", None) != "meta_judge":
                continue

            playbook_name = _TRACK_TYPE_TO_PLAYBOOK.get(track.track_type)
            if playbook_name is None:
                continue

            if not self._should_trigger_next_pulse(track, handler_cls):
                continue

            self._trigger_pulse(persona_id, persona, track, playbook_name)

    # ------------------------------------------------------------------
    # 判定 + 起動
    # ------------------------------------------------------------------

    def _should_trigger_next_pulse(
        self, track: ActionTrack, handler_cls: type
    ) -> bool:
        """次 Pulse を起動すべきか判定する。

        判定基準 (Intent B v0.10 制御点 1, 2):
        - last_pulse_at から default_pulse_interval (またはトラック metadata
          上書き値) が経過しているか
        - consecutive_pulse_count が default_max_consecutive_pulses 未満か
          (-1 = 無制限)
        """
        meta = self._read_metadata(track)

        # 制御点 1: Pulse 間隔
        interval = meta.get("pulse_interval_seconds")
        if interval is None:
            interval = getattr(handler_cls, "default_pulse_interval", 30)

        last_pulse_at = meta.get("last_pulse_at")
        if last_pulse_at is not None:
            try:
                elapsed = time.time() - float(last_pulse_at)
            except (TypeError, ValueError):
                elapsed = float("inf")
            if elapsed < float(interval):
                return False

        # 制御点 2: 連続実行回数上限
        max_consecutive = meta.get("max_consecutive_pulses")
        if max_consecutive is None:
            max_consecutive = getattr(handler_cls, "default_max_consecutive_pulses", -1)
        if max_consecutive != -1:
            count = meta.get("consecutive_pulse_count", 0)
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0
            if count >= int(max_consecutive):
                logging.debug(
                    "[subline-scheduler] Track %s reached max_consecutive_pulses=%s; "
                    "skipping",
                    track.track_id, max_consecutive,
                )
                return False
        return True

    def _trigger_pulse(
        self,
        persona_id: str,
        persona: Any,
        track: ActionTrack,
        playbook_name: str,
    ) -> None:
        """指定 Track に対してサブライン Pulse を起動する。

        起動経路: SAIVerseManager.run_sea_auto (meta_playbook 指定で
        track_autonomous を auto pulse として起動)。
        起動後、Track metadata の last_pulse_at と consecutive_pulse_count を
        更新する。
        """
        building_id = getattr(persona, "current_building_id", None)
        if building_id is None:
            logging.debug(
                "[subline-scheduler] Persona %s has no current_building_id; "
                "skipping subline pulse",
                persona_id,
            )
            return

        logging.info(
            "[subline-scheduler] Triggering subline pulse: persona=%s track=%s "
            "playbook=%s",
            persona_id, track.track_id, playbook_name,
        )

        try:
            self.manager.run_sea_auto(
                persona,
                building_id,
                occupants=[],  # auto pulse では使われない
                meta_playbook=playbook_name,
                args={"track_id": track.track_id},
            )
        except Exception:
            logging.exception(
                "[subline-scheduler] Failed to trigger subline pulse for track=%s",
                track.track_id,
            )
            return

        # Pulse メタデータ更新 (Track metadata に書き込む)
        self._update_pulse_metadata(track)

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _read_metadata(self, track: ActionTrack) -> Dict[str, Any]:
        if not track.track_metadata:
            return {}
        try:
            data = json.loads(track.track_metadata)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _update_pulse_metadata(self, track: ActionTrack) -> None:
        """Pulse 起動後に metadata.last_pulse_at と consecutive_pulse_count を更新。"""
        from database.models import ActionTrack as ActionTrackModel
        db = self.track_manager.SessionLocal()
        try:
            row = db.query(ActionTrackModel).filter_by(track_id=track.track_id).first()
            if row is None:
                return
            try:
                meta = json.loads(row.track_metadata) if row.track_metadata else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["last_pulse_at"] = time.time()
            meta["consecutive_pulse_count"] = int(meta.get("consecutive_pulse_count", 0)) + 1
            row.track_metadata = json.dumps(meta, ensure_ascii=False)
            db.commit()
        except Exception:
            logging.exception(
                "[subline-scheduler] Failed to update pulse metadata for track=%s",
                track.track_id,
            )
            db.rollback()
        finally:
            db.close()
