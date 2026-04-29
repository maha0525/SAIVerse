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
from typing import Any, List

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

    # v0.11 拡張: 起点ライン種別 (Intent A v0.14, Intent B v0.11)
    # 自律 Track は連続的な Playbook 実行 (思索・記憶整理・創作等) なので、
    # Pulse 1 サイクルあたりのコストを抑えるため軽量モデル (sub_line) を起点にする。
    # 重量級モデルが必要な部分処理は子ラインとして必要時のみ呼び出す。
    default_entry_line_role: str = "sub_line"

    pulse_completion_notice: str = (
        "あなたは自律 Track で作業中です。\n"
        "- output_target: none (独白、直接の他者応答ではない)\n"
        "- Pulse 完了後の挙動: メタレイヤーが続行 / 切替 / 完了を判断する。\n"
        "  作業が一段落したと感じたら track_complete で完了、別の作業に移りたければ track_pause で一時停止できる。"
    )

    available_spells_doc: str = (
        "[使用可能な Track 操作スペル (自律 Track)]\n"
        "発動形式は **行頭が `/spell ` で始まる**こと:\n"
        "  /spell <スペル名> key='value' key2=value2 ...\n"
        "例: /spell track_complete track_id='...'\n"
        "\n"
        "利用可能なスペル名:\n"
        "- track_pause: 現在の Track を一時停止 (引数: track_id='...')\n"
        "- track_complete: 現在の Track を完了 (引数: track_id='...')\n"
        "- track_create: 新しい Track を作成 (引数: track_type='...', title='...', intent='...', activate=True)\n"
        "- track_list: 現在の Track 一覧を確認 (引数なし)\n"
        "- note_open: Note を開く (引数: note_id='...')\n"
        "- note_close: Note を閉じる (引数: note_id='...')\n"
        "- note_search: Note を検索 (引数: query='...')"
    )

    def __init__(self, track_manager: TrackManager):
        self.track_manager = track_manager

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
        track_id_short = track.track_id[:8] + "…"
        intent = track.intent or "(意図未設定)"
        lines = [
            "## Track 切替通知 (自律 Track)",
            f"あなたは Track 「{title}」 (id={track_id_short}, type=autonomous) に入りました。",
            f"intent: {intent}",
            "",
            self.pulse_completion_notice,
            "",
            self.available_spells_doc,
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
