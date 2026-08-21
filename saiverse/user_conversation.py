"""ユーザーとの会話の入口 — Track を経由しない会話経路 (束 6 第三便、2026-08-21)。

Track は「ペルソナがやっていること」を全部入れる器として v1 で生まれ、会話も
その器に乗せられていた。撤廃計画 (docs/intent/track_retirement.md §2 住人 2)
の裁定どおり、会話の実体は次の三つに分解して持ち主へ返した:

- **いま会話中か** = 開いている ``kind='conversation'`` の出来事 (Episode)
- **応答する** = main_line Pulse (``manager.run_sea_user``)
- **会話の終わり** = 沈黙タイマー (本モジュールの ``arm_conversation_timeout``)

旧 ``UserConversationTrackHandler`` が持っていた仕事のうち、Track 固有だった
もの (Track 切替通知の SAIMemory 注入 / Track タイトルの生成と自己修復 /
``on_track_activated`` hook 経由の連鎖) は器ごと退役した。

責務:
- ユーザー発話イベントの受け口 (:func:`on_user_utterance`)
- 会話の開始 (出来事を開く → main_line 起動 → 沈黙タイマー装填)
- 沈黙タイマーの装填 / 解除 / 発火

責務外:
- 仲裁の判断ロジックそのもの (on_event 判断点 = judgment_points /
  ``autonomy_wiring.handle_user_utterance_conflict``)
- メインライン LLM 呼び出しの実装 (SEARuntime)
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import timedelta
from typing import Any, Callable, Dict, Optional

from saiverse import clock

LOGGER = logging.getLogger(__name__)

#: ``AI.USER_CONV_TIMEOUT_MINUTES`` が NULL のときの既定の沈黙時間 (分)。
DEFAULT_CONVERSATION_TIMEOUT_MINUTES = 30

#: :func:`start_conversation` が main_line Pulse へ転送してよい起動オプション。
#: ``handle_user_input_stream`` が継続発話用の closure に渡しているものと同じ顔ぶれ
#: にしてある — 初回発話だけ選択 Playbook や pre-spell が落ちる事故を防ぐため
#: (2026-08-21 Codex 指摘 5)。
PULSE_OPTION_KEYS = (
    "metadata",
    "meta_playbook",
    "args",
    "pre_spells",
    "event_callback",
)


class UserUtteranceError(RuntimeError):
    """ユーザー発話の受け口 (:func:`on_user_utterance`) の失敗。

    呼び出し元 (``PulseDispatcher.dispatch_user_utterance``) が「この失敗を
    直接応答で肩代わりしてよいか」を判断できるように、**どこまで実行したか**を
    例外自身が運ぶ。無条件フォールバックは、状態を変えた後の失敗で同じ発話を
    もう一度処理して二重応答になる (2026-08-21 Codex 指摘 4)。

    Attributes:
        stage: どの段で転んだか (ログ用の短い識別子)。
        side_effects_done: 応答の起動・台帳への書き込み・判断点の発火など、
            外から見える副作用を既に実行したか。True なら再実行は二重発話。
        fallback_safe: 呼び出し元が ``invoke_main_line()`` の直接応答で
            肩代わりしてよいか。**副作用が無くても False になりうる** —
            会話の出来事 (=「いま会話中か」の正典) を開けなかった失敗は、
            記録の無いまま応答を返すと仲裁・沈黙タイマー・スルースの判定が
            全部狂うので、黙って応答するより見えて失敗する方が正しい
            (Codex 指摘 6)。
    """

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        side_effects_done: bool,
        fallback_safe: bool,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.side_effects_done = side_effects_done
        self.fallback_safe = fallback_safe


def _timeout_key(persona_id: str) -> str:
    return f"conversation_timeout:{persona_id}"


def _lookup_persona(manager: Any, persona_id: str) -> Optional[Any]:
    personas = getattr(manager, "personas", None) or {}
    return personas.get(persona_id)


# ---------------------------------------------------------------------------
# 沈黙タイマーの予約世代トークン (Codex 指摘 2)
# ---------------------------------------------------------------------------
#
# EventScheduler は期限到来エントリを heap と ``_entries_by_key`` の**両方から
# 外してから**ロックの外で callback を実行する。その隙間にユーザー発話が同じ key
# へ再予約すると、``schedule()`` は取り消す相手を見つけられず、走り出した古い
# callback が「いま開いている会話」を無条件に閉じてしまう。
#
# そこで予約ごとに一意なトークンを発行し、callback は**自分が現行の予約である
# ことを照合できたときだけ**動く。照合値に乱数 nonce を使うのは repo の規律 —
# カウンタや時刻は 0 や既定値のような再到達可能な点を持ち、そこで破れる。
# 出来事の参照 (episode_ref) では足りない: 同じ会話に対するタイマー延長
# (再装填) も別世代として区別する必要があるため。

_TOKENS_ATTR = "_conversation_timeout_tokens"
_TOKENS_LOCK = threading.Lock()
#: manager へ属性を生やせない環境 (frozen オブジェクト等) 用の退避先。
_FALLBACK_TOKENS: Dict[str, str] = {}


def _timeout_tokens(manager: Any) -> Dict[str, str]:
    """予約世代トークンの置き場 (manager ごと)。``_TOKENS_LOCK`` 配下で呼ぶこと。"""
    if manager is None:
        return _FALLBACK_TOKENS
    tokens = getattr(manager, _TOKENS_ATTR, None)
    if not isinstance(tokens, dict):
        tokens = {}
        try:
            setattr(manager, _TOKENS_ATTR, tokens)
        except (AttributeError, TypeError):
            return _FALLBACK_TOKENS
    return tokens


def _consume_timeout_token(manager: Any, persona_id: str, token: str) -> bool:
    """``token`` が現行の予約なら消費して True。古い世代なら False (= no-op)。

    照合と消費を 1 手に畳む — 「照合してから消す」形だと、その隙間に入った
    再装填の世代まで一緒に消せてしまう。
    """
    with _TOKENS_LOCK:
        tokens = _timeout_tokens(manager)
        if tokens.get(persona_id) != token:
            return False
        tokens.pop(persona_id, None)
        return True


# ---------------------------------------------------------------------------
# 会話開始の排他 (Codex 指摘 3)
# ---------------------------------------------------------------------------
#
# 「開いている会話を探す → 無ければ開く」は検索と INSERT が別処理なので、同じ
# ペルソナへの同時発話が双方「未開」と判定して出来事と Pulse を二重に作れる。
# タイマーの key はペルソナ単位で 1 本に潰れるため、後から開いた行だけが閉じられ、
# 先行行はタイマーの無い開きっぱなしとして残る。
#
# 再入 (RLock) を許すのは、Pulse の内部から会話開始が再び呼ばれた場合に自分自身で
# 固まらないため。再入した側は再検査で「既に開いている」を見て相乗りする。

_START_LOCKS: Dict[str, threading.RLock] = {}
_START_LOCKS_GUARD = threading.Lock()


def _conversation_lock(persona_id: str) -> threading.RLock:
    with _START_LOCKS_GUARD:
        lock = _START_LOCKS.get(persona_id)
        if lock is None:
            lock = threading.RLock()
            _START_LOCKS[persona_id] = lock
        return lock


# ---------------------------------------------------------------------------
# 会話の出来事 (Episode)
# ---------------------------------------------------------------------------


def get_open_conversation(manager: Any, persona_id: str) -> Optional[Dict[str, Any]]:
    """開いている会話の出来事 (無ければ None)。読み取り失敗も None。

    「いま会話中か」の唯一の真実 (life.md §7 案 Y)。読めない環境 (manager 不在の
    テスト等) と読み取り失敗は「会話していない」に倒す — 呼びかけへの応答を
    機構の不備で黙らせないため (会話していない側に倒すと直接応答へ進む)。
    """
    if manager is None or getattr(manager, "SessionLocal", None) is None:
        return None
    try:
        from saiverse import episodes

        return episodes.get_open_episode(
            manager, persona_id, kind=episodes.KIND_CONVERSATION,
        )
    except Exception:
        LOGGER.warning(
            "[user-conv] failed to read the open conversation episode for %s; "
            "treating the persona as not in a conversation",
            persona_id, exc_info=True,
        )
        return None


def _open_conversation_episode(
    manager: Any, persona_id: str, user_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    """``kind='conversation'`` の出来事を開く (冪等 — 既に開いていれば no-op)。

    **開設の成功は main_line 起動の前提**。開けなかったら送出する (2026-08-21
    Codex 指摘 6)。出来事は単なる記録ではなく「いま会話中か」の正典なので、
    記録の無いまま応答を返すと、次の発話の経路判定・別行動中の仲裁・沈黙タイマー
    による終了・Metabolism のスルースが揃って狂う。黙って応答するより、見えて
    失敗する方が正しい。

    ⚠ 旧経路 (Track 時代の ``on_track_activated`` hook) はここを WARN で握り
    潰していたが、あの頃の「いま会話中か」は Track の status が持っており、
    出来事は付随的な記録にすぎなかった。正典の持ち主が変わったので、失敗の
    扱いも変わる。

    Returns:
        開いた (または既に開いていた) 出来事 dict。台帳そのものが無い環境
        (manager / SessionLocal 不在) では None。
    """
    if manager is None or getattr(manager, "SessionLocal", None) is None:
        # 台帳が「壊れている」のではなく「存在しない」環境。get_open_conversation も
        # 一貫して None を返すので、会話は毎回「開始」として一貫して扱われる。
        return None
    persona = _lookup_persona(manager, persona_id)
    building_id = getattr(persona, "current_building_id", None)
    participants = [persona_id] + ([str(user_id)] if user_id else [])

    from saiverse.episodes import open_conversation_episode

    return open_conversation_episode(
        manager,
        persona_id,
        building_id=building_id,
        participants=participants,
    )


def _get_open_non_conversation_episode(
    manager: Any, persona_id: str
) -> Optional[Dict[str, Any]]:
    """「別の活動中か」= 開いている会話以外の出来事 (あればその dict)。

    判定は :func:`saiverse.episodes.get_open_non_conversation_episode` に一本化
    する — 「最後に開いた 1 件」を見て会話ならそこで打ち切る読み方だと、会話が
    作業より後に開いた並びで「別の活動中」を取りこぼす。読めない環境や読み取り
    失敗は「活動なし」に倒す (呼びかけへの応答を機構の不備で黙らせない)。
    """
    if manager is None:
        return None
    try:
        from saiverse import episodes

        return episodes.get_open_non_conversation_episode(manager, persona_id)
    except Exception:
        LOGGER.warning(
            "[user-conv] failed to read the open episode for %s; "
            "treating as no open activity", persona_id, exc_info=True,
        )
    return None


# ---------------------------------------------------------------------------
# 沈黙タイマー (旧 wait_response timeout)
# ---------------------------------------------------------------------------


def conversation_timeout_minutes(manager: Any, persona_id: str) -> Optional[int]:
    """このペルソナの沈黙時間 (分)。タイマー対象外なら None。

    対象外になるのは:
    - デバッグ完全手動モードのペルソナ (debug_controller.md)
    - ペルソナが manager にロードされていない
    - ``AI.USER_CONV_TIMEOUT_MINUTES`` が 0 以下 (= タイマー無効化)

    ⚠ ``AUTONOMY_ENABLED`` はゲートにしない。会話の出来事の close がこの
    タイマーに乗っており、記録系は「認知不変・全ペルソナ」が原則
    (life_concept_map.md §8)。自律 OFF のまま会話が永遠に「いま」に残る実害を
    まはーが観測した (2026-07-07)。
    """
    manual = getattr(manager, "_debug_manual_mode_personas", None) or set()
    if persona_id in manual:
        return None
    if _lookup_persona(manager, persona_id) is None:
        return None

    minutes = DEFAULT_CONVERSATION_TIMEOUT_MINUTES
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is not None:
        try:
            from database.models import AI

            db = session_factory()
            try:
                row = db.query(AI).filter_by(AIID=persona_id).first()
                if row is not None and row.USER_CONV_TIMEOUT_MINUTES is not None:
                    minutes = int(row.USER_CONV_TIMEOUT_MINUTES)
            finally:
                db.close()
        except Exception:
            LOGGER.warning(
                "[user-conv] failed to read USER_CONV_TIMEOUT_MINUTES for %s; "
                "using the default %d",
                persona_id, DEFAULT_CONVERSATION_TIMEOUT_MINUTES, exc_info=True,
            )
    if minutes <= 0:
        return None
    return minutes


def arm_conversation_timeout(
    manager: Any, persona_id: str, *, only_if_absent: bool = False
) -> bool:
    """沈黙タイマーを ``now + N 分`` に張る (冪等 — 同じ key は上書き)。

    装填点は「会話に動きがあった瞬間」— 会話の開始と、ユーザー発話への同期応答
    の直後。どちらも基準は ``now`` で、旧実装の「最終メッセージ時刻を基準に
    する」は Track 紐付け (``messages.origin_track_id``) の退役で供給源ごと
    消えた。実質の挙動は変わらない — 旧実装も基準時刻が過去なら ``now`` へ
    丸めており、最終メッセージ時刻は常に過去だった。

    ⚠ ``now`` は :func:`saiverse.clock.now` から取る (実時刻ではない)。
    EventScheduler と DaySimulator が同じ時計を見ており、``datetime.now()`` で
    刻むと仮想日付のシミュレーション中に期限がシミュレーション終了後へ飛び、
    開いた会話の出来事が最後まで閉じない (2026-08-21 Codex 指摘 1)。

    予約には一意な世代トークンを添える。走り出した古い callback が新しい会話を
    閉じる競合を塞ぐため — 詳細はモジュール上部の「予約世代トークン」節。

    Args:
        only_if_absent: True なら**有効な予約が無いときだけ**張る。起動時の
            復旧 (失われた予約を埋める) 用。上書きすると、その間に通常経路が
            張った予約を潰して期限が後退する。

    Returns:
        張ったら True。対象外 / スケジューラ不在 / 既存予約を残した場合は False。
    """
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return False
    minutes = conversation_timeout_minutes(manager, persona_id)
    if minutes is None:
        return False

    fire_at = clock.now() + timedelta(minutes=int(minutes))
    key = _timeout_key(persona_id)
    token = uuid.uuid4().hex

    def _on_timeout(pid: str = persona_id, tok: str = token) -> None:
        if not _consume_timeout_token(manager, pid, tok):
            # この予約は既に別の予約に取って代わられている (再装填 / 解除)。
            # 走り出した後では EventScheduler 側から止められないので、ここで降りる。
            LOGGER.info(
                "[user-conv] a superseded conversation-timeout callback fired for "
                "%s; ignoring it (a newer reservation owns the conversation)", pid,
            )
            return
        handle_conversation_timeout(manager, pid)

    # 登録とトークンの発行を 1 つのロック区間に畳む。分けると、その隙間に入った
    # 別スレッドの予約のトークンを後から上書きして、生きている予約を no-op に
    # 変えてしまう。
    with _TOKENS_LOCK:
        if only_if_absent:
            # 判定と登録を EventScheduler のロック内で一息に行う。has_key で確認して
            # から schedule する形 (check-then-act) では、その隙間に通常経路が入れた
            # 予約を上書きしうる。
            armed = scheduler.schedule_if_absent(
                fire_at=fire_at, callback=_on_timeout, key=key,
            )
            if not armed:
                LOGGER.info(
                    "[user-conv] conversation timeout already armed for %s "
                    "(leaving the existing reservation untouched)", persona_id,
                )
                return False
        else:
            scheduler.schedule(fire_at=fire_at, callback=_on_timeout, key=key)
        _timeout_tokens(manager)[persona_id] = token

    LOGGER.info(
        "[user-conv] armed the conversation timeout: persona=%s timeout_min=%s "
        "fire_at=%s only_if_absent=%s",
        persona_id, minutes, fire_at.isoformat(timespec="seconds"), only_if_absent,
    )
    return True


def cancel_conversation_timeout(manager: Any, persona_id: str) -> None:
    """沈黙タイマーを解除する (会話の出来事を閉じたとき / 手動モード ON)。

    トークンを先に落とす — EventScheduler の cancel は「まだ heap に居る予約」に
    しか効かないので、既に走り出した callback はトークンの側でしか止められない。
    """
    with _TOKENS_LOCK:
        _timeout_tokens(manager).pop(persona_id, None)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return
    scheduler.cancel(_timeout_key(persona_id))


def rearm_conversation_timeout_on_load(manager: Any, persona_id: str) -> None:
    """起動時のタイマー再確立 (``_on_persona_registered``)。

    EventScheduler はインメモリなので、予約は再起動で失われる。開いている会話の
    出来事があるペルソナにだけ張り直す — 既に終わった会話へ張ると、起動 N 分後に
    タイムアウトが空撃ちされる (2026-07-29 実機で観測)。

    読み取りに失敗したときは「会話していない」に倒れる (:func:`get_open_conversation`
    の契約) ので張らない。旧実装が持っていた「判定不能なら読み取りを再試行する」
    経路は、会話が開いていれば次のユーザー発話が必ず装填し直す (Track 時代と違い
    仲裁経路を経ても最後は本モジュールが張る) ため不要になった。
    """
    if get_open_conversation(manager, persona_id) is None:
        return
    arm_conversation_timeout(manager, persona_id, only_if_absent=True)


def handle_conversation_timeout(
    manager: Any, persona_id: str, *, expected_episode_ref: Optional[str] = None
) -> None:
    """沈黙タイマーの発火 — 会話の出来事を閉じる帳簿処理。

    会話終了判断 (post_conversation) は 2026-08-16 の裁定で退役した
    (autonomous_behavior_v3.md §13.3)。ここに残るのは待ちを閉じる帳簿処理だけ。

    Args:
        expected_episode_ref: 呼び出し元が「この会話を終える」と決めた時点で
            見ていた会話の出来事。指定すると条件付き close になる (debug の
            切り上げのように、検証から実行までに間が空く経路で使う)。
    """
    try:
        from saiverse.autonomy_wiring import handle_conversation_end

        handle_conversation_end(
            manager, persona_id, expected_episode_ref=expected_episode_ref,
        )
    except Exception:
        LOGGER.exception(
            "[user-conv] conversation-end bookkeeping failed: persona=%s", persona_id,
        )

    # watchdog (AutonomyManager) の次回 tick を押し戻す (直後の watchdog と
    # 重ならないように)。
    try:
        autonomy_managers = getattr(manager, "_autonomy_managers", None) or {}
        am = autonomy_managers.get(persona_id)
        if am is not None:
            am.defer_next_tick()
    except Exception:
        LOGGER.exception(
            "[user-conv] defer_next_tick failed: persona=%s", persona_id,
        )


# ---------------------------------------------------------------------------
# 会話の開始 (旧 on_track_activated hook の連鎖の後継)
# ---------------------------------------------------------------------------


def start_conversation(
    manager: Any,
    persona_id: str,
    user_id: Optional[str],
    *,
    pulse_options: Optional[Dict[str, Any]] = None,
) -> bool:
    """会話を開始する: 出来事を開く → main_line Pulse → 沈黙タイマー装填。

    旧 ``TrackManager.activate`` → ``on_track_activated`` hook が連鎖させていた
    副作用のうち、Track なしでも要るもの**だけ**をここで直接行う。

    三手 (検索 → 開設 → Pulse とタイマー) はペルソナ単位のロックで原子化する。
    同時発話が双方「未開」と判定して出来事と Pulse を二重に作るのを防ぐため
    (2026-08-21 Codex 指摘 3)。競合に負けた側は既に開いた出来事へ相乗りし、
    Pulse を起こさずタイマーだけ張り直す — ユーザーの発話は
    ``auto_ingest_building_messages`` 経由で先行 Pulse の入力に含まれる。

    ユーザー発話メッセージは別経路 (building_histories →
    ``auto_ingest_building_messages``) で取り込まれるため、Pulse の ``user_input``
    は空文字列で良い。

    Args:
        pulse_options: main_line Pulse へ転送する起動オプション
            (:data:`PULSE_OPTION_KEYS`)。継続発話の closure が持っているものと
            同じ値を渡す — 渡さないと新規会話の初回発話だけ選択 Playbook・引数・
            pre-spell が落ちて挙動が変わる (Codex 指摘 5)。

    Returns:
        会話を開始したら True。既に開いていて相乗りしたら False。

    Raises:
        UserUtteranceError: 会話の出来事を開けなかったとき (stage=open_episode)。
    """
    with _conversation_lock(persona_id):
        # ロック内での再検査。ロックの外の判定は競合に対して何も保証しない。
        existing = get_open_conversation(manager, persona_id)
        if existing is not None:
            LOGGER.info(
                "[user-conv] conversation %s is already open for %s; riding along "
                "instead of opening a second one", existing.get("episode_ref"),
                persona_id,
            )
            _arm_quietly(manager, persona_id)
            return False

        try:
            _open_conversation_episode(manager, persona_id, user_id)
        except Exception as exc:
            LOGGER.exception(
                "[user-conv] failed to open the conversation episode (persona=%s); "
                "not starting the main_line pulse", persona_id,
            )
            raise UserUtteranceError(
                f"failed to open the conversation episode for {persona_id}: {exc}",
                stage="open_episode",
                side_effects_done=False,
                fallback_safe=False,
            ) from exc

        try:
            _start_main_line_pulse(manager, persona_id, pulse_options)
        finally:
            # 応答の成否に関わらず張る。ここで張り損ねると、開いた会話の出来事が
            # 永久に閉じない。
            _arm_quietly(manager, persona_id)
        return True


def _arm_quietly(manager: Any, persona_id: str) -> None:
    """沈黙タイマーを張り、失敗はログへ落とす (呼び出し元の結末を変えない)。"""
    try:
        arm_conversation_timeout(manager, persona_id)
    except Exception:
        LOGGER.exception(
            "[user-conv] failed to arm the conversation timeout: persona=%s",
            persona_id,
        )


def _start_main_line_pulse(
    manager: Any,
    persona_id: str,
    pulse_options: Optional[Dict[str, Any]] = None,
) -> None:
    """会話開始用の main_line Pulse を起動する。

    manager / persona / building_id が揃わないと起動できないので、欠けていれば
    WARN ログを出してスキップする (起動経路の堅牢性のため)。
    """
    if manager is None:
        LOGGER.warning(
            "[user-conv] cannot start the main_line pulse: manager is None "
            "(persona=%s)", persona_id,
        )
        return
    persona = _lookup_persona(manager, persona_id)
    if persona is None:
        LOGGER.warning(
            "[user-conv] cannot start the main_line pulse: persona not found (%s)",
            persona_id,
        )
        return
    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        LOGGER.warning(
            "[user-conv] cannot start the main_line pulse: persona %s has no "
            "current_building_id", persona_id,
        )
        return
    run_sea_user = getattr(manager, "run_sea_user", None)
    if run_sea_user is None:
        LOGGER.warning(
            "[user-conv] cannot start the main_line pulse: manager.run_sea_user "
            "is not available (persona=%s)", persona_id,
        )
        return

    options = dict(pulse_options or {})
    unknown = sorted(set(options) - set(PULSE_OPTION_KEYS))
    if unknown:
        LOGGER.warning(
            "[user-conv] dropping unknown pulse options %s (persona=%s)",
            unknown, persona_id,
        )
        for name in unknown:
            options.pop(name, None)
    event_callback = options.pop("event_callback", None)

    try:
        if event_callback is None:
            # いま開いている SSE の event_callback を拾う (handle_user_input_stream が
            # 登録する)。これが無いと、ここから起動した Pulse のイベントは虚空へ流れ、
            # フロントに吹き出しが出ない。
            sse_callbacks = getattr(manager, "_active_sse_callbacks", None)
            event_callback = sse_callbacks.get(building_id) if sse_callbacks else None
        LOGGER.info(
            "[user-conv] starting the main_line pulse: persona=%s building=%s "
            "event_callback=%s options=%s",
            persona_id, building_id, event_callback is not None, sorted(options),
        )
        run_sea_user(
            persona,
            building_id,
            "",  # user_input: 空文字列 (auto_ingest が building_histories から取り込む)
            event_callback=event_callback,
            **options,
        )
    except Exception:
        LOGGER.exception(
            "[user-conv] main_line pulse start failed: persona=%s", persona_id,
        )


# ---------------------------------------------------------------------------
# イベント受け口
# ---------------------------------------------------------------------------


def on_user_utterance(
    manager: Any,
    persona_id: str,
    user_id: str,
    event: Dict[str, Any],
    invoke_main_line: Callable[[], Any],
    *,
    pulse_options: Optional[Dict[str, Any]] = None,
) -> None:
    """ユーザー発話イベント。

    経路は「会話が開いているか」で分かれる:

    - **開いている** (会話継続): 直接メインラインを起動する
      (``invoke_main_line``)。応答後に沈黙タイマーを張り直す。
    - **開いていない かつ 別の活動中でない**: :func:`start_conversation` で
      会話を開始する (ユーザーの呼びかけには常に即応答 — 2026-07-07 改訂)。
    - **開いていない かつ 別の活動中** (開いている出来事 ≠ 会話): on_event 判断点
      へ直結し仲裁を委ねる (``autonomy_wiring.handle_user_utterance_conflict``)。
      判断が engage_now を選べば :func:`start_conversation`、選ばなければ応答
      しない (track_retirement.md §7.4 の直結化)。

    旧実装は 1 段目の判定に「対ユーザー会話 Track が running か」を使っていた。
    案 Y (life.md §7) 以降その running は会話が終わっても残るため、仲裁は事実上
    「初回の発話」と「ゲーム参加で押し出された後」でしか発火しなかった。会話の
    器を出来事へ移した本便で、判定は §7.4 が設計どおりに書いていた
    「開いている出来事 ≠ 会話」に揃う。

    Args:
        pulse_options: 会話開始経路で main_line Pulse へ転送する起動オプション
            (:data:`PULSE_OPTION_KEYS`)。``invoke_main_line`` の closure が抱えて
            いるものと同じ値を渡すこと — 初回発話だけオプションが落ちると、
            継続発話と挙動が変わる (Codex 指摘 5)。

    Raises:
        UserUtteranceError: 失敗の性質 (副作用の有無 / 直接応答で肩代わりして
            よいか) を載せて送出する。無条件フォールバックは二重応答を作る。
    """
    open_conversation = get_open_conversation(manager, persona_id)

    if open_conversation is not None:
        LOGGER.debug(
            "[user-conv] conversation %s is open for %s; direct main-line response",
            open_conversation.get("episode_ref"), persona_id,
        )
        try:
            invoke_main_line()
        except Exception as exc:
            raise UserUtteranceError(
                f"the direct main-line response failed for {persona_id}: {exc}",
                stage="direct_response",
                # 応答は起動済み。同じ発話をもう一度処理すると二重発話になる。
                side_effects_done=True,
                fallback_safe=False,
            ) from exc
        finally:
            # 沈黙タイマーは一度発火すると消費される一回限りの予約。同期応答の
            # 後に張り直さないと、二度目以降の会話で出来事が永久に閉じなくなる。
            # タイマー再装填の失敗だけを理由に dispatcher 側で応答を再試行すると
            # 二重発話になり得るので、応答本体の例外は外へ保ったまま housekeeping
            # の失敗はログへ残して吸収する。
            _arm_quietly(manager, persona_id)
        return

    busy_episode = _get_open_non_conversation_episode(manager, persona_id)
    if busy_episode is None:
        LOGGER.info(
            "[user-conv] no open conversation and no open activity for %s -> "
            "starting the conversation directly", persona_id,
        )
        start_conversation(
            manager, persona_id, user_id, pulse_options=pulse_options,
        )
        return

    LOGGER.info(
        "[user-conv] no open conversation but activity %s (%s) is open for %s -> "
        "firing the on_event judgment",
        busy_episode.get("episode_ref") or busy_episode.get("episode_id"),
        busy_episode.get("kind"), persona_id,
    )
    from saiverse.autonomy_wiring import handle_user_utterance_conflict

    # 仲裁が engage_now を選んだ瞬間から「応答を起こした」側に回る。フラグは
    # start_conversation の**手前**で立てる — 開始の途中で転んだ場合も、直接応答で
    # 肩代わりすると二重に走りうるため。
    engaged = {"started": False}

    def _engage() -> None:
        engaged["started"] = True
        start_conversation(
            manager, persona_id, user_id, pulse_options=pulse_options,
        )

    try:
        route = handle_user_utterance_conflict(
            manager,
            persona_id,
            str(event.get("content") or ""),
            engage=_engage,
            user_id=user_id,
        )
    except UserUtteranceError:
        # 会話開始側の分類 (台帳の開設失敗など) をそのまま上へ通す。
        raise
    except Exception as exc:
        raise UserUtteranceError(
            f"the utterance-conflict judgment failed for {persona_id}: {exc}",
            stage="utterance_conflict",
            side_effects_done=engaged["started"],
            fallback_safe=not engaged["started"],
        ) from exc
    LOGGER.info(
        "[user-conv] utterance-conflict route=%s for persona %s", route, persona_id,
    )
