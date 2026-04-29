"""UserConversationTrackHandler: 対ユーザー Track のイベント振る舞いを定義する。

Intent A v0.9 / Intent B v0.6 における「対ユーザー会話 Track」(永続 Track) の
ユーザー発話イベントへの反応ルールを実装する。

責務:
- ユーザー発話を受けたときに対ユーザー Track の取得 / 自動作成
- Track が running ならメインライン応答を直接起動
- Track が running 以外なら alert 遷移を起こし、メタレイヤーに判断を委ねる
  (alert observer 経由で MetaLayer が動く)
- メタレイヤー処理後、最終的にメインライン応答を必ず 1 回起動する
- Track が running に **遷移したタイミング** で Track コンテキストを SAIMemory に
  注入する (Intent A v0.12 / 議論 2026-04-28: 末尾追加でキャッシュ親和、ペルソナの
  認知としては「Track 切替を会話の流れの中で受け取る」自然な順序を実現)

責務外:
- alert への遷移ルールそのもの (TrackManager に委譲)
- メタレイヤーの判断ロジック (MetaLayer が alert observer として独立に動く)
- メインライン LLM 呼び出しの実装

詳細: docs/intent/persona_action_tracks.md
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from database.models import ActionTrack

from ..track_manager import (
    STATUS_RUNNING,
    TrackManager,
)


# メインライン応答を起動するための closure シグネチャ。
MainLineInvoker = Callable[[], Any]


class UserConversationTrackHandler:
    """対ユーザー Track のイベント受け口。

    ペルソナごとにインスタンスを分けず、SAIVerseManager から 1 つだけ
    保持する想定 (内部状態を持たないため共有可能)。

    manager 参照を持つのは Track コンテキスト注入時に persona の
    history_manager を引くため (SAIMemory への永続化経路を使う)。
    """

    # Track コンテキストの構成要素 (クラス属性、種別固有)。
    # build_track_context が組み立てる際に参照される。
    pulse_completion_notice: str = (
        "あなたは対ユーザー会話 Track にいます。\n"
        "- output_target: building:current (今いる場所の参加者全員に届く)\n"
        "- Pulse 完了後の挙動: ユーザーの返答を待つ。次のイベントが来るまで他のことを考えなくて良い。"
    )

    available_spells_doc: str = (
        "[使用可能な Track 操作スペル]\n"
        "発話の中に独白として埋め込んで使用できます。必要なときだけ使ってください。\n"
        "発動形式は **行頭が `/spell ` で始まる**こと:\n"
        "  /spell <スペル名> key='value' key2=value2 ...\n"
        "例: /spell track_create track_type='autonomous' title='メモ整理' activate=True\n"
        "\n"
        "利用可能なスペル名:\n"
        "- track_pause: 現在の Track を一時停止 (引数: track_id='...')\n"
        "- track_activate: 別の Track をアクティブ化 (引数: track_id='...')\n"
        "- track_create: 新しい Track を作成 (引数: track_type='...', title='...', intent='...', activate=True)\n"
        "- track_list: 現在の Track 一覧を確認 (引数なし)\n"
        "- note_open: Note を開く (引数: note_id='...')\n"
        "- note_close: Note を閉じる (引数: note_id='...')\n"
        "- note_search: Note を検索 (引数: query='...')"
    )

    # v0.10 拡張: Pulse サイクル制御属性 (Intent B v0.10)
    # 対ユーザー会話 Track は応答待ち型なので、Pulse 連続実行の概念は適用されない。
    # ユーザー発話イベントが来たときだけ起動する。
    post_complete_behavior: str = "wait_response"  # 応答待ち型 (Pulse 完了後アイドル化)
    default_pulse_interval: int = 0  # 連続実行しない (応答駆動)
    default_max_consecutive_pulses: int = 1  # 1 回だけ
    default_subline_pulse_interval: int = 0

    # v0.11 拡張: 起点ライン種別 (Intent A v0.14, Intent B v0.11)
    # ユーザーへの応答は重量級モデルが書く (Intent A 不変条件 9: 他者会話は重量級)。
    # → 起点メインライン Pulse として動作し、メインキャッシュに会話履歴が積まれる。
    default_entry_line_role: str = "main_line"

    # Phase 1.1: prepare_pulse_root_context 用の Track 種別固有の context 指針。
    # 「相手の発話は審判ではなく対話の一部」を明示し、応答待ち姿勢を補強する。
    track_specific_guidance: str = (
        "## Track 種別固有の指針 (対ユーザー会話)\n"
        "- 相手の発話は審判ではなく対話の一部として受け取る。\n"
        "- 応答後はユーザーの返答を待つだけで良い。次の独白までキャッシュは保たれている。\n"
        "- 対ユーザー Track は永続 Track。complete / abort には遷移しない。"
    )

    def __init__(
        self,
        track_manager: TrackManager,
        manager: Any = None,
    ):
        self.track_manager = track_manager
        # SAIVerseManager 参照。persona 取得 (manager.personas) 用。
        # None でも動作する設計 (テスト容易性のため)。
        self.manager = manager

    # ------------------------------------------------------------------
    # Track の取得 / 自動作成
    # ------------------------------------------------------------------

    def get_or_create_track(
        self, persona_id: str, user_id: str
    ) -> Tuple[ActionTrack, bool]:
        """対ユーザー Track を取得 (なければ作成 + activate)。

        Returns:
            (track, was_newly_created)
            - was_newly_created=True の場合、Track は新規に作成 + activate された
              (= running への遷移が発生した)
            - was_newly_created=False の場合、既存 Track を返した
              (status は running のままかもしれないし pending 等かもしれない)
        """
        existing = self._find_existing(persona_id, user_id)
        if existing is not None:
            return existing, False

        track_id = self.track_manager.create(
            persona_id=persona_id,
            track_type="user_conversation",
            title=f"対 user{user_id} 会話",
            is_persistent=True,
            output_target="building:current",
            metadata=json.dumps({"user_id": user_id}, ensure_ascii=False),
        )
        self.track_manager.activate(track_id)
        logging.info(
            "[user-conv-handler] Created and activated user_conversation track "
            "%s persona=%s user_id=%s",
            track_id, persona_id, user_id,
        )
        return self.track_manager.get(track_id), True

    def _find_existing(
        self, persona_id: str, user_id: str
    ) -> Optional[ActionTrack]:
        for t in self.track_manager.list_for_persona(persona_id):
            if t.track_type != "user_conversation":
                continue
            try:
                md = json.loads(t.track_metadata) if t.track_metadata else {}
            except (TypeError, ValueError):
                md = {}
            if md.get("user_id") == user_id:
                return t
        return None

    # ------------------------------------------------------------------
    # Track コンテキスト構築 / 注入
    # ------------------------------------------------------------------

    def build_track_context(self, track: ActionTrack) -> str:
        """Track 切替時に SAIMemory に注入する Track コンテキスト本文を組み立てる。

        この内容は <system>...</system> でラップされて user メッセージとして
        SAIMemory に書かれる。ペルソナにとっては「Track が切り替わって、こういう
        前提状況に入った」という会話の流れの中の system 通知として認識される。
        """
        title = track.title or "(無題)"
        track_id_short = track.track_id[:8] + "…"
        lines = [
            "## Track 切替通知",
            f"あなたは Track 「{title}」 (id={track_id_short}, type={track.track_type}) に入りました。",
            "",
            self.pulse_completion_notice,
            "",
            self.available_spells_doc,
        ]
        return "\n".join(lines)

    def _inject_track_context(self, persona_id: str, track: ActionTrack) -> None:
        """Track コンテキストを SAIMemory に user メッセージ (system タグ付き) として注入する。

        永続化することで:
        - 次回以降の _prepare_context が会話履歴の一部として取得する
        - メインラインのキャッシュは末尾追加のみ (Track 切替してもキャッシュ継続)
        - ペルソナは「Track 切替を会話の流れの中で受け取った」と認識できる
        """
        if self.manager is None:
            logging.warning(
                "[user-conv-handler] Cannot inject track context: manager is None"
            )
            return
        persona = self._lookup_persona(persona_id)
        if persona is None:
            logging.warning(
                "[user-conv-handler] Cannot inject track context: persona not found (%s)",
                persona_id,
            )
            return

        text = self.build_track_context(track)
        formatted = f"<system>{text}</system>"
        try:
            history_manager = getattr(persona, "history_manager", None)
            if history_manager is None:
                logging.warning(
                    "[user-conv-handler] Persona %s has no history_manager; "
                    "track context not injected",
                    persona_id,
                )
                return
            history_manager.add_to_persona_only({
                "role": "user",
                "content": formatted,
                "metadata": {"tags": ["conversation", "track_context"]},
            })
            logging.info(
                "[user-conv-handler] Injected track context for track=%s persona=%s "
                "(SAIMemory user message, tags=conversation+track_context)",
                track.track_id, persona_id,
            )
        except Exception:
            logging.exception(
                "[user-conv-handler] Failed to inject track context for track=%s",
                track.track_id,
            )

    def _lookup_persona(self, persona_id: str) -> Optional[Any]:
        personas = getattr(self.manager, "personas", None) or {}
        return personas.get(persona_id)

    # ------------------------------------------------------------------
    # Pulse 完了フック (v0.10)
    # ------------------------------------------------------------------

    def on_pulse_complete(
        self, persona_id: str, track: ActionTrack, pulse_outputs: Any
    ) -> None:
        """Pulse 完了時の処理。

        対ユーザー会話 Track は post_complete_behavior=wait_response なので、
        Pulse 完了 = ユーザー応答待ち状態に入る = 次の Pulse は外部イベント
        (ユーザー発話) 駆動でしか起動しない。
        本メソッドではアイドル化のための積極的処理は不要 (デフォルト挙動で済む)。
        ログ記録のみ行う。
        """
        logging.debug(
            "[user-conv-handler] on_pulse_complete: track=%s persona=%s "
            "(behavior=wait_response, awaiting user input)",
            track.track_id, persona_id,
        )

    # ------------------------------------------------------------------
    # イベント受け口
    # ------------------------------------------------------------------

    def on_user_utterance(
        self,
        persona_id: str,
        user_id: str,
        event: Dict[str, Any],
        invoke_main_line: MainLineInvoker,
    ) -> None:
        """ユーザー発話イベント。

        対ユーザー Track を取得 (なければ作成 + activate) し、その状態で分岐:

        - 新規作成された (= running 即遷移): Track コンテキスト注入 + メインライン起動
        - 既存 running (連続会話): メタレイヤー介入なし、注入もなし、メインライン起動のみ
        - それ以外 (pending / waiting / alert / unstarted):
          alert に遷移 → alert observer (MetaLayer) が同期実行され Track 切替判断
          → MetaLayer が activate して running に遷移していれば Track コンテキスト注入
          → メインライン起動

        Track コンテキスト注入は **「running への遷移」が発生したタイミングのみ** 行う
        ことで:
        - キャッシュの末尾追加だけになり Track 切替してもキャッシュは継続温まり続ける
        - ペルソナは「Track 切替を会話の流れの中で受け取った」と自然に認識できる
        - 連続会話では何も追加しない (肥大化しない)
        """
        track, was_newly_created = self.get_or_create_track(persona_id, user_id)

        if was_newly_created:
            # 新規作成 → 既に running、初回 Track コンテキスト注入
            logging.debug(
                "[user-conv-handler] Track %s newly created, injecting track context",
                track.track_id,
            )
            self._inject_track_context(persona_id, track)
        elif track.status == STATUS_RUNNING:
            # 既存 running → セッション継続、注入不要
            logging.debug(
                "[user-conv-handler] Track %s is running; direct main-line response",
                track.track_id,
            )
        else:
            logging.info(
                "[user-conv-handler] Track %s status=%s; raising alert for metalayer",
                track.track_id, track.status,
            )
            ctx = {
                "trigger": "user_utterance",
                "user_id": user_id,
                "event": event,
            }
            # set_alert は内部で alert observer (MetaLayer) を同期呼び出しする
            self.track_manager.set_alert(track.track_id, context=ctx)

            # MetaLayer 経由で Track が activate されて running になっていれば
            # コンテキスト注入を行う (running への遷移発生)
            updated = self.track_manager.get(track.track_id)
            if updated.status == STATUS_RUNNING:
                logging.info(
                    "[user-conv-handler] Track %s transitioned to running via metalayer; "
                    "injecting track context",
                    track.track_id,
                )
                self._inject_track_context(persona_id, updated)
            else:
                logging.info(
                    "[user-conv-handler] Track %s did not transition to running "
                    "(status=%s); no track context injection",
                    track.track_id, updated.status,
                )

        # メインライン応答は分岐によらず必ず起動する
        invoke_main_line()
