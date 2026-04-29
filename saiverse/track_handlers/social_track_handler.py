"""SocialTrackHandler: 交流 (social) Track の取得 / 自動作成。

Intent A v0.9 / Intent B v0.6 における「交流 Track」(永続 Track) の管理を担う。
交流 Track はペルソナにつき 1 個。他ペルソナとの会話の場の文脈を保持する。

責務:
- ペルソナ作成時 / 起動時 migration での Track 自動作成 (ensure_track)
- 将来追加: 他ペルソナ発話イベントの受け口 (Phase B-Y)

責務外:
- alert への遷移ルールそのもの (TrackManager に委譲)
- 「相手は誰か」判定 (Phase B-Y で扱う)
- メインライン応答起動 (呼び出し元の責務)

設計上の選択:
- 初期状態は **unstarted**。即 activate しない。
  対ユーザー Track と競合させないため、初回の対ペルソナイベントが来てから activate する。
- output_target は固定で `building:current` (Intent B v0.6)。
  ペルソナが Building 間を移動しても output_target は変わらず、配信先が動的に解決される。

詳細: docs/intent/persona_action_tracks.md (Track 種別 / 永続 Track セクション)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from database.models import ActionTrack

from ..track_manager import TrackManager


# 交流 Track の固定属性。揺らぎを避けるため定数化する。
SOCIAL_TRACK_TYPE = "social"
SOCIAL_TRACK_TITLE = "交流"
SOCIAL_TRACK_OUTPUT_TARGET = "building:current"


class SocialTrackHandler:
    """ペルソナごとの交流 Track の存在保証を担う。

    内部状態を持たないため、SAIVerseManager から 1 インスタンスだけ
    保持して全ペルソナで共有する。
    """

    # v0.10 拡張: Pulse サイクル制御属性 (Intent B v0.10)
    # 交流 Track は応答待ち型 (他ペルソナ発話駆動)。
    post_complete_behavior: str = "wait_response"
    default_pulse_interval: int = 0
    default_max_consecutive_pulses: int = 1
    default_subline_pulse_interval: int = 0

    # v0.11 拡張: 起点ライン種別 (Intent A v0.14, Intent B v0.11)
    # 他ペルソナへの応答も「他者会話」(Intent A 不変条件 9) に該当するため、
    # 重量級モデル = 起点メインラインで動作する。
    default_entry_line_role: str = "main_line"

    pulse_completion_notice: str = (
        "あなたは交流 Track にいます。\n"
        "- output_target: building:current (今いる場所の他ペルソナに届く)\n"
        "- Pulse 完了後の挙動: 相手の応答を待つ。次のイベント (他ペルソナの発話) が来るまで他のことを考えなくて良い。"
    )

    def __init__(self, track_manager: TrackManager):
        self.track_manager = track_manager

    # ------------------------------------------------------------------
    # Pulse 完了フック (v0.10)
    # ------------------------------------------------------------------

    def on_pulse_complete(
        self, persona_id: str, track: ActionTrack, pulse_outputs: Any
    ) -> None:
        """Pulse 完了時の処理。応答待ち型なのでアイドル化のみ。"""
        logging.debug(
            "[social-handler] on_pulse_complete: track=%s persona=%s "
            "(behavior=wait_response, awaiting peer utterance)",
            track.track_id, persona_id,
        )

    # ------------------------------------------------------------------
    # Ensure
    # ------------------------------------------------------------------

    def ensure_track(self, persona_id: str) -> ActionTrack:
        """ペルソナの交流 Track を取得 (なければ作成)。

        既に存在する場合は何も変更せず既存 Track を返す。
        新規作成時は **unstarted** で作成する (即 activate しない)。

        ペルソナ作成時 hook と起動時 migration の両方から呼ばれる前提で
        冪等性を持たせる。

        Returns:
            交流 Track の ActionTrack。
        """
        existing = self._find_existing(persona_id)
        if existing is not None:
            logging.debug(
                "[social-handler] Existing social track found for persona=%s: %s",
                persona_id, existing.track_id,
            )
            return existing

        track_id = self.track_manager.create(
            persona_id=persona_id,
            track_type=SOCIAL_TRACK_TYPE,
            title=SOCIAL_TRACK_TITLE,
            is_persistent=True,
            output_target=SOCIAL_TRACK_OUTPUT_TARGET,
        )
        logging.info(
            "[social-handler] Created social track %s for persona=%s (unstarted)",
            track_id, persona_id,
        )
        return self.track_manager.get(track_id)

    def _find_existing(self, persona_id: str) -> Optional[ActionTrack]:
        """ペルソナの既存 social Track を探す (1 つ目を返す)。

        Intent B v0.6 の「ペルソナにつき 1 個」原則に従い、本来複数
        存在しないが、データ整合性チェックは責務外なので最初に見つかった
        ものを返す。複数残存していた場合の整理はマイグレーション側の責務。
        """
        for t in self.track_manager.list_for_persona(persona_id):
            if t.track_type == SOCIAL_TRACK_TYPE:
                return t
        return None
