"""PulseDispatcher: ペルソナを動かす全イベントの一元的なディスパッチャ。

pulse_dispatch.md §7 で定義した統一ディスパッチャ層。各起点コード
(manager/runtime, SubLineScheduler, ScheduleManager, AutonomyManager,
phenomena 系) は本クラスのメソッドを通じてイベントを発火させる。

責務:
- イベント受け口 (各起点が呼ぶ統一インターフェース)
- 経路選択 (直接経路 / 熟慮経路) — 経路マッピングはイベント種別ごとに固定
  ハードコード。動的な Track 状態判定は Handler に委譲する
- 実行先の振り分け (Handler / PulseController.submit_xxx / MetaLayer)
- 共通処理を仕込む受け皿 (メトリクス / pre-spell 注入 / 経路別ログ等の余地)

責務外:
- Track 状態の動的判定 (Handler 内に残す: 種別ごとの振る舞い差を吸収する責務)
- Pulse の priority 制御 (PulseController に委譲)
- メタ判断のロジック (MetaLayer に委譲)

詳細: docs/intent/persona_cognition/pulse_dispatch.md §7
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

LOGGER = logging.getLogger(__name__)


class PulseDispatcher:
    """ペルソナの全イベントを一元的にディスパッチするレイヤ。"""

    def __init__(self, manager: "SAIVerseManager"):
        self.manager = manager

    # ------------------------------------------------------------------
    # ユーザー発話 (1a / 1b)
    # ------------------------------------------------------------------

    def dispatch_user_utterance(
        self,
        persona_id: str,
        user_id: str,
        event: Dict[str, Any],
        invoke_main_line: Callable[..., Any],
    ) -> None:
        """ユーザー発話イベントを UserConversationTrackHandler にディスパッチする。

        Track 状態判定 (running → 直接経路 1-A, それ以外 → 熟慮経路 1-B) は
        Handler 内で行う (種別固有の振る舞いなので Handler 責務)。本メソッドは
        Handler への委譲 + 共通処理 + 例外時のフォールバックを担う。

        Handler が例外を出した場合は ``invoke_main_line()`` を直接呼んで応答を
        守る (現状の handle_user_input フォールバックと同等)。
        """
        handler = getattr(self.manager, "user_conversation_handler", None)
        if handler is None:
            LOGGER.warning(
                "[dispatcher] user_utterance: user_conversation_handler unavailable; "
                "falling back to direct invoke_main_line (persona=%s)",
                persona_id,
            )
            invoke_main_line()
            return
        try:
            handler.on_user_utterance(
                persona_id=persona_id,
                user_id=user_id,
                event=event,
                invoke_main_line=invoke_main_line,
            )
        except Exception:
            LOGGER.exception(
                "[dispatcher] user_utterance handler raised; falling back to invoke_main_line "
                "(persona=%s)",
                persona_id,
            )
            invoke_main_line()

    # ------------------------------------------------------------------
    # 自律 Track 連続 Pulse — SubLineScheduler の poll 経路 (2a)
    # ------------------------------------------------------------------

    def dispatch_subline_poll(
        self,
        persona_id: str,
        persona: Any,
        track: Any,
        playbook_name: str,
    ) -> None:
        """SubLineScheduler の poll で running な連続実行型 Track を発見した
        ときのディスパッチ。直接経路で ``run_sea_auto`` を呼ぶ。"""
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            LOGGER.debug(
                "[dispatcher] subline_poll: no building_id (persona=%s); skipping",
                persona_id,
            )
            return
        run_sea_auto = getattr(self.manager, "run_sea_auto", None)
        if run_sea_auto is None:
            LOGGER.warning(
                "[dispatcher] subline_poll: run_sea_auto unavailable (persona=%s)",
                persona_id,
            )
            return
        try:
            run_sea_auto(
                persona,
                building_id,
                occupants=[],  # auto pulse では使われない
                meta_playbook=playbook_name,
                args={"track_id": track.track_id},
            )
        except Exception:
            LOGGER.exception(
                "[dispatcher] subline_poll dispatch failed: persona=%s track=%s",
                persona_id, track.track_id,
            )

    # ------------------------------------------------------------------
    # スケジュール時刻到来 (3)
    # ------------------------------------------------------------------

    def dispatch_schedule_fire(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        pre_spells: Optional[List[str]] = None,
    ) -> None:
        """スケジュール時刻到来時のディスパッチ。直接経路で submit_schedule。

        スケジュール ↔ Track 紐付けは別 Intent doc 範疇 (pulse_dispatch.md §11)。
        現状はスケジュール定義通りの Pulse 種別で起動する。
        """
        pulse_controller = getattr(self.manager, "pulse_controller", None)
        if pulse_controller is None:
            LOGGER.warning(
                "[dispatcher] schedule_fire: pulse_controller unavailable (persona=%s)",
                persona_id,
            )
            return
        try:
            pulse_controller.submit_schedule(
                persona_id=persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata=metadata,
                meta_playbook=meta_playbook,
                args=args,
                pre_spells=pre_spells,
            )
        except Exception:
            LOGGER.exception(
                "[dispatcher] schedule_fire dispatch failed: persona=%s",
                persona_id,
            )

    # ------------------------------------------------------------------
    # 現象 (Phenomenon) からのイベント注入 (9)
    # ------------------------------------------------------------------

    def dispatch_phenomenon_event(
        self,
        persona_id: str,
        building_id: str,
        user_input: str,
        metadata: Optional[Dict[str, Any]] = None,
        meta_playbook: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
    ) -> None:
        """現象システムからのイベント注入 (Kitchen 等)。直接経路で submit_schedule。

        現状はスケジュールと同じ priority レーン (SCHEDULE) で起動する。
        現象種別ごとに別 priority が必要になれば将来分岐させる。
        """
        pulse_controller = getattr(self.manager, "pulse_controller", None)
        if pulse_controller is None:
            LOGGER.warning(
                "[dispatcher] phenomenon_event: pulse_controller unavailable (persona=%s)",
                persona_id,
            )
            return
        try:
            pulse_controller.submit_schedule(
                persona_id=persona_id,
                building_id=building_id,
                user_input=user_input,
                metadata=metadata,
                meta_playbook=meta_playbook,
                args=args,
            )
        except Exception:
            LOGGER.exception(
                "[dispatcher] phenomenon_event dispatch failed: persona=%s",
                persona_id,
            )

    # ------------------------------------------------------------------
    # 自律 tick — AutonomyManager の定期メタ判断 (2b)
    # ------------------------------------------------------------------

    def dispatch_autonomy_tick(
        self,
        persona_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """AutonomyManager の定期 tick。熟慮経路で MetaLayer.on_periodic_tick。

        段階 5 で MetaLayer も PulseController 経由 (並列レーン) に統一する
        予定 (pulse_dispatch.md §6.3, §9.5)。現状は MetaLayer 直叩き。
        """
        meta_layer = getattr(self.manager, "meta_layer", None)
        if meta_layer is None:
            LOGGER.warning(
                "[dispatcher] autonomy_tick: meta_layer unavailable (persona=%s)",
                persona_id,
            )
            return
        try:
            meta_layer.on_periodic_tick(persona_id, context=context or {})
        except Exception:
            LOGGER.exception(
                "[dispatcher] autonomy_tick dispatch failed: persona=%s",
                persona_id,
            )
