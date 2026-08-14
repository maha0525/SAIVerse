"""UserConversationTrackHandler: 対ユーザー Track のイベント振る舞いを定義する。

Intent A v0.9 / Intent B v0.6 における「対ユーザー会話 Track」(永続 Track) の
ユーザー発話イベントへの反応ルールを実装する。

責務:
- ユーザー発話を受けたときに対ユーザー Track の取得 / 自動作成
- Track が running ならメインライン応答を直接起動
- Track が running 以外でも、別の活動中 (開いている会話以外の出来事) でなければ
  直接 activate してメインライン応答を起動する (Idle への呼びかけは常に即応答。
  2026-07-07 改訂、pulse_dispatch.md §4.2)
- 別の活動中の場合のみ on_event 判断点へ直結し仲裁を委ねる
  (autonomy_wiring.handle_user_utterance_conflict。engage_now でなければ
  応答しない。旧 set_alert → MetaLayer 経路は track_retirement.md §7.4 で撤去)
- Track が running に **遷移したタイミング** で Track コンテキストを SAIMemory に
  注入する (Intent A v0.12 / 議論 2026-04-28: 末尾追加でキャッシュ親和、ペルソナの
  認知としては「Track 切替を会話の流れの中で受け取る」自然な順序を実現)

責務外:
- 仲裁の判断ロジックそのもの (on_event 判断点 = judgment_points / autonomy_wiring)
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
# 引数: 起点 Track の id (None なら SEA 側で get_running フォールバック)
MainLineInvoker = Callable[[Optional[str]], Any]


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

    # NOTE: かつてここに available_spells_doc (Track 操作スペルの一覧) があったが、
    # head pipeline の SpellListSection (sea/head_pipeline/sections/spell_list.py) が
    # システムプロンプトに「## スペル」セクションとして同じ内容を常時描画している。
    # Track 切替通知に重複して載せるのは無駄なので削除した (2026-06-13)。

    # v0.10 拡張: Pulse サイクル制御属性 (Intent B v0.10)
    # 対ユーザー会話 Track は応答待ち型なので、Pulse 連続実行の概念は適用されない。
    # ユーザー発話イベントが来たときだけ起動する。
    post_complete_behavior: str = "wait_response"  # 応答待ち型 (Pulse 完了後アイドル化)
    default_pulse_interval: int = 0  # 連続実行しない (応答駆動)
    default_subline_pulse_interval: int = 0
    # NOTE: default_max_consecutive_pulses は自律行動 v2 で概念ごと廃止
    # (intent §9.3。連続 Pulse 駆動が消え、予算はセッションのラウンド予算に置換)。

    # v0.11 拡張: 起点ライン種別 (Intent A v0.14, Intent B v0.11)
    # ユーザーへの応答は重量級モデルが書く (Intent A 不変条件 9: 他者会話は重量級)。
    # → 起点メインライン Pulse として動作し、メインキャッシュに会話履歴が積まれる。
    default_entry_line_role: str = "main_line"

    # Track 種別固有の context 指針。将来 Track Chronicle 周辺の context 注入候補。
    # 「相手の発話は審判ではなく対話の一部」を明示し、応答待ち姿勢を補強する。
    # NOTE: 旧 prepare_pulse_root_context は v0.32 (2026-05-09) で削除済み。属性は残置。
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
            # 既存 Track のタイトルが旧形式 (対 userN 会話) のままなら、
            # ユーザー名入りの新形式に貼り替える (自己修復)。
            self._heal_legacy_title(existing, user_id)
            return existing, False

        track_id = self.track_manager.create(
            persona_id=persona_id,
            track_type="user_conversation",
            title=self._make_title(user_id),
            is_persistent=True,
            output_target="building:current",
            metadata=json.dumps({"user_id": user_id}, ensure_ascii=False),
            initial_status=STATUS_RUNNING,
        )
        logging.info(
            "[user-conv-handler] Created user_conversation track as running "
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

    def _make_title(self, user_id: str) -> str:
        """対ユーザー Track のタイトルを組み立てる。

        ユーザー名 (User.USERNAME) を解決して「対 <名前>（id:<user_id>）会話」
        の形にする。名前を引けない場合は「対 ユーザー（id:<user_id>）会話」。
        """
        name = self._resolve_user_name(user_id)
        return f"対 {name}（id:{user_id}）会話"

    def _resolve_user_name(self, user_id: str) -> str:
        """user_id (USERID) から USERNAME を引く。引けなければ「ユーザー」。"""
        session_factory = getattr(self.manager, "SessionLocal", None) if self.manager else None
        if session_factory is None:
            return "ユーザー"
        db = session_factory()
        try:
            from database.models import User as UserModel
            user = db.query(UserModel).filter_by(USERID=int(user_id)).first()
            if user is not None and user.USERNAME:
                return user.USERNAME
        except (TypeError, ValueError):
            pass
        except Exception:
            logging.warning(
                "[user-conv-handler] Failed to resolve username for user_id=%s",
                user_id, exc_info=True,
            )
        finally:
            db.close()
        return "ユーザー"

    def _heal_legacy_title(self, track: ActionTrack, user_id: str) -> None:
        """旧形式タイトル (対 userN 会話) を新形式に貼り替える自己修復。

        新形式の生成と毎回付き合わせ、ズレていれば DB を更新して
        渡された detached track オブジェクトにも反映する。ユーザー名解決に
        失敗した (= フォールバック「ユーザー」) ときは貼り替えない
        — 名前が引けない瞬間に正しい名前を消してしまわないため。
        """
        desired = self._make_title(user_id)
        if track.title == desired:
            return
        if "（id:" not in desired:  # 念のため (理論上は常に含む)
            return
        # 名前解決に失敗したフォールバックタイトルでは上書きしない。
        if desired == f"対 ユーザー（id:{user_id}）会話":
            return
        self.track_manager.set_title(track.track_id, desired)
        track.title = desired
        logging.info(
            "[user-conv-handler] Healed legacy track title -> %r (track=%s)",
            desired, track.track_id,
        )

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
        sid = f"track:{track.short_id}" if track.short_id is not None else track.track_id[:8] + "…"
        lines = [
            "## Track 切替通知",
            f"あなたは Track 「{title}」 (id={sid}, type={track.track_type}) に入りました。",
            "",
            self.pulse_completion_notice,
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
            history_manager.add_to_persona_only(
                {
                    "role": "user",
                    "content": formatted,
                    "metadata": {"tags": ["conversation", "track_context"]},
                },
                origin_track_id=track.track_id,
            )
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
    # 会話の出来事 (Episode) の開き (life_concept_map.md §8.1)
    # ------------------------------------------------------------------

    def _open_conversation_episode(self, persona_id: str, track: ActionTrack) -> None:
        """kind='conversation' の出来事を開く (冪等 — 既に開いていれば no-op)。

        呼び出し点は「会話が running になった / running のままユーザーが話した」
        瞬間。閉じ (close) は wait_response タイムアウト
        (SAIVerseManager._wait_response_timeout_callback) が担う。
        出来事は記録であってペルソナの認知・行動には影響しない — 失敗しても
        会話経路を止めない (WARN のみ)。
        """
        if self.manager is None or getattr(self.manager, "SessionLocal", None) is None:
            return
        try:
            user_id: Optional[str] = None
            try:
                md = json.loads(track.track_metadata) if track.track_metadata else {}
                if isinstance(md, dict) and md.get("user_id") is not None:
                    user_id = str(md["user_id"])
            except (TypeError, ValueError):
                pass
            persona = self._lookup_persona(persona_id)
            building_id = getattr(persona, "current_building_id", None)
            participants = [persona_id] + ([user_id] if user_id else [])

            from saiverse.episodes import open_conversation_episode
            open_conversation_episode(
                self.manager,
                persona_id,
                building_id=building_id,
                participants=participants,
                origin_ref=f"track:{track.short_id}" if track.short_id is not None else None,
            )
        except Exception:
            logging.warning(
                "[user-conv-handler] Failed to open conversation episode "
                "(persona=%s track=%s) — conversation continues without it",
                persona_id, getattr(track, "track_id", "?"), exc_info=True,
            )

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
        """Track が activate (= running 遷移) されたときに呼ばれる hook。

        TrackManager.activate 末尾で全 track_activated observer に通知される。
        本 Handler は user_conversation 種別の Track のみ反応する。

        責務 (pulse_dispatch.md §5.2 + 段階 2 + 段階 3):
        - Track 切替通知の SAIMemory 注入 (``_inject_track_context``)
        - main_line Pulse 起動 (空 ``user_input``、building_histories 経由で
          ユーザー発話は ``auto_ingest_building_messages`` に取り込まれる)

        ``pulse_id`` は activate を起こした Pulse の ID (Pulse 外なら None)。
        deferred apply 経路で渡されてくる元のメタ判断 Pulse の id。

        ``suppress_pulse=True`` (サイレント activate) では main_line Pulse 起動を
        スキップする。ライフビューの停止パッケージが「待機に戻す」とき、停止
        ボタンを押した瞬間にペルソナが自動発言する事故を防ぐ。Track 切替通知の
        注入は行う — ペルソナの認知としては「ユーザー待ちに戻った」と知るべき
        (docs/intent/persona_activity_view.md §6.3)。
        """
        if track.track_type != "user_conversation":
            # 他種別の Track には反応しない (種別固有の振る舞いは各 Handler が担う)
            return
        logging.info(
            "[user-conv-handler] on_track_activated: track=%s persona=%s pulse=%s suppress_pulse=%s",
            track.track_id, persona_id, pulse_id, suppress_pulse,
        )
        # 会話の出来事 (Episode) を開く (冪等)。create(initial_status=running) と
        # activate の両経路がこの hook を通るため、running 遷移 = 会話開始の
        # 単一の開き点になる (life_concept_map.md §8.1)。
        self._open_conversation_episode(persona_id, track)
        # Track 切替通知を SAIMemory に注入する。これにより:
        # - ケース1 (ユーザー発話 → alert → metalayer → activate)
        # - ケース2 (自律 tick → metalayer → activate)
        # の両方で同じ経路で通知が出る (pulse_dispatch.md §5.3)
        self._inject_track_context(persona_id, track)

        if suppress_pulse:
            logging.info(
                "[user-conv-handler] suppress_pulse=True; skipping main_line pulse for track=%s",
                track.track_id,
            )
            return

        # main_line Pulse を起動する (pulse_dispatch.md §9.3 段階 3)。
        # ユーザー発話メッセージは別経路 (building_histories →
        # auto_ingest_building_messages) で取り込まれるため、user_input は
        # 空文字列で OK。track_user_conversation Playbook は {input} を
        # 参照しないので問題なし。
        self._start_main_line_pulse(persona_id, track)

    def _start_main_line_pulse(self, persona_id: str, track: ActionTrack) -> None:
        """activate された user_conversation Track 用に main_line Pulse を起動する。

        manager / persona / building_id が揃わないと起動できないので、
        欠けていれば WARN ログを出してスキップする (起動経路の堅牢性のため)。
        """
        if self.manager is None:
            logging.warning(
                "[user-conv-handler] Cannot start main_line pulse: manager is None (track=%s)",
                track.track_id,
            )
            return
        persona = self._lookup_persona(persona_id)
        if persona is None:
            logging.warning(
                "[user-conv-handler] Cannot start main_line pulse: persona not found (%s, track=%s)",
                persona_id, track.track_id,
            )
            return
        building_id = getattr(persona, "current_building_id", None)
        if not building_id:
            logging.warning(
                "[user-conv-handler] Cannot start main_line pulse: persona %s has no current_building_id (track=%s)",
                persona_id, track.track_id,
            )
            return
        run_sea_user = getattr(self.manager, "run_sea_user", None)
        if run_sea_user is None:
            logging.warning(
                "[user-conv-handler] Cannot start main_line pulse: manager.run_sea_user not available (track=%s)",
                track.track_id,
            )
            return
        try:
            # Pick up the active SSE event_callback (set by handle_user_input_stream).
            # This routes streaming events back to the user's open SSE so the
            # frontend can render the bubble. Without this, main_line pulses
            # triggered via on_track_activated would emit events into a void
            # (= wrapped_event_callback wraps None → silent no-op).
            sse_callbacks = getattr(self.manager, "_active_sse_callbacks", None)
            event_callback = sse_callbacks.get(building_id) if sse_callbacks else None
            logging.info(
                "[user-conv-handler] Starting main_line pulse: persona=%s building=%s track=%s event_callback=%s",
                persona_id, building_id, track.track_id, event_callback is not None,
            )
            run_sea_user(
                persona,
                building_id,
                "",  # user_input: 空文字列 (auto_ingest が building_histories から取り込む)
                event_callback=event_callback,
                origin_track_id=track.track_id,
            )
        except Exception:
            logging.exception(
                "[user-conv-handler] main_line pulse start failed: persona=%s track=%s",
                persona_id, track.track_id,
            )

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

        - running (新規作成 or 既存 running): Track 切替通知は不要 (新規作成時は
          ``on_track_activated`` hook が走って通知される)。直接メインライン起動。
        - それ以外 (pending / alert / unstarted) で、**別の活動中でない** (開いて
          いる会話以外の出来事が無い) 場合: 判断を経由せず ``TrackManager.activate``
          で直接 running 化する (2026-07-07 改訂: 衝突が無ければユーザー発話には
          常に即応答する)。activate により ``on_track_activated`` hook が Track
          切替通知の注入 + main_line Pulse 起動を担う。
        - それ以外で**別の活動中** (開いている出来事 ≠ 会話) の場合:
          on_event 判断点へ直結 (``handle_user_utterance_conflict``) → 判断が
          engage_now を選べば activate → ``on_track_activated`` hook 経由で
          Track 切替通知が注入されメインラインが起動する。engage_now 以外なら
          応答しない (track_retirement.md §7.4 の直結化。旧 set_alert →
          MetaLayer 経路は撤去)。

        Track 切替通知 (``_inject_track_context``) は ``on_track_activated`` hook
        経由に統一された (pulse_dispatch.md §5、段階 2)。本メソッドからは直接
        呼ばない。これによりケース1 (ユーザー発話起因) と ケース2 (自律 tick 起因)
        の両方で同じ経路で通知が出る。
        """
        track, was_newly_created = self.get_or_create_track(persona_id, user_id)

        if was_newly_created:
            # 直接経路 (1-A 新規作成): get_or_create_track 内の activate で
            # on_track_activated hook が発火し、main_line Pulse はそこで起動
            # される (Track 切替通知の注入も hook 内)。重複起動を避けるため
            # ここでは invoke_main_line を呼ばない。
            logging.debug(
                "[user-conv-handler] Track %s newly created; main_line started via on_track_activated hook",
                track.track_id,
            )
        elif track.status == STATUS_RUNNING:
            # 直接経路 (1-A 既存 running): activate は走らないので hook は発火
            # しない。invoke_main_line で直接 main_line Pulse を起動する。
            # 会話出来事の開きも冪等に通す — 前回タイムアウトで閉じた後も
            # Track が running のまま残る経路 (再起動直後等) で、発話 = 会話
            # 再開の瞬間に新しい出来事が開くようにするため。
            logging.debug(
                "[user-conv-handler] Track %s is running; direct main-line response",
                track.track_id,
            )
            self._open_conversation_episode(persona_id, track)
            try:
                invoke_main_line(track.track_id)
            finally:
                # wait_response timeout は一度発火すると消費される一回限りの予約。
                # life.md §7 案 Y では発火後も Track は running のままなので、
                # 二度目以降の会話は activate を通らず、ここで明示的に張り直さない
                # 限り新しく開いた conversation Episode が永久に閉じなくなる。
                # 同期応答後に呼ぶことで、provider が参照する最終メッセージ時刻を
                # 今回の応答まで進めた上でタイムアウトを再装填する。
                try:
                    self.track_manager.ensure_wait_response_timeout(persona_id)
                except Exception:
                    # タイマー再装填の失敗だけを理由に dispatcher 側で応答を再試行
                    # すると二重発話になり得る。応答本体の例外は外へ保ったまま、
                    # housekeeping の失敗はログへ残して吸収する。
                    logging.exception(
                        "[user-conv-handler] Failed to re-arm wait_response timeout "
                        "after direct response: track=%s persona=%s",
                        track.track_id,
                        persona_id,
                    )
        else:
            # running 以外 (pending / alert / unstarted)。「別の活動中か」で経路を
            # 分ける (track_retirement.md §7.4 の直結化。旧 running-Track 衝突判定は
            # 案 Y 以降 running が残留するため誤検知源だった):
            # - 別の活動中でない → 直接経路 (1-A'): 判断を経由せず直接 activate。
            #   ユーザーの呼びかけには常に応答する (2026-07-07 改訂)。
            # - 別の活動中 (開いている出来事 ≠ 会話) → 直結経路 (1-B'): on_event
            #   判断点へ直結し、engage_now のときだけ activate。
            busy_episode = self._get_open_non_conversation_episode(persona_id)
            if busy_episode is None:
                # 直接経路 (1-A'): 別の活動なし → 直接 activate。
                # TrackManager.activate 末尾で on_track_activated hook が走り、
                # Track 切替通知の注入 + 会話出来事の開き + main_line Pulse 起動
                # が行われる (invoke_main_line の直呼びで hook をバイパスしない)。
                logging.info(
                    "[user-conv-handler] Track %s status=%s; no open activity -> "
                    "direct activate (main_line starts via on_track_activated hook)",
                    track.track_id, track.status,
                )
                self.track_manager.activate(track.track_id)
                return
            # 直結経路 (1-B'): 別の活動中 → on_event 判断点で仲裁
            # (旧: set_alert → MetaLayer の v1 メタ判断。alert 状態は経由しない)。
            # engage_now なら activate → on_track_activated hook 経由で
            # Track 切替通知の注入と main_line Pulse 起動が行われる。
            # engage_now 以外なら応答しない (旧経路と同じ挙動)。
            logging.info(
                "[user-conv-handler] Track %s status=%s; open activity %s (%s) -> "
                "firing on_event judgment (no alert transition)",
                track.track_id, track.status,
                busy_episode.get("episode_ref") or busy_episode.get("episode_id"),
                busy_episode.get("kind"),
            )
            from saiverse.autonomy_wiring import handle_user_utterance_conflict

            route = handle_user_utterance_conflict(
                self.manager,
                persona_id,
                str(event.get("content") or ""),
                activate=lambda: self.track_manager.activate(track.track_id),
            )
            logging.info(
                "[user-conv-handler] utterance-conflict route=%s for track %s",
                route, track.track_id,
            )

    def _get_open_non_conversation_episode(
        self, persona_id: str
    ) -> Optional[Dict[str, Any]]:
        """「別の活動中か」= 開いている会話以外の出来事 (あればその dict)。

        「いま」の真実は開いている出来事が持つ (life.md §7 案 Y)。読めない環境
        (manager 不在のテスト等) や読み取り失敗は「活動なし」に倒す — 呼びかけへ
        の応答を機構の不備で黙らせない。

        判定は :func:`saiverse.episodes.get_open_non_conversation_episode` に
        一本化する — 「最後に開いた 1 件」を見て会話ならそこで打ち切る読み方だと、
        会話が作業より後に開いた並びで「別の活動中」を取りこぼす。
        """
        if self.manager is None:
            return None
        try:
            from saiverse import episodes

            return episodes.get_open_non_conversation_episode(self.manager, persona_id)
        except Exception:
            logging.warning(
                "[user-conv-handler] failed to read open episode for %s; "
                "treating as no open activity", persona_id, exc_info=True,
            )
        return None
