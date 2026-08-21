"""PulseDispatcher: ペルソナを動かす全イベントの一元的なディスパッチャ。

pulse_dispatch.md §7 で定義した統一ディスパッチャ層。各起点コード
(manager/runtime, ScheduleManager, AutonomyManager, phenomena 系) は
本クラスのメソッドを通じてイベントを発火させる。
(旧 SubLineScheduler 経路 2a は自律行動 v2 で廃止 — intent §9.3。)

責務:
- イベント受け口 (各起点が呼ぶ統一インターフェース)
- 経路選択 — 経路マッピングはイベント種別ごとに固定ハードコード
- 実行先の振り分け (会話経路 / PulseController.submit_xxx / 判断点)
- 共通処理を仕込む受け皿 (メトリクス / pre-spell 注入 / 経路別ログ等の余地)

責務外:
- 会話の状態判定 (``saiverse.user_conversation`` に残す)
- Pulse の priority 制御 (PulseController に委譲)
- 判断点のロジック (saiverse.autonomy_wiring / judgment_points に委譲)

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
        invoke_main_line: Callable[[], Any],
        pulse_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """ユーザー発話イベントを会話経路 (``saiverse.user_conversation``) へ渡す。

        経路判定 (会話継続 → 直接応答 / 会話が閉じていて別の活動中 → on_event
        判断点での仲裁) は受け口側が行う。本メソッドは委譲 + 共通処理 + 例外時の
        フォールバックを担う。

        **フォールバックは「まだ何もしていない失敗」に限る** (2026-08-21 Codex
        指摘 4)。旧実装は受け口の任意の例外で ``invoke_main_line()`` を呼び直して
        いたが、新規会話では出来事の開設と main_line 起動の**後**に沈黙タイマーの
        装填が走るため、装填だけ転んだ並びで同じ発話がもう一度処理され二重応答に
        なった。受け口は :class:`~saiverse.user_conversation.UserUtteranceError`
        に「どこまで実行したか」を載せて送出するので、その申告で分岐する:

        - ``fallback_safe=True``: 副作用の手前で転んだ → 直接応答で肩代わりする
          (旧フォールバックと同じ救済)。
        - ``side_effects_done=True``: 応答・台帳・判断点のどれかが既に動いた →
          **呼び直さず**、元の例外をそのまま上へ通す。握り潰さないのは、
          ``handle_user_input_stream`` の backend_worker が LLMError を専用の
          エラーイベントへ変換しており、ここで飲むとユーザーの画面に何も出ない
          まま応答が消えるため (旧実装は 2 回目の呼び出しでこの例外を再生産して
          いた — 失敗の見え方は保ち、二重実行だけをやめる)。
        - どちらでもない (会話の出来事を開けなかった): 送出する。記録なしの応答は
          「いま会話中か」の判定・仲裁・スルースを揃って狂わせるので、黙って
          返すより見えて失敗する方が正しい (Codex 指摘 6)。

        Args:
            pulse_options: 会話開始経路で main_line Pulse へ転送する起動オプション
                (metadata / meta_playbook / args / pre_spells / event_callback)。
                ``invoke_main_line`` の closure が抱えているものと同じ値を渡す。
        """
        from saiverse.user_conversation import UserUtteranceError, on_user_utterance

        try:
            on_user_utterance(
                self.manager,
                persona_id=persona_id,
                user_id=user_id,
                event=event,
                invoke_main_line=invoke_main_line,
                pulse_options=pulse_options,
            )
        except UserUtteranceError as e:
            if e.fallback_safe:
                LOGGER.exception(
                    "[dispatcher] user_utterance handling failed at stage=%s before "
                    "any side effect; falling back to invoke_main_line (persona=%s)",
                    e.stage, persona_id,
                )
                invoke_main_line()
                return
            if e.side_effects_done:
                LOGGER.exception(
                    "[dispatcher] user_utterance handling failed at stage=%s after "
                    "side effects were already performed; NOT retrying the response "
                    "(persona=%s)",
                    e.stage, persona_id,
                )
                # 元の例外を通す (LLMError の専用エラー表示を殺さないため)。
                if e.__cause__ is not None:
                    raise e.__cause__
                raise
            LOGGER.exception(
                "[dispatcher] user_utterance handling failed at stage=%s; refusing to "
                "answer without a conversation record (persona=%s)",
                e.stage, persona_id,
            )
            raise
        except Exception:
            # 受け口が分類しなかった失敗 (import 失敗など、経路に入る手前)。
            # 副作用は起きていないので旧来どおり直接応答で救う。
            LOGGER.exception(
                "[dispatcher] user_utterance handling raised before the handler "
                "classified it; falling back to invoke_main_line (persona=%s)",
                persona_id,
            )
            invoke_main_line()

    # NOTE: 旧 dispatch_subline_poll (SubLineScheduler の autonomous Track
    # 連続 Pulse、経路 2a) は自律行動 v2 で廃止 (intent §9.3)。自律駆動は
    # 時間割のコマ発火 (saiverse/day_plan.py) + 判断点
    # (saiverse/autonomy_wiring.py) が担う。

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
    ) -> Dict[str, Any]:
        """スケジュール時刻到来時のディスパッチ。直接経路で schedule Pulse を submit。

        スケジュール ↔ Track 紐付けは別 Intent doc 範疇 (pulse_dispatch.md §11)。
        現状はスケジュール定義通りの Pulse 種別で起動する。

        W3 Chunk A (schedule 台帳化 D4): 旧「例外握り潰し・None 戻し」をやめ、
        型付き outcome を返す。ExecutionRequest を自前で組んで submit し、
        PulseController が request に記入する観測フィールド
        (dispatch_action / runtime_outcome) から顛末を読む。例外は再送出しない
        (呼び出し元 = ScheduleManager が台帳の failed/unknown 分類に使う)。

        Returns:
            ``{"action": str, "runtime_outcome": Optional[str], "error": Optional[str]}``

            - action: "execute" / "queued" / "skipped" (submit の受付裁定) /
              "unavailable" (pulse_controller 不在) /
              "error_before_submit" (受付裁定前に例外)
            - runtime_outcome: "completed" / "gate_closed" / "cancelled" /
              "error"。queued / skipped / unavailable では None。
            - error: 例外発生時のみ str(e)。
        """
        # NOTE: sea.pulse_controller は saiverse 側から遅延 import する
        # (dispatch_autonomy_tick と同じ流儀 — module 間の import 循環を避ける)
        from sea.pulse_controller import ExecutionRequest

        pulse_controller = getattr(self.manager, "pulse_controller", None)
        if pulse_controller is None:
            LOGGER.warning(
                "[dispatcher] schedule_fire: pulse_controller unavailable (persona=%s)",
                persona_id,
            )
            return {"action": "unavailable", "runtime_outcome": None, "error": None}

        request = ExecutionRequest(
            type="schedule",
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
            pre_spells=pre_spells,
        )
        try:
            pulse_controller.submit(request)
        except Exception as e:
            # LLMError 含む。submit が受付裁定 (dispatch_action) を記入する前に
            # 転んだ場合は "error_before_submit" とする。
            LOGGER.exception(
                "[dispatcher] schedule_fire dispatch failed: persona=%s",
                persona_id,
            )
            if request.runtime_outcome == "completed":
                # 実行は完走済み (request に記入済み) で、例外はその後の後処理
                # (queue 処理等) のもの (Codex W3 第七陣)。完了済みの顛末を
                # "error" で上書きすると、成功した発火が unknown 扱いになり
                # oneshot/interval の occurrence が自動再実行禁止で封印される。
                # 完走の事実を優先し、後処理の失敗は error に併記する。
                return {
                    "action": request.dispatch_action,
                    "runtime_outcome": "completed",
                    "error": f"post-completion: {e or type(e).__name__}",
                }
            return {
                "action": request.dispatch_action or "error_before_submit",
                "runtime_outcome": "error",
                "error": str(e) or type(e).__name__,
            }
        return {
            "action": request.dispatch_action,
            "runtime_outcome": request.runtime_outcome,
            "error": None,
        }

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
    ) -> bool:
        """現象システムからのイベント注入 (Kitchen 等)。直接経路で schedule Pulse。

        現状はスケジュールと同じ priority レーン (SCHEDULE) で起動する。
        現象種別ごとに別 priority が必要になれば将来分岐させる。

        実体は :meth:`dispatch_schedule_fire` (型付き outcome) — submit が例外を
        投げなくても Pulse が起動していないケース (Beat 関所閉鎖 / PulseController
        内部で畳まれた例外) があるため、受付裁定と実行顛末を観測してから成否を
        返す。

        Returns:
            True = Pulse が実行完走した、または queue に受付されて消えない
            (action=queued / runtime_outcome=cancelled は復帰 queue に残る —
            ScheduleManager._classify_dispatch_outcome の accepted と同じ裁定)。
            False = 起動していない (pulse_controller 不在 / 関所閉鎖 / skipped /
            例外)。呼び出し側が「起動できた」を前提に成功を記録しないための
            型付き戻り値 — 回収経路の応対復元 (autonomy_wiring) と
            inject_persona_event が読む。
        """
        result = self.dispatch_schedule_fire(
            persona_id=persona_id,
            building_id=building_id,
            user_input=user_input,
            metadata=metadata,
            meta_playbook=meta_playbook,
            args=args,
        )
        return (
            result.get("action") == "queued"
            or result.get("runtime_outcome") in ("completed", "cancelled")
        )

    # ------------------------------------------------------------------
    # 自律 tick — AutonomyManager の定期 tick (2b, v2 で watchdog に縮退)
    # ------------------------------------------------------------------

    def dispatch_autonomy_tick(
        self,
        persona_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """AutonomyManager の定期 tick。自律行動 v2 で **watchdog** に縮退した
        (intent §4.2)。

        旧: MetaLayer.on_periodic_tick (状況分類 → meta_judgment_* の定期
        ディスパッチ)。この役割は判断点 5 種 (day_open / post_conversation /
        post_session / on_event / day_close) が引き継いだため、定期経路は
        「時間割の発火が途絶えたときだけ火を入れ直す見張り」
        (saiverse.autonomy_wiring.watchdog_tick) になった。正常時は何もしない。
        (旧 v1 メタ判断は on_track_alert ごと退役 — track_retirement.md §7.4。
        別行動中のユーザー発話の仲裁は on_event 判断点への直結が後継。)
        """
        try:
            from saiverse.autonomy_wiring import watchdog_tick

            watchdog_tick(self.manager, persona_id)
        except Exception:
            LOGGER.exception(
                "[dispatcher] autonomy_tick (watchdog) failed: persona=%s",
                persona_id,
            )
