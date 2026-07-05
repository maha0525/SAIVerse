"""AutonomousTrackHandler: 自律 Track の管理。

Intent A v0.13 / Intent B v0.10 における「自律 Track」(一時 Track) の振る舞いを
定義する。自律 Track はペルソナが自分の意思で立ち上げる作業の単位。
記憶整理、創作、調査、思索など多様な用途を持つ。

責務:
- 自律 Track の取得 / 一覧
- Pulse 完了時のメタ判断委譲 (post_complete_behavior=meta_judge)
- 連続実行型 Track として SubLineScheduler の対象になる属性提供

責務外:
- Track の自動作成 (ペルソナがメインラインから /track_create で作る経路)
- メタ判断ロジック (Playbook で書く)
- スケジューラ本体 (Phase C-3b/c で別途実装)

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
    # 自律 Track は連続実行型 (Pulse 完了後にメタ判断 → 続行 / 切替 / 完了)。
    # SubLineScheduler の対象になる。
    post_complete_behavior: str = "meta_judge"  # 連続実行型
    default_pulse_interval: int = 30  # 30 秒間隔 (環境次第で上書き)
    default_max_consecutive_pulses: int = -1  # 無制限 (メインキャッシュ TTL までは続行可)
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

        autonomous Track の連続 Pulse は SubLineScheduler の 5 秒 poll で
        拾われる設計。activate 時に Track コンテキスト (intent 含む) を
        SAIMemory に注入する。

        ``suppress_pulse`` は本 Handler では参照しない (Pulse 起動は
        SubLineScheduler 側の責務で、activate 時に直接起動しないため)。
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
        """ペルソナの running な自律 Track 一覧を返す (SubLineScheduler が使う)。"""
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
        """Pulse 完了時の処理。

        自律 Track は post_complete_behavior=meta_judge なので、本来はここで
        メタレイヤーへ「次どうするか」の判断を促す。
        Phase C-3a の最小実装ではログ記録のみ。SubLineScheduler が次 Pulse
        スケジュールを担うため、ここでは追加処理不要。
        メタ判断委譲は Phase C-3c (MainLineScheduler 整備時) で実装する。
        """
        logging.debug(
            "[autonomous-handler] on_pulse_complete: track=%s persona=%s "
            "(behavior=meta_judge, will be picked up by SubLineScheduler for next Pulse)",
            track.track_id, persona_id,
        )
