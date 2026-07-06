"""AutonomousTrackHandler: 自律 Track の管理。

Intent A v0.13 / Intent B v0.10 における「自律 Track」(一時 Track) の振る舞いを
定義する。自律 Track はペルソナが自分の意思で立ち上げる作業の単位。
記憶整理、創作、調査、思索など多様な用途を持つ。

責務:
- 自律 Track の取得 / 一覧
- activate 時の Track コンテキスト注入

責務外:
- Track の自動作成 (ペルソナがメインラインから /track_create で作る経路)
- メタ判断ロジック (Playbook で書く)
- Pulse の駆動 — 旧 SubLineScheduler (30 秒連続 Pulse) は自律行動 v2 で廃止
  (intent autonomous_behavior_v2.md §9.3)。自律 Track は関心の帳簿であり、
  実行は時間割のコマ発火 (saiverse/day_plan.py) + 判断点が担う

詳細: docs/intent/persona_action_tracks.md (Track 種別 / Pulse 階層 / 7 制御点)
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from database.models import ActionTrack

from ..track_manager import TrackManager


# 自律 Track の固定属性
AUTONOMOUS_TRACK_TYPE = "autonomous"


class AutonomousTrackHandler:
    """自律 Track のイベント振る舞いを定義する。

    内部状態を持たないため、SAIVerseManager から 1 インスタンスだけ
    保持する想定。
    """

    # v0.10 拡張: Pulse サイクル制御属性 (Intent B v0.10)
    # NOTE: 連続 Pulse の駆動 (旧 SubLineScheduler) と max_consecutive_pulses
    # 概念は自律行動 v2 で廃止 (intent §9.3。予算はセッションのラウンド予算に置換)。
    post_complete_behavior: str = "meta_judge"
    default_pulse_interval: int = 30  # 旧・作業のテンポの既定値 (表示互換で残置)
    default_subline_pulse_interval: int = 0  # サブライン連続実行 (ローカル想定)

    # v0.11→v0.32 変更: 起点ライン種別
    # 自律 Track はペルソナの体験そのものであり、通常会話と同じ記憶空間
    # (main_line) に記録されるべき。コスト抑制はモデル選択 (pulse_type=auto
    # → 軽量モデル) で行い、line_role では分離しない。
    default_entry_line_role: str = "main_line"

    pulse_completion_notice: str = (
        "あなたは自律 Track で作業中です。\n"
        "- output_target: none (独白、直接の他者応答ではない)\n"
        "- Pulse 完了後の挙動: メタレイヤーが続行 / 切替 / 完了を判断する。\n"
        "  作業が一段落したと感じたら track_complete で完了、別の作業に移りたければ track_pause で一時停止できる。"
    )

    # Track 種別固有の context 指針。将来 Track Chronicle 周辺の context 注入候補。
    # 自律 Track はサブライン起点 + 連続実行型なので、メタ判断との切り分けが重要。
    # NOTE: 旧 prepare_pulse_root_context は v0.32 (2026-05-09) で削除済み。属性は残置。
    track_specific_guidance: str = (
        "## Track 種別固有の指針 (自律 Track)\n"
        "- 自律 Track は軽量モデルで実行される。\n"
        "- Pulse 完了後はメタレイヤー判断に任せる。続行 / 切替 / 完了は無理に決めなくて良い。\n"
        "- 一段落したら track_pause か track_complete で合図できる。"
    )

    # NOTE: かつてここに available_spells_doc (Track 操作スペルの一覧) があったが、
    # head pipeline の SpellListSection がシステムプロンプトに「## スペル」セクション
    # として同じ内容を常時描画しているため、Track 切替通知への重複掲載は削除した
    # (2026-06-13)。

    def __init__(self, track_manager: TrackManager, manager: Any = None):
        self.track_manager = track_manager
        self.manager = manager

    # ------------------------------------------------------------------
    # Track 状態遷移フック (pulse_dispatch.md §5)
    # ------------------------------------------------------------------

    def on_track_activated(
        self,
        persona_id: str,
        track: ActionTrack,
        pulse_id: Optional[str] = None,
        suppress_pulse: bool = False,
    ) -> None:
        """Track が activate されたときに呼ばれる hook。

        activate 時に Track コンテキスト (intent 含む) を SAIMemory に注入する。
        Pulse は起動しない — autonomous Track の連続 Pulse 駆動は自律行動 v2 で
        廃止済み (実行は時間割のコマ発火が担う)。

        ``suppress_pulse`` は本 Handler では参照しない (もともと activate 時に
        Pulse を直接起動しないため)。
        """
        if track.track_type != AUTONOMOUS_TRACK_TYPE:
            return
        logging.info(
            "[autonomous-handler] on_track_activated: track=%s persona=%s pulse=%s",
            track.track_id, persona_id, pulse_id,
        )
        self._inject_track_context(persona_id, track)

    # ------------------------------------------------------------------
    # Track コンテキスト注入
    # ------------------------------------------------------------------

    def _inject_track_context(self, persona_id: str, track: ActionTrack) -> None:
        """Track コンテキストを SAIMemory に user メッセージ (system タグ付き) として注入する。"""
        if self.manager is None:
            logging.warning(
                "[autonomous-handler] Cannot inject track context: manager is None"
            )
            return
        persona = self._lookup_persona(persona_id)
        if persona is None:
            logging.warning(
                "[autonomous-handler] Cannot inject track context: persona not found (%s)",
                persona_id,
            )
            return

        text = self.build_track_context(track)
        formatted = f"<system>{text}</system>"
        try:
            history_manager = getattr(persona, "history_manager", None)
            if history_manager is None:
                logging.warning(
                    "[autonomous-handler] Persona %s has no history_manager; "
                    "track context not injected",
                    persona_id,
                )
                return
            history_manager.add_to_persona_only(
                {
                    "role": "user",
                    "content": formatted,
                    "metadata": {"tags": ["conversation", "track_context"]},
                },
                origin_track_id=track.track_id,
            )
            logging.info(
                "[autonomous-handler] Injected track context for track=%s persona=%s",
                track.track_id, persona_id,
            )
        except Exception:
            logging.exception(
                "[autonomous-handler] Failed to inject track context for track=%s",
                track.track_id,
            )

    def _lookup_persona(self, persona_id: str) -> Optional[Any]:
        personas = getattr(self.manager, "personas", None) or {}
        return personas.get(persona_id)

    # ------------------------------------------------------------------
    # Track 検索
    # ------------------------------------------------------------------

    def list_active_autonomous_tracks(self, persona_id: str) -> List[ActionTrack]:
        """ペルソナの running な自律 Track 一覧を返す。"""
        from ..track_manager import STATUS_RUNNING
        result = []
        for t in self.track_manager.list_for_persona(persona_id, statuses=[STATUS_RUNNING]):
            if t.track_type == AUTONOMOUS_TRACK_TYPE:
                result.append(t)
        return result

    def build_track_context(self, track: ActionTrack) -> str:
        """Track 切替時に SAIMemory に注入する Track コンテキスト本文。

        UserConversationTrackHandler.build_track_context と同じ構造で、
        自律 Track 種別の情報を入れる。
        """
        title = track.title or "(無題)"
        sid = f"track:{track.short_id}" if track.short_id is not None else track.track_id[:8] + "…"
        intent = track.intent or "(意図未設定)"
        lines = [
            "## Track 切替通知 (自律 Track)",
            f"あなたは Track 「{title}」 (id={sid}, type=autonomous) に入りました。",
            f"intent: {intent}",
            "",
            self.pulse_completion_notice,
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Pulse 完了フック (v0.10)
    # ------------------------------------------------------------------

    def on_pulse_complete(
        self, persona_id: str, track: ActionTrack, pulse_outputs: Any
    ) -> None:
        """Pulse 完了時の処理 (ログ記録のみ)。

        旧設計では SubLineScheduler が次 Pulse を拾っていたが、連続 Pulse は
        自律行動 v2 で廃止された。「次どうするか」は判断点 (post_session 等) が
        決める。
        """
        logging.debug(
            "[autonomous-handler] on_pulse_complete: track=%s persona=%s",
            track.track_id, persona_id,
        )
