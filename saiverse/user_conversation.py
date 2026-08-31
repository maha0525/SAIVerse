"""ユーザーとの会話の入口 — Track も出来事も経由しない会話経路。

Track は「ペルソナがやっていること」を全部入れる器として v1 で生まれ、会話も
その器に乗せられていた。撤廃計画 (docs/intent/track_retirement.md §2 住人 2)
の裁定で会話は Track から降り (2026-08-21、束 6 第三便)、続く束 6c
(2026-08-22) で **出来事 (Episode) というテーブルの行からも降りた**
(autonomous_behavior_v3.md §7「エピソードという専用の記録行は持たない」)。

会話の実体は三つに分解して持ち主へ返してある:

- **いま会話中か** = **メモリ内の会話状態** (ペルソナ単位。本モジュールが持つ)
- **応答する** = main_line Pulse (``manager.run_sea_user``)
- **会話の終わり** = 沈黙タイマー (本モジュールの ``arm_conversation_timeout``)

**始まりと終わりはどこにも記録しない。** 会話に終わりの合図は実在しないので、
実在しない区切りを記録に書くとどう置いても捏造になる (docs/intent/episode.md の
不変条件)。2026-08-22 の束 6c は始まり / 終わりを機構名義の 1 行として Building
ログへ書いていたが、その行がペルソナの記憶へ写って文脈を汚していたため、
2026-08-23 のまはー裁定で機構ごと撤去した (代替の記録も作らない)。できごと UI
(v0.4) が区間を要るときは、発言の時刻と沈黙の幅から導出する。

**再起動でメモリ内状態は消える = 「会話していない」に一貫して倒れる。** これは
欠陥ではなく設計 (v3 §9-9): 会話の束ねは表示時に ``conversation`` タグの範囲から
導出するので、状態が消えても記録は失われない。旧実装 (出来事の行) は逆に、閉じ
損ねた行が再起動を跨いで「永遠に会話中」として残る事故を持っていた。

責務:
- ユーザー発話イベントの受け口 (:func:`on_user_utterance`)
- 会話の開始 (会話状態を立てる → main_line 起動 → 沈黙タイマー装填)
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
from typing import Any, Callable, Dict, List, Optional

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
# 沈黙タイマーの予約 — 世代トークン + 会話の身元 (Codex 指摘 2 / 2026-08-22 指摘 1)
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
# 会話の識別子だけでは足りない: 同じ会話に対するタイマー延長 (再装填) も別世代
# として区別する必要があるため。
#
# ⚠ トークンだけでも足りない。トークンの照合が通ってから終了処理が状態を落とす
# までの間に、別スレッドが会話 A を閉じて会話 B を開くと、古い callback は
# **通ったトークンを手に**新しい会話 B を閉じてしまう。そこで予約は
# 「世代トークン + そのとき開いていた会話の識別子」の組で持ち、
#
#   - 発火した callback は :func:`handle_conversation_timeout` へ会話の識別子を
#     ``expected_conversation_id`` として渡す (条件付き解除になる)
#   - 予約の解除も「その会話の予約であるときだけ」効く
#     (:func:`cancel_conversation_timeout` の ``expected_conversation_id``)
#
# の二段で、古い世代が新しい会話に触れる経路を塞ぐ。

_RESERVATIONS_ATTR = "_conversation_timeout_reservations"
_TOKENS_LOCK = threading.Lock()
#: manager へ属性を生やせない環境 (frozen オブジェクト等) 用の退避先。
_FALLBACK_RESERVATIONS: Dict[str, Dict[str, Any]] = {}


def _timeout_reservations(manager: Any) -> Dict[str, Dict[str, Any]]:
    """沈黙タイマー予約の置き場 (manager ごと)。``_TOKENS_LOCK`` 配下で呼ぶこと。

    値は ``{"token": str, "conversation_id": str | None}``。
    """
    if manager is None:
        return _FALLBACK_RESERVATIONS
    reservations = getattr(manager, _RESERVATIONS_ATTR, None)
    if not isinstance(reservations, dict):
        reservations = {}
        try:
            setattr(manager, _RESERVATIONS_ATTR, reservations)
        except (AttributeError, TypeError):
            return _FALLBACK_RESERVATIONS
    return reservations


def _consume_timeout_reservation(
    manager: Any, persona_id: str, token: str, conversation_id: Optional[str]
) -> bool:
    """``token`` が現行の予約なら消費して True。古い世代なら False (= no-op)。

    照合と消費を 1 手に畳む — 「照合してから消す」形だと、その隙間に入った
    再装填の世代まで一緒に消せてしまう。会話の識別子も併せて照合する: 予約は
    「どの会話の沈黙を見張っているか」まで含めて 1 つの世代なので、片方でも
    違えばこの callback は現行ではない。
    """
    with _TOKENS_LOCK:
        reservations = _timeout_reservations(manager)
        current = reservations.get(persona_id)
        if current is None:
            return False
        if current.get("token") != token:
            return False
        if current.get("conversation_id") != conversation_id:
            return False
        reservations.pop(persona_id, None)
        return True


# ---------------------------------------------------------------------------
# 会話の開始と終了の排他 (Codex 指摘 3 / 2026-08-22 指摘 1)
# ---------------------------------------------------------------------------
#
# 「開いている会話を探す → 無ければ開く」は検索と登録が別処理なので、同じ
# ペルソナへの同時発話が双方「未開」と判定して会話と Pulse を二重に作れる。
# タイマーの key はペルソナ単位で 1 本に潰れるため、後から開いた会話だけが
# 閉じられ、先行分はタイマーの無い開きっぱなしとして残る。
#
# **終了処理も同じロックで直列化する** (``autonomy_wiring.handle_conversation_end``
# がこのロックを取る)。開始と終了が別々のロックだと、「会話 A を閉じる」処理の
# 途中で会話 B が開き、A の後片付け (予約の解除) が B の予約を消す並びが残る。
#
# ⚠ ロックは**応答 (Pulse) を含まない**。Pulse は LLM 呼び出しを含む長い処理で、
# それをロックの中に置くと、EventScheduler の dispatch スレッド (沈黙タイマーの
# callback を同期実行する) が LLM の後ろで待たされ、他ペルソナの予約まで止まる。
# ロックが原子化する必要があるのは「開いているか調べる → 状態を立てる → 予約を
# 張る」までで、そこを抜けた後の相乗り判定はロック無しでも正しく効く
# (状態は既に立っている)。
#
# 再入 (RLock) を許すのは、Pulse の内部から会話開始が再び呼ばれた場合に自分自身で
# 固まらないため。再入した側は再検査で「既に開いている」を見て相乗りする。

_START_LOCKS: Dict[str, threading.RLock] = {}
_START_LOCKS_GUARD = threading.Lock()


def conversation_lock(persona_id: str) -> threading.RLock:
    """このペルソナの会話の開始 / 終了を直列化する再入可能ロック。

    ``autonomy_wiring.handle_conversation_end`` も同じロックを取る (終了処理を
    開始処理と直列化するため)。
    """
    with _START_LOCKS_GUARD:
        lock = _START_LOCKS.get(persona_id)
        if lock is None:
            lock = threading.RLock()
            _START_LOCKS[persona_id] = lock
        return lock


# ---------------------------------------------------------------------------
# 会話状態 (メモリ内) — 「いま会話中か」の正典
# ---------------------------------------------------------------------------
#
# 旧実装は ``episodes`` テーブルの open な ``kind='conversation'`` 行だった。
# v3 §7 の裁定でエピソードという専用の記録行を持たなくなったので、状態はプロセス
# 内のメモリだけが持つ。**始まりと終わりはどこにも記録しない** (2026-08-23 裁定。
# 会話に区切りは保存しない = episode.md の不変条件)。
#
# キャッシュではなく正典なので、DB フォールバックは持たない。再起動で状態が
# 消えるのは設計どおり — 「会話していない」に一貫して倒れる (v3 §9-9)。
#
# 置き場を manager 属性にするのは open-episode キャッシュ (旧 episodes.py) と
# 同じ理由: テストが別々の in-memory DB を持つ複数 manager を同一プロセスで作る
# ため、モジュールグローバルだと persona_id 衝突で漏れる。

_STATE_ATTR = "_open_conversation_state"
_STATE_LOCK = threading.Lock()
#: manager へ属性を生やせない環境 (frozen オブジェクト等) 用の退避先。
_FALLBACK_STATE: Dict[str, Dict[str, Any]] = {}


def _state_map(manager: Any) -> Dict[str, Dict[str, Any]]:
    """会話状態の置き場 (manager ごと)。``_STATE_LOCK`` 配下で呼ぶこと。"""
    if manager is None:
        return _FALLBACK_STATE
    state = getattr(manager, _STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        try:
            setattr(manager, _STATE_ATTR, state)
        except (AttributeError, TypeError):
            return _FALLBACK_STATE
    return state


def get_open_conversation(manager: Any, persona_id: str) -> Optional[Dict[str, Any]]:
    """いま開いている会話 (無ければ None)。

    「いま会話中か」の唯一の真実。メモリ内の読み出しなので失敗しようがない —
    旧実装の「読み取り失敗は会話していない側に倒す」フォールバックは対象消滅
    した (倒し方の意図は生きている: 状態が無ければ直接応答へ進み、呼びかけを
    機構の不備で黙らせない)。

    Returns:
        ``{"conversation_id", "persona_id", "building_id", "participants",
        "started_at"}`` の dict。``started_at`` は epoch 秒 (仮想クロック基準)。
    """
    if not persona_id:
        return None
    with _STATE_LOCK:
        return _state_map(manager).get(persona_id)


def _set_open_conversation(
    manager: Any,
    persona_id: str,
    *,
    building_id: Optional[str],
    participants: List[str],
) -> Dict[str, Any]:
    """会話状態を立てて、その dict を返す (呼び出し元がロック内で使う)。"""
    state = {
        "conversation_id": f"conv:{uuid.uuid4().hex}",
        "persona_id": persona_id,
        "building_id": building_id,
        "participants": list(participants),
        "started_at": int(clock.now().timestamp()),
    }
    with _STATE_LOCK:
        _state_map(manager)[persona_id] = state
    return state


def clear_open_conversation(
    manager: Any, persona_id: str, *, expected_conversation_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """会話状態を落として、落とした状態を返す (無ければ None)。

    Args:
        expected_conversation_id: 指定すると**その会話が現行のときだけ**落とす
            条件付き解除になる。照合と解除は 1 手 (ロック内) — 「読んで照合して
            から落とす」形だと、その隙間に別経路が会話を閉じ、新しい会話を開いた
            場合に**別の会話**を落とす (2026-08-14 Codex 三巡目の規律を器ごと
            引き継ぐ)。
    """
    with _STATE_LOCK:
        state = _state_map(manager)
        current = state.get(persona_id)
        if current is None:
            return None
        if (
            expected_conversation_id is not None
            and current.get("conversation_id") != expected_conversation_id
        ):
            return None
        return state.pop(persona_id, None)


def _get_open_non_conversation_episode(
    manager: Any, persona_id: str
) -> Optional[Dict[str, Any]]:
    """「別の活動中か」— v0.3 では常に None (仲裁は直接応答へ縮退)。

    旧実装は「開いている会話以外の出来事」を引いていたが、束 6c で出来事の
    書き手が全滅した (v3 §7) ので、**新しい活動の行はどこからも生まれない**。
    既存 DB に残る旧 open 行を読むと、退役した機構の残骸を理由に仲裁が永久に
    発火し続けるので、読みごと止める。

    関数を残すのは、v0.4 のティック設計が「別の活動中か」の答えを作り直す口が
    ここだから (autonomous_behavior_v3.md §9-3: 並走の組を数え上げて防ぐ / 耐える
    を決める工事)。それまでは「活動なし」= 呼びかけには常に直接応答する。
    """
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

    装填点は「会話に動きがあった瞬間」— 会話の開始と、ユーザー発話の**到着時**、
    およびその同期応答の**直後**。どちらも基準は ``now`` で、旧実装の「最終
    メッセージ時刻を基準にする」は Track 紐付け (``messages.origin_track_id``)
    の退役で供給源ごと消えた。実質の挙動は変わらない — 旧実装も基準時刻が過去なら
    ``now`` へ丸めており、最終メッセージ時刻は常に過去だった。

    ⚠ **到着時の装填を省いてはいけない。** 応答の直後だけに張ると、沈黙の終わり際
    に来た発話への応答中に前回の予約が発火し、ユーザーがまさに話している会話が
    閉じられる (2026-08-22 発見)。会話 ID が変わらないので、
    :func:`cancel_conversation_timeout` の照合 (会話 ID のみ) では「古い世代」と
    「応答後に張り直した生きた予約」を区別できず、後者まで巻き添えで消える。

    ⚠ ``now`` は :func:`saiverse.clock.now` から取る (実時刻ではない)。
    EventScheduler と DaySimulator が同じ時計を見ており、``datetime.now()`` で
    刻むと仮想日付のシミュレーション中に期限がシミュレーション終了後へ飛び、
    開いた会話の出来事が最後まで閉じない (2026-08-21 Codex 指摘 1)。

    予約には一意な世代トークンと、**そのとき開いていた会話の識別子**を添える。
    走り出した古い callback が新しい会話を閉じる競合を塞ぐため — 詳細はモジュール
    上部の「沈黙タイマーの予約」節。会話が開いていない状態で張られた予約は
    見張る相手を持たないので、発火しても何も閉じない (誰かの会話を巻き込まない)。

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

    # 「いま開いている会話を読む → 予約に焼き付ける」を会話ロックの中で行う。
    # 外で読むと、読んだ直後に会話が入れ替わった予約が生まれる。
    with conversation_lock(persona_id):
        open_conversation = get_open_conversation(manager, persona_id)
        conversation_id = (
            open_conversation.get("conversation_id")
            if open_conversation is not None else None
        )
        fire_at = clock.now() + timedelta(minutes=int(minutes))
        key = _timeout_key(persona_id)
        token = uuid.uuid4().hex

        def _on_timeout(
            pid: str = persona_id,
            tok: str = token,
            cid: Optional[str] = conversation_id,
        ) -> None:
            if not _consume_timeout_reservation(manager, pid, tok, cid):
                # この予約は既に別の予約に取って代わられている (再装填 / 解除)。
                # 走り出した後では EventScheduler 側から止められないので、ここで降りる。
                LOGGER.info(
                    "[user-conv] a superseded conversation-timeout callback fired "
                    "for %s; ignoring it (a newer reservation owns the "
                    "conversation)", pid,
                )
                return
            if cid is None:
                # 会話が開いていないときに張られた予約。閉じる相手を持たないので、
                # 「いま開いている会話」を掴んで閉じてはいけない。
                LOGGER.warning(
                    "[user-conv] a conversation-timeout callback fired for %s but "
                    "it was armed without an open conversation; closing nothing",
                    pid,
                )
                return
            handle_conversation_timeout(manager, pid, expected_conversation_id=cid)

        # 登録と予約の記帳を 1 つのロック区間に畳む。分けると、その隙間に入った
        # 別スレッドの予約を後から上書きして、生きている予約を no-op に変えてしまう。
        with _TOKENS_LOCK:
            if only_if_absent:
                # 判定と登録を EventScheduler のロック内で一息に行う。has_key で
                # 確認してから schedule する形 (check-then-act) では、その隙間に
                # 通常経路が入れた予約を上書きしうる。
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
            _timeout_reservations(manager)[persona_id] = {
                "token": token, "conversation_id": conversation_id,
            }

    LOGGER.info(
        "[user-conv] armed the conversation timeout: persona=%s conversation=%s "
        "timeout_min=%s fire_at=%s only_if_absent=%s",
        persona_id, conversation_id, minutes,
        fire_at.isoformat(timespec="seconds"), only_if_absent,
    )
    return True


def cancel_conversation_timeout(
    manager: Any, persona_id: str, *, expected_conversation_id: Optional[str] = None
) -> bool:
    """沈黙タイマーを解除する (会話を閉じたとき / 手動モード ON)。

    予約の記帳を先に落とす — EventScheduler の cancel は「まだ heap に居る予約」に
    しか効かないので、既に走り出した callback は記帳の側でしか止められない。

    Args:
        expected_conversation_id: 指定すると**その会話のために張られた予約だけ**を
            解除する条件付きの解除になる。会話 A を閉じた後片付けが、その間に
            開いた会話 B の予約を消してしまう並びを塞ぐため — B は自前の予約を
            持っており、A の後片付けはそれに触れてはいけない。照合と解除は 1 手
            (同じロック区間) で行う。None なら無条件 (手動モード ON のように
            「このペルソナのタイマーを止める」意図の経路)。

    Returns:
        解除したら True。対象の予約が無い / 別の会話の予約だったら False。
    """
    with _TOKENS_LOCK:
        reservations = _timeout_reservations(manager)
        current = reservations.get(persona_id)
        if expected_conversation_id is not None and (
            current is None
            or current.get("conversation_id") != expected_conversation_id
        ):
            # 記帳が無い = この予約は既に発火して消費されている (解除する相手が
            # 居ない)。記帳が別の会話 = その予約は新しい会話のもので、閉じた側の
            # 後片付けが触れてよい相手ではない。どちらも「何もしない」が正しい。
            LOGGER.info(
                "[user-conv] leaving the conversation timeout of %s alone: the "
                "live reservation belongs to %s, not to %s",
                persona_id,
                current.get("conversation_id") if current else None,
                expected_conversation_id,
            )
            return False
        reservations.pop(persona_id, None)
        scheduler = getattr(manager, "event_scheduler", None)
        if scheduler is not None:
            scheduler.cancel(_timeout_key(persona_id))
    return True


def rearm_conversation_timeout_on_load(manager: Any, persona_id: str) -> None:
    """起動時のタイマー再確立 (``_on_persona_registered``)。

    ⚠ **v0.3 (束 6c) 以降、ここは常に no-op で終わる。** 会話状態はプロセス内の
    メモリが持つので、再起動した時点でどのペルソナも「会話していない」— 張り直す
    べき待ちが構造上存在しない (v3 §9-9)。

    関数を残すのは配線 (``_on_persona_registered``) の口としてで、旧実装が防いで
    いた事故 (終わった会話へ張り直して起動 N 分後に空撃ち、2026-07-29 実機で観測)
    は、供給源ごと消えたことで再発しない。
    """
    if get_open_conversation(manager, persona_id) is None:
        return
    arm_conversation_timeout(manager, persona_id, only_if_absent=True)


def handle_conversation_timeout(
    manager: Any, persona_id: str, *, expected_conversation_id: Optional[str] = None
) -> None:
    """沈黙タイマーの発火 — 会話状態を落とす帳簿処理。

    会話終了判断 (post_conversation) は 2026-08-16 の裁定で退役した
    (autonomous_behavior_v3.md §13.3)。ここに残るのは待ちを閉じる帳簿処理だけ。

    帳簿処理の本体 (``autonomy_wiring.handle_conversation_end``) は会話ロックを
    取り、会話の開始と直列化される。

    Args:
        expected_conversation_id: 呼び出し元が「この会話を終える」と決めた時点で
            見ていた会話。指定すると条件付き解除になる。**沈黙タイマーの通常発火
            も指定する** — 予約が張られた時点で開いていた会話を焼き付けてあり、
            走り出した後に会話が入れ替わっても別の会話を閉じないため
            (2026-08-22 指摘 1)。debug の切り上げのように、検証から実行までに
            間が空く経路も同じ器を使う。
    """
    try:
        from saiverse.autonomy_wiring import handle_conversation_end

        handle_conversation_end(
            manager, persona_id, expected_conversation_id=expected_conversation_id,
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
    """会話を開始する: 会話状態 → main_line Pulse → 沈黙タイマー。

    旧 ``TrackManager.activate`` → ``on_track_activated`` hook が連鎖させていた
    副作用のうち、Track なしでも要るもの**だけ**をここで直接行う。会話の器は
    2026-08-21 に Track から出来事の行へ、2026-08-22 (束 6c) に出来事の行から
    メモリ内状態へ移った (v3 §7) — 意味論は据え置きで器だけが変わる。始まりを
    記録に残さないのは 2026-08-23 の裁定 (会話に区切りは保存しない)。

    三手 (検査 → 状態を立てる → タイマー) はペルソナ単位のロックで原子化する。
    同時発話が双方「未開」と判定して会話と Pulse を二重に作るのを
    防ぐため (2026-08-21 Codex 指摘 3)。競合に負けた側は既に開いた会話へ相乗りし、
    Pulse を起こさずタイマーだけ張り直す — ユーザーの発話は
    ``auto_ingest_building_messages`` 経由で先行 Pulse の入力に含まれる。

    **応答 (Pulse) はロックを抜けてから起こす。** 同じロックを終了処理も取る
    ようになった (2026-08-22 指摘 1) ため、Pulse をロックの中に置くと、沈黙
    タイマーの callback を同期実行する EventScheduler の dispatch スレッドが
    LLM 呼び出しの後ろで待たされる。二重 Pulse の封じはロックの中で状態を
    立て切ることで既に効いており、後から来た発話はロックを抜けた状態を見て
    相乗りする。

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
    """
    with conversation_lock(persona_id):
        # ロック内での再検査。ロックの外の判定は競合に対して何も保証しない。
        existing = get_open_conversation(manager, persona_id)
        if existing is not None:
            LOGGER.info(
                "[user-conv] conversation %s is already open for %s; riding along "
                "instead of opening a second one", existing.get("conversation_id"),
                persona_id,
            )
            _arm_quietly(manager, persona_id)
            return False

        persona = _lookup_persona(manager, persona_id)
        building_id = getattr(persona, "current_building_id", None)
        participants = [persona_id] + ([str(user_id)] if user_id else [])
        state = _set_open_conversation(
            manager, persona_id,
            building_id=building_id, participants=participants,
        )
        LOGGER.info(
            "[user-conv] conversation %s opened for %s (building=%s)",
            state["conversation_id"], persona_id, building_id,
        )
        # 予約はロックの中で張る。Pulse をロックの外へ出したので、ここで張らないと
        # 「開いているのにタイマーが無い会話」がロックの外に一瞬でも現れる。
        _arm_quietly(manager, persona_id)

    try:
        _start_main_line_pulse(manager, persona_id, pulse_options)
    finally:
        # 応答の成否に関わらず張り直す (会話に動きがあった瞬間が装填点)。
        # ここで張り損ねても、ロック内で張った予約が会話を閉じてくれる。
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
    except Exception as exc:
        # 握り潰さない。ここで飲むと、会話が開いていない相手への発話だけが
        # 「返事も説明も出ないまま終わる」形になっていた (2026-08-24 実害)。
        # 継続経路 (直接応答) と同じ分類に載せて上へ通す — 素の例外で上げると
        # ディスパッチャの分類漏れ経路が invoke_main_line() を呼び直して
        # 二重発話になるため、必ず UserUtteranceError で包む。
        # 会話状態は既に開いており Pulse も起動済みなので side_effects_done=True。
        raise UserUtteranceError(
            f"the conversation-start main-line pulse failed for {persona_id}: {exc}",
            stage="conversation_start",
            side_effects_done=True,
            fallback_safe=False,
        ) from exc


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
      (``invoke_main_line``)。沈黙タイマーは応答の**前と後の両方**で張り直す —
      前で張るのは応答中に前回の予約が発火して会話を閉じるのを防ぐため、後で
      張るのは沈黙の起点を「やりとりが終わった時刻」に置くため。
    - **開いていない かつ 別の活動中でない**: :func:`start_conversation` で
      会話を開始する (ユーザーの呼びかけには常に即応答 — 2026-07-07 改訂)。
    - **開いていない かつ 別の活動中**: on_event 判断点へ直結し仲裁を委ねる
      (``autonomy_wiring.handle_user_utterance_conflict``)。判断が engage_now を
      選べば :func:`start_conversation`、選ばなければ応答しない
      (track_retirement.md §7.4 の直結化)。

    ⚠ **v0.3 (束 6c) では 3 本目の経路に入らない** —
    :func:`_get_open_non_conversation_episode` が常に None を返すため、判定は
    実質「会話中か / そうでないか」の二分岐へ縮退する。活動の器を作り直すのは
    v0.4 のティック設計 (v3 §9-3)。

    旧実装は 1 段目の判定に「対ユーザー会話 Track が running か」を使っていた。
    案 Y (life.md §7) 以降その running は会話が終わっても残るため、仲裁は事実上
    「初回の発話」と「ゲーム参加で押し出された後」でしか発火しなかった。

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
            open_conversation.get("conversation_id"), persona_id,
        )
        # 応答を起こす**前**に張り直す。ここを応答の後だけにすると、沈黙の終わり際に
        # 来た発話への応答中に前回の予約が発火し、ユーザーがまさに話している会話が
        # 閉じられる (2026-08-22 ローカルレビュー指摘 1)。会話 ID が変わらないため、
        # 後片付けの cancel_conversation_timeout は「同じ会話の生きた予約」と
        # 「消費済みの古い世代」を見分けられず、応答後に張り直した予約まで巻き添えで
        # 取り消される。先に張れば古い callback は token 不一致で自ら降りる。
        #
        # 開始経路 (start_conversation) は Pulse の前に張っており (731-733 行)、
        # この一行はその規律を継続経路へ揃えるもの。
        _arm_quietly(manager, persona_id)
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
            # 後に張り直さないと、二度目以降の会話が永久に閉じなくなる。
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
