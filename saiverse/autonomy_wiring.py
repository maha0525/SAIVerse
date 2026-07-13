"""自律行動 v2 の本番配線 (活性化) — 判断点の恒久起動と watchdog。

``saiverse.judgment_points`` は「判断点そのもの」(状況テキスト・動的スキーマ・
Playbook 起動) を持つが、**自動起動の配線は持たない** (中間起動の空打ち防止)。
本モジュールがその配線を担う:

- :func:`fire_judgment_point` — 本番共通の起動ゲート。ACTIVITY_STATE=Active の
  ペルソナのみ発火し (既存の自律ゲートの流儀)、判断点 Playbook が DB に無ければ
  エラーでなく WARNING + スキップ。MetaLayer の per-persona Lock で他のメタ判断
  (alert 即応等) と直列化する。day_open/day_close ではここでライフ (活動区間)
  の確定・終了処理も行う (life.md v0.5 §4/§6.2 — ライフはユーザー設定から
  システムが確定し、ペルソナは宣言しない)
- :func:`handle_scheduled_judgment` — PersonaSchedule (META_PLAYBOOK=
  ``judgment_day_open`` / ``judgment_day_close``) の発火を判断点起動へ変換する
  (ScheduleManager._execute_schedule から呼ばれる)。起床・就寝時刻の出所は
  PersonaSchedule 行そのもの — **スケジュール未設定のペルソナは day_open を
  発火しない** (AI テーブルに起床・就寝カラムは存在しないため)
- :func:`handle_wait_response_timeout` — 会話終了 (wait_response タイムアウト)。
  対ユーザー会話 Track なら会話の出来事を閉じて **post_conversation** 判断を撃つ。
  1 往復も成立しなかった会話 (応答生成失敗等) では撃たない (偽前提の状況
  テキストは作話を誘発する — シムで実証済みの抑止を本番にも適用)。
  それ以外の wait_response Track (social 等) は従来どおり MetaLayer の
  イベント駆動メタ判断に委ねる (v2 判断点の対ペルソナ社交は未設計)
- :func:`handle_external_event` — 実イベント (inject_persona_event) の入口。
  Active かつユーザー会話中でなければ **on_event** 判断を撃ち、判断が
  engage_now を選んだときだけ従来の応対 Pulse を起動する。非 Active ペルソナは
  従来どおり直接応対 (非自律ペルソナのイベント応答を壊さない)
- :func:`watchdog_tick` — AutonomyManager の定期 tick の縮退先 (v2 §4.2)。
  正常時は何もしない。「Active・起床時間帯・今日の day_plan が無い or コマ予約が
  途絶」のときだけ day_open の火入れ直し / コマ予約の再 push を行う保守的な見張り

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import date, timedelta
from typing import Any, Callable, Dict, Optional

from saiverse import clock
from saiverse.judgment_points import (
    JUDGMENT_PLAYBOOK_MAP,
    KIND_DAY_CLOSE,
    KIND_DAY_OPEN,
    KIND_ON_EVENT,
    KIND_POST_CONVERSATION,
    run_judgment_point,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 一日リズムの深夜跨ぎ (overnight) ヘルパ
# ---------------------------------------------------------------------------


def is_overnight(wake: str, close: Optional[str]) -> bool:
    """就寝時刻が日付を跨ぐリズムかどうかを返す。

    ``close`` が存在し、かつ ``close < wake`` (就寝が起床より前の HH:MM) なら
    跨ぎリズム。close が None (就寝スケジュール未設定) は跨ぎなし。

    Args:
        wake:  起床時刻 "HH:MM"
        close: 就寝時刻 "HH:MM" または None
    """
    if not close:
        return False
    return close < wake


def effective_plan_date(
    now_dt: Any, wake: Optional[str], close: Optional[str]
) -> date:
    """現在時刻が属する「営業日」の date を返す。

    「営業日」= 覚醒日。深夜跨ぎリズム (close < wake) で、かつ現在時刻が
    00:00〜wake の深夜帯 (前日リズムの尻尾) に在るときは **前日** を返す。
    それ以外 (通常帯 / 非跨ぎ / wake が None) は ``now_dt.date()`` を返す。

    Args:
        now_dt: saiverse.clock.now() の戻り値 (naive datetime)
        wake:   起床時刻 "HH:MM"。None なら暦日をそのまま返す
        close:  就寝時刻 "HH:MM"。None は跨ぎなし扱い
    """
    if not wake:
        return now_dt.date()
    hhmm = now_dt.strftime("%H:%M")
    if is_overnight(wake, close) and hhmm < wake:
        # 深夜帯 (前日リズムの尻尾) — 覚醒日は前日
        return now_dt.date() - timedelta(days=1)
    return now_dt.date()


def in_waking_window(hhmm: str, wake: str, close: Optional[str]) -> bool:
    """「起きている時間帯」かどうかを返す。

    - 跨ぎリズム (close < wake): ``hhmm >= wake`` (起床後) または
      ``hhmm < close`` (深夜帯の尻尾) が「窓の中」
    - 非跨ぎリズム (wake <= close): ``wake <= hhmm < close`` が「窓の中」
    - close が None (就寝なし): ``hhmm >= wake`` が「窓の中」

    Args:
        hhmm:  現在時刻 "HH:MM"
        wake:  起床時刻 "HH:MM"
        close: 就寝時刻 "HH:MM" または None
    """
    if not close:
        return hhmm >= wake
    if is_overnight(wake, close):
        return hhmm >= wake or hhmm < close
    return wake <= hhmm < close


#: 判断点 Playbook 名の集合 (ScheduleManager の経路分岐が使う)
JUDGMENT_PLAYBOOK_NAMES = frozenset(JUDGMENT_PLAYBOOK_MAP.values())

#: Playbook 名 → 判断点 kind の逆引き
PLAYBOOK_TO_KIND: Dict[str, str] = {v: k for k, v in JUDGMENT_PLAYBOOK_MAP.items()}

#: PersonaSchedule 経由で起動してよい判断点 (時刻駆動なのは起床・就寝のみ。
#: post_* / on_event は文脈必須なのでスケジュールから撃つと偽前提になる)
_SCHEDULABLE_KINDS = (KIND_DAY_OPEN, KIND_DAY_CLOSE)

#: handle_external_event の経路ラベル (テスト・ログの観察用)
ROUTE_DIRECT_NOT_ACTIVE = "direct:not_active"
ROUTE_DIRECT_IN_CONVERSATION = "direct:in_conversation"
ROUTE_DIRECT_JUDGMENT_UNAVAILABLE = "direct:judgment_unavailable"
ROUTE_JUDGED_ENGAGE_NOW = "judged:engage_now"
ROUTE_JUDGED_UNKNOWN = "judged:unknown_reaction"


# ---------------------------------------------------------------------------
# 共通ゲート
# ---------------------------------------------------------------------------


def _get_persona(manager: Any, persona_id: str) -> Optional[Any]:
    return (getattr(manager, "personas", None) or {}).get(persona_id)


def _is_active(manager: Any, persona_id: str) -> bool:
    """ACTIVITY_STATE=Active か (既存ゲートの流儀: 属性欠落は Idle 扱い)。"""
    persona = _get_persona(manager, persona_id)
    if persona is None:
        return False
    return getattr(persona, "activity_state", "Idle") == "Active"


def playbook_available(manager: Any, playbook_name: str) -> bool:
    """判断点 Playbook が DB に import 済みか (playbooks テーブルの存在確認)。

    判定不能 (SessionLocal 無し・クエリ失敗) は True に倒す — 実行側
    (run_meta_user) の Playbook not found エラーハンドリングに委ね、
    ここで黙って落とさない。
    """
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        return True
    try:
        from database.models import Playbook

        db = session_factory()
        try:
            row = (
                db.query(Playbook.name)
                .filter(Playbook.name == playbook_name)
                .first()
            )
        finally:
            db.close()
        return row is not None
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] playbook availability check failed for %r; "
            "assuming available",
            playbook_name, exc_info=True,
        )
        return True


def _judgment_lock(manager: Any, persona_id: str):
    """MetaLayer の per-persona Lock (あれば)。判断 Pulse の直列化を共有する。"""
    meta_layer = getattr(manager, "meta_layer", None)
    get_lock = getattr(meta_layer, "_get_lock", None)
    if callable(get_lock):
        try:
            lock = get_lock(persona_id)
            if hasattr(lock, "__enter__"):
                return lock
        except Exception:
            LOGGER.warning(
                "[autonomy-wiring] failed to acquire meta-layer lock for %s",
                persona_id, exc_info=True,
            )
    return nullcontext()


def fire_judgment_point(
    manager: Any,
    persona_id: str,
    kind: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    precondition: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """判断点を本番ゲート付きで 1 回起動する。

    ゲート (順に):
    1. ACTIVITY_STATE=Active のペルソナのみ (既存の自律ゲートの流儀)
    2. 判断点 Playbook が DB に無ければ WARNING + スキップ (エラーにしない。
       import は運用手順: ``python scripts/import_playbook.py --file
       builtin_data/playbooks/public/judgment_*.json``)
    3. MetaLayer の per-persona Lock で直列化 (alert 即応メタ判断と同じ列)
    4. ``precondition`` (あれば) を **Lock 取得後に** 再評価 — watchdog の
       day_open 再発火が、待っている間に済んだ本物の day_open と二重にならない

    v0.5 (life.md §3/§4/§6.2): ``kind`` が day_open / day_close のときは、
    ``run_judgment_point`` の前にライフ (活動区間) まわりのシステム処理を行う
    (両方とも本番の day_open / day_close 発火経路はここ 1 箇所に集約される —
    ``handle_scheduled_judgment`` と watchdog の day_open 再発火の両方が
    この関数を通る):

    - **day_open**: :func:`saiverse.day_plan.confirm_life_for_today` で
      ユーザー設定 (PersonaSchedule の起床・就寝 + ``context`` 経由の
      ``daily_budget_pulses``) から今日のライフを確定する (冪等 — 既に
      確定済みなら何もしない)。**当日はじめての確定のときだけ**
      :func:`saiverse.day_plan._handle_life_start` を呼ぶ (TTL override・
      tail 通知。再確定では二重通知しない)
    - **day_close**: その日の確定済みライフがあれば
      :func:`saiverse.day_plan._handle_life_end` を呼ぶ (keep-alive 予約
      cancel・TTL 遅延解除予約・tail 通知)

    Returns:
        ``run_judgment_point`` の結果 dict (``submitted`` / ``reason`` /
        ``applied_events`` 等)。ゲートで止まった場合は ``submitted=False`` +
        ``reason``。
    """
    playbook_name = JUDGMENT_PLAYBOOK_MAP.get(kind)
    if playbook_name is None:
        raise ValueError(f"unknown judgment kind: {kind!r}")

    if not _is_active(manager, persona_id):
        LOGGER.debug(
            "[autonomy-wiring] %s skipped (persona=%s not Active)", kind, persona_id,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "persona not Active"}

    if not playbook_available(manager, playbook_name):
        LOGGER.warning(
            "[autonomy-wiring] judgment playbook %r is not in DB; skipping %s "
            "(persona=%s). Import it with: python scripts/import_playbook.py "
            "--file builtin_data/playbooks/public/%s.json",
            playbook_name, kind, persona_id, playbook_name,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "playbook not imported"}

    with _judgment_lock(manager, persona_id):
        if precondition is not None:
            try:
                still_needed = bool(precondition())
            except Exception:
                LOGGER.warning(
                    "[autonomy-wiring] precondition for %s raised; skipping "
                    "(persona=%s)", kind, persona_id, exc_info=True,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False, "reason": "precondition raised"}
            if not still_needed:
                LOGGER.info(
                    "[autonomy-wiring] %s no longer needed at dispatch; skipping "
                    "(persona=%s)", kind, persona_id,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False, "reason": "precondition not met"}

        if kind == KIND_DAY_OPEN:
            _confirm_life_at_day_open(manager, persona_id, context or {})
        elif kind == KIND_DAY_CLOSE:
            _apply_life_end_at_day_close(manager, persona_id)

        result = run_judgment_point(manager, persona_id, kind, context)

    # 判断点の発火回数を「別枠」で記帳する (life.md v0.5 §5.3/§8.2)。予算
    # (used_pulses) には触れない — 判断点はペルソナが編成でコントロールできない
    # 発火 (会話がいつ終わるかはペルソナ次第ではない) であり、同じ財布に
    # 入れると構造矛盾が生じる (実機初日の教訓)。lives の無い日は no-op。
    # ロックの外で行ってよい (メタ判断の直列化とは無関係な帳簿処理)。
    if result.get("submitted"):
        try:
            from saiverse import day_plan
            day_plan.record_judgment_pulse(manager, persona_id)
        except Exception:
            LOGGER.warning(
                "[autonomy-wiring] record_judgment_pulse failed (persona=%s kind=%s)",
                persona_id, kind, exc_info=True,
            )
    return result


def _confirm_life_at_day_open(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> None:
    """day_open 発火経路でのライフ確定 (life.md v0.5 §4/§11.2)。

    区間はユーザーが設定した起床・就寝 (PersonaSchedule、
    :func:`_find_day_schedules` が解決)、予算は ``context`` 経由のユーザー
    設定値 (``daily_budget_pulses``、無ければ最低値)。冪等
    (:func:`~saiverse.day_plan.confirm_life_for_today` が既存確定を保持する)
    — 当日はじめての確定のときだけライフ開始の節目処理
    (:func:`~saiverse.day_plan._handle_life_start`) を呼ぶ (再確定での
    二重 TTL 設定・二重 tail 通知を避ける)。
    """
    from saiverse import day_plan

    plan_date = clock.now().date().isoformat()
    already_confirmed = bool(day_plan.get_lives(manager, persona_id, plan_date))
    sched = _find_day_schedules(manager, persona_id)
    budget = context.get("daily_budget_pulses") if isinstance(context, dict) else None
    try:
        life = day_plan.confirm_life_for_today(
            manager, persona_id, plan_date,
            sched.get("wake"), sched.get("close"),
            requested_budget_pulses=budget,
        )
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to confirm today's life at day_open "
            "(persona=%s date=%s)", persona_id, plan_date, exc_info=True,
        )
        return
    if life is not None and not already_confirmed:
        try:
            day_plan._handle_life_start(manager, persona_id, plan_date, 0, life)
        except Exception:
            LOGGER.warning(
                "[autonomy-wiring] life-start processing failed "
                "(persona=%s date=%s)", persona_id, plan_date, exc_info=True,
            )


def _apply_life_end_at_day_close(manager: Any, persona_id: str) -> None:
    """day_close 発火経路でのライフ終了処理 (life.md v0.5 §4.1/§11.2)。

    「ライフ終了 = 就寝判断 (day_close) の発火」そのもの — 専用のライフ境界
    イベント予約は v0.5 で廃止した。営業日 (覚醒日) の算出は
    ``judgment_points.build_judgment_args`` の KIND_DAY_CLOSE 分岐と同じ規則
    (深夜跨ぎリズムでは 01:00 発火の day_close は前日が営業日)。
    """
    from saiverse import day_plan

    sched = _find_day_schedules(manager, persona_id)
    plan_date = effective_plan_date(
        clock.now(), sched.get("wake"), sched.get("close"),
    ).isoformat()
    lives = day_plan.get_lives(manager, persona_id, plan_date)
    if not lives:
        return
    try:
        day_plan._handle_life_end(manager, persona_id, plan_date, 0, lives[0])
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] life-end processing failed (persona=%s date=%s)",
            persona_id, plan_date, exc_info=True,
        )


# ---------------------------------------------------------------------------
# day_open / day_close: PersonaSchedule の発火から (ScheduleManager が呼ぶ)
# ---------------------------------------------------------------------------


def handle_scheduled_judgment(
    manager: Any,
    persona_id: str,
    playbook_name: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """PersonaSchedule (META_PLAYBOOK=judgment_*) の発火を判断点起動へ変換する。

    通常のスケジュールは「<system> プロンプト + Playbook」の submit_schedule で
    走るが、判断点 Playbook は発火時に judgment_points が組む動的 args
    (situation_text / response_schema) が必須のため、この専用経路を通す。

    時刻駆動で意味を持つのは day_open / day_close のみ。他の判断点名が
    スケジュールに書かれていた場合は WARNING + スキップ (偽前提の防止)。

    Args:
        params: PersonaSchedule.PLAYBOOK_PARAMS (パース済み dict)。day_open は
            ``daily_budget_rounds`` (正整数、作業ラウンドの日次予算) と
            ``daily_budget_pulses`` (正整数、ライフの標準パルス予算 —
            ユーザー設定。未設定/最低値未満は最低値へ切り上げ。
            life.md v0.5 §4.2) を context に透過する。
    """
    kind = PLAYBOOK_TO_KIND.get(playbook_name)
    if kind is None:
        LOGGER.warning(
            "[autonomy-wiring] scheduled playbook %r is not a judgment playbook; "
            "skipping (persona=%s)", playbook_name, persona_id,
        )
        return {"kind": None, "submitted": False, "reason": "not a judgment playbook"}
    if kind not in _SCHEDULABLE_KINDS:
        LOGGER.warning(
            "[autonomy-wiring] judgment kind %r cannot be fired from a schedule "
            "(only %s); skipping (persona=%s)",
            kind, "/".join(_SCHEDULABLE_KINDS), persona_id,
        )
        return {"kind": kind, "submitted": False, "reason": "kind not schedulable"}

    context: Dict[str, Any] = {}
    if kind == KIND_DAY_OPEN and isinstance(params, dict):
        budget = params.get("daily_budget_rounds")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1:
            context["daily_budget_rounds"] = budget
        budget_pulses = params.get("daily_budget_pulses")
        if isinstance(budget_pulses, int) and not isinstance(budget_pulses, bool) \
                and budget_pulses >= 1:
            context["daily_budget_pulses"] = budget_pulses

    LOGGER.info(
        "[autonomy-wiring] scheduled judgment firing: kind=%s persona=%s",
        kind, persona_id,
    )
    return fire_judgment_point(manager, persona_id, kind, context)


# ---------------------------------------------------------------------------
# post_conversation: 会話終了 (wait_response タイムアウト) から
# ---------------------------------------------------------------------------


def _conversation_had_exchange(manager: Any, persona_id: str, track_id: str) -> bool:
    """今の会話区間 (開いている会話の出来事) にペルソナ応答が実在するか。

    判定材料: 会話の出来事 (kind='conversation', A1 で track activate 時に開く)
    の started_at 以降に、当該 Track 紐付き (origin_track_id) の assistant
    メッセージが SAIMemory に 1 件でもあるか。対ユーザー会話 Track は永続なので
    「Track に assistant メッセージがあるか」だけでは過去の会話に反応してしまう
    — 区間は出来事で切る。

    判定不能 (出来事が無い / adapter 未対応 / クエリ失敗) は **True に倒す**
    (実際にあった会話の収穫を取りこぼすより、稀な空判断の方が害が小さい。
    シムの mock ドライバ既定と同じ向き)。
    """
    persona = _get_persona(manager, persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    checker = getattr(adapter, "has_track_assistant_message_since", None)
    if not callable(checker):
        return True
    try:
        from saiverse import episodes

        ep = episodes.get_open_episode(
            manager, persona_id, kind=episodes.KIND_CONVERSATION,
        )
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to read open conversation episode "
            "(persona=%s); assuming exchange happened", persona_id, exc_info=True,
        )
        return True
    if ep is None:
        return True
    since = ep.get("started_at")
    if not isinstance(since, int):
        return True
    result = checker(track_id, since)
    if result is None:
        return True
    return bool(result)


def handle_conversation_end(
    manager: Any, persona_id: str, track_id: str
) -> Dict[str, Any]:
    """対ユーザー会話の終了処理: 出来事を閉じ、post_conversation 判断を撃つ。

    1 往復も成立しなかった会話 (応答生成の失敗等) では判断を撃たない —
    「会話がひと区切りつきました」という偽前提の状況テキストは、存在しない
    会話の振り返り (作話) を誘発する (接地原則 v2 §3-1。2026-07-05 実 LLM シムで
    実証済みの抑止を本番へ適用)。往復判定は出来事を閉じる **前** に行う
    (会話区間の started_at が要るため)。
    """
    had_exchange = _conversation_had_exchange(manager, persona_id, track_id)

    # 会話の出来事を閉じる (A1 の運用の線)。記録専用 — 失敗しても判断は止めない。
    # 閉じた出来事の参照は post_conversation 判断へ渡す (層2 棚入れの対象 §9.1)。
    episode_ref: Optional[str] = None
    try:
        from saiverse.episodes import close_conversation_episode

        closed = close_conversation_episode(manager, persona_id)
        episode_ref = (closed or {}).get("episode_ref")
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to close conversation episode: "
            "persona=%s track=%s", persona_id, track_id, exc_info=True,
        )

    if not had_exchange:
        LOGGER.warning(
            "[autonomy-wiring] conversation ended with zero exchange "
            "(persona=%s track=%s); skipping post_conversation judgment",
            persona_id, track_id,
        )
        return {"kind": KIND_POST_CONVERSATION, "submitted": False,
                "reason": "conversation had no exchange; judgment skipped"}

    context: Dict[str, Any] = {}
    if episode_ref:
        context["episode_ref"] = episode_ref
    return fire_judgment_point(manager, persona_id, KIND_POST_CONVERSATION, context)


def handle_wait_response_timeout(manager: Any, persona_id: str, track_id: str) -> None:
    """wait_response タイムアウト発火後の本番処理 (SAIVerseManager callback の実体)。

    - 対ユーザー会話 Track → :func:`handle_conversation_end`
      (= v2 の「会話終了」判断点。intent §10-5)
    - それ以外の wait_response Track (social 等) → 従来どおり MetaLayer の
      イベント駆動メタ判断 (対ペルソナ社交の判断点は v2 未設計 — Phase 5 系統 ii)
    - 最後に AutonomyManager (watchdog) の次回 tick を押し戻す (直後の
      watchdog と重ならないように)
    """
    track_type: Optional[str] = None
    try:
        track = manager.track_manager.get(track_id)
        track_type = getattr(track, "track_type", None)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to read track %s for timeout handling "
            "(persona=%s)", track_id, persona_id, exc_info=True,
        )

    if track_type == "user_conversation":
        try:
            handle_conversation_end(manager, persona_id, track_id)
        except Exception:
            LOGGER.exception(
                "[autonomy-wiring] post_conversation handling failed: "
                "persona=%s track=%s", persona_id, track_id,
            )
    else:
        meta_layer = getattr(manager, "meta_layer", None)
        if meta_layer is None:
            LOGGER.warning(
                "[autonomy-wiring] meta_layer not initialized; cannot fire "
                "judgment for persona=%s track=%s", persona_id, track_id,
            )
        else:
            try:
                meta_layer.on_periodic_tick(
                    persona_id,
                    context={
                        "trigger": "wait_response_timeout",
                        "track_id": track_id,
                    },
                )
            except Exception:
                LOGGER.exception(
                    "[autonomy-wiring] meta-judgment fire failed: persona=%s track=%s",
                    persona_id, track_id,
                )

    # watchdog (AutonomyManager) の次回 tick を押し戻す (存在すれば)
    try:
        autonomy_managers = getattr(manager, "_autonomy_managers", None) or {}
        am = autonomy_managers.get(persona_id)
        if am is not None:
            am.defer_next_tick()
    except Exception:
        LOGGER.exception(
            "[autonomy-wiring] defer_next_tick failed: persona=%s", persona_id,
        )


# ---------------------------------------------------------------------------
# on_event: 実イベント (inject_persona_event) から
# ---------------------------------------------------------------------------


def _extract_reaction(result: Dict[str, Any]) -> Optional[str]:
    """judgment_finalize が emit した judgment_applied イベントから reaction を読む。"""
    for ev in result.get("applied_events") or []:
        if not isinstance(ev, dict):
            continue
        for extra in ev.get("extras") or []:
            if isinstance(extra, str) and extra.startswith("reaction="):
                return extra.split("=", 1)[1]
    return None


def handle_external_event(
    manager: Any,
    persona_id: str,
    event_text: str,
    *,
    dispatch_direct: Callable[[], None],
    is_alert: bool = False,
) -> str:
    """実イベントの本番入口 (inject_persona_event の既定経路)。

    経路の判断基準:

    - **Active でないペルソナ**: 従来どおり即応対 (``dispatch_direct``)。
      非自律ペルソナのイベント応答 (X メンション等) は v2 の管轄外で、
      従来挙動を壊さない
    - **ユーザー会話中**: on_event は撃たない (会話の至上性、judgment_points.md
      §7)。イベントは従来経路で応対 Pulse として submit され、PulseController の
      priority 制御 (user 優先) に従う
    - **Active かつ手すき**: on_event 判断を撃つ。判断が ``engage_now`` を
      選んだときだけ従来の応対 Pulse を起動する。insert_slot / note_only /
      ignore は finalize が適用済みなので応対は起動しない
    - 判断が起動できなかった (Playbook 未 import 等) 場合はイベントを落とさない
      よう従来経路へフォールバックする
    - 判断は走ったが reaction が読めなかった場合は応対を起動しない
      (二重応対の方が害が大きい。WARNING で観察可能にする)

    Returns:
        経路ラベル (``direct:*`` / ``judged:*``)。ログ・テストの観察用。
    """
    if not _is_active(manager, persona_id):
        dispatch_direct()
        return ROUTE_DIRECT_NOT_ACTIVE

    try:
        from saiverse.day_plan import _is_in_user_conversation

        in_conversation = _is_in_user_conversation(manager, persona_id)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] conversation check failed (persona=%s); "
            "treating as not in conversation", persona_id, exc_info=True,
        )
        in_conversation = False
    if in_conversation:
        dispatch_direct()
        return ROUTE_DIRECT_IN_CONVERSATION

    result = fire_judgment_point(
        manager, persona_id, KIND_ON_EVENT,
        {"event_text": event_text, "is_alert": is_alert},
    )
    if not result.get("submitted"):
        LOGGER.info(
            "[autonomy-wiring] on_event judgment unavailable (%s); "
            "falling back to direct dispatch (persona=%s)",
            result.get("reason"), persona_id,
        )
        dispatch_direct()
        return ROUTE_DIRECT_JUDGMENT_UNAVAILABLE

    reaction = _extract_reaction(result)
    if reaction == "engage_now":
        LOGGER.info(
            "[autonomy-wiring] on_event judged engage_now; dispatching response "
            "(persona=%s)", persona_id,
        )
        dispatch_direct()
        return ROUTE_JUDGED_ENGAGE_NOW
    if reaction is None:
        LOGGER.warning(
            "[autonomy-wiring] on_event judgment ran but reaction could not be "
            "read; NOT dispatching a response to avoid double handling "
            "(persona=%s)", persona_id,
        )
        return ROUTE_JUDGED_UNKNOWN
    LOGGER.info(
        "[autonomy-wiring] on_event judged %s (persona=%s); no immediate response",
        reaction, persona_id,
    )
    return f"judged:{reaction}"


# ---------------------------------------------------------------------------
# watchdog: AutonomyManager 定期 tick の縮退先 (v2 §4.2)
# ---------------------------------------------------------------------------


def _find_day_schedules(manager: Any, persona_id: str) -> Dict[str, Any]:
    """ペルソナの起床・就寝スケジュール (PersonaSchedule) を読む。

    Returns:
        ``{"wake": "HH:MM"|None, "close": "HH:MM"|None,
        "wake_days": set[int]|None, "day_open_params": dict|None}``。
        wake が None = v2 の一日リズム未設定 (watchdog は何もしない)。
    """
    out: Dict[str, Any] = {
        "wake": None, "close": None, "wake_days": None, "day_open_params": None,
    }
    session_factory = getattr(manager, "SessionLocal", None)
    if session_factory is None:
        return out
    try:
        from database.models import PersonaSchedule

        db = session_factory()
        try:
            rows = (
                db.query(PersonaSchedule)
                .filter(
                    PersonaSchedule.PERSONA_ID == persona_id,
                    PersonaSchedule.ENABLED == True,  # noqa: E712
                    PersonaSchedule.SCHEDULE_TYPE == "periodic",
                    PersonaSchedule.META_PLAYBOOK.in_([
                        JUDGMENT_PLAYBOOK_MAP[KIND_DAY_OPEN],
                        JUDGMENT_PLAYBOOK_MAP[KIND_DAY_CLOSE],
                    ]),
                )
                .all()
            )
            for row in rows:
                tod = (row.TIME_OF_DAY or "").strip()
                if not tod:
                    continue
                if row.META_PLAYBOOK == JUDGMENT_PLAYBOOK_MAP[KIND_DAY_OPEN]:
                    if out["wake"] is None or tod < out["wake"]:
                        out["wake"] = tod
                        if row.DAYS_OF_WEEK:
                            try:
                                days = json.loads(row.DAYS_OF_WEEK)
                                out["wake_days"] = {int(d) for d in days}
                            except (TypeError, ValueError):
                                out["wake_days"] = None
                        else:
                            out["wake_days"] = None
                        if row.PLAYBOOK_PARAMS:
                            try:
                                parsed = json.loads(row.PLAYBOOK_PARAMS)
                                if isinstance(parsed, dict):
                                    out["day_open_params"] = parsed
                            except (TypeError, ValueError):
                                pass
                else:
                    if out["close"] is None or tod > out["close"]:
                        out["close"] = tod
        finally:
            db.close()
    except Exception:
        LOGGER.warning(
            "[watchdog] failed to read day schedules for %s", persona_id,
            exc_info=True,
        )
    return out


def watchdog_tick(manager: Any, persona_id: str) -> Dict[str, Any]:
    """自律稼働の watchdog (旧 50 分メタ判断 tick の縮退形、v2 §4.2)。

    正常時は何もしない。以下のときだけ火を入れ直す (判定は保守側):

    - Active・起床時間帯 (day_open スケジュールの時刻〜day_close の時刻)・
      **今日の day_plan 行が無い** → day_open を発火し直す
      (起床時刻にサーバーが落ちていた / 途中で Active 化された等)
    - plan はあるが pending / deferred コマの EventScheduler 予約が消えている
      (再起動等でインメモリ予約が失われた) → コマ予約を再 push する

    day_open / day_close の PersonaSchedule が無いペルソナ (v2 の一日リズム
    未設定) では何もしない。発火時は必ず INFO ログを残す。

    Returns:
        ``{"action": "none"|"skip"|"day_open_refire"|"reschedule", ...}``
        (観察・テスト用)。
    """
    if not _is_active(manager, persona_id):
        return {"action": "skip", "reason": "not Active"}

    sched = _find_day_schedules(manager, persona_id)
    wake = sched.get("wake")
    if not wake:
        return {"action": "skip", "reason": "no day_open schedule"}

    now = clock.now()
    hhmm = now.strftime("%H:%M")
    close = sched.get("close")

    if not in_waking_window(hhmm, wake, close):
        # 起きていない時間帯 — before wake か after close か
        if not is_overnight(wake, close):
            reason = "before wake" if hhmm < wake else "after close"
        else:
            # 跨ぎリズムで窓外 = close <= hhmm < wake の帯 (前日就寝後・未起床)
            reason = "before wake" if hhmm < wake else "after close"
        return {"action": "none", "reason": reason}

    wake_days = sched.get("wake_days")
    if wake_days is not None:
        # 跨ぎリズムの深夜帯 (hhmm < wake) は「前日の weekday」が正しい対照日
        check_date = effective_plan_date(now, wake, close)
        if check_date.weekday() not in wake_days:
            return {"action": "none", "reason": "not a scheduled day"}

    from saiverse import day_plan

    # 営業日 (覚醒日) の plan を引く
    plan_date = effective_plan_date(now, wake, close)
    today = plan_date.isoformat()
    plan = day_plan.load_day_plan(manager, persona_id, today)
    if plan is None:
        # day_open 再発火の制約: hhmm >= wake の帯 (起床後の通常帯) でのみ撃つ。
        # 深夜帯 (跨ぎの尻尾、hhmm < wake) は「前日の覚醒日に plan が無い」状態
        # だが、ここで新しい day_open を 00:30 に撃つのは誤り — 起きなかった日に
        # 深夜に plan を作っても時間割が即発火してしまう。
        if is_overnight(wake, close) and hhmm < wake:
            LOGGER.debug(
                "[watchdog] overnight tail: no plan for %s but in midnight zone; "
                "skipping day_open refire (persona=%s)", today, persona_id,
            )
            return {"action": "none", "reason": "overnight tail: no refire in midnight zone"}

        LOGGER.info(
            "[watchdog] no day plan for today; re-firing day_open "
            "(persona=%s date=%s wake=%s)", persona_id, today, wake,
        )
        context: Dict[str, Any] = {}
        params = sched.get("day_open_params")
        if isinstance(params, dict):
            budget = params.get("daily_budget_rounds")
            if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1:
                context["daily_budget_rounds"] = budget
            budget_pulses = params.get("daily_budget_pulses")
            if isinstance(budget_pulses, int) and not isinstance(budget_pulses, bool) \
                    and budget_pulses >= 1:
                context["daily_budget_pulses"] = budget_pulses
        result = fire_judgment_point(
            manager, persona_id, KIND_DAY_OPEN, context,
            # Lock 待ちの間に本物の day_open が済んでいたら撃たない (二重編成防止)
            precondition=lambda: day_plan.load_day_plan(
                manager, persona_id, today,
            ) is None,
        )
        return {"action": "day_open_refire", "result": result}

    # v0.5 (life.md §11.2): 専用のライフ境界イベント予約は廃止した — ライフの
    # 開始/終了処理は day_open/day_close の発火経路 (fire_judgment_point) に
    # 統合済みのため、ここで見張るのはコマ予約の途絶だけでよい。
    lost = day_plan.find_lost_slot_reservations(manager, persona_id, today)
    if lost:
        LOGGER.info(
            "[watchdog] %d slot reservation(s) lost; re-scheduling pending slots "
            "(persona=%s date=%s indices=%s)", len(lost), persona_id, today, lost,
        )
        pushed = day_plan.reschedule_pending_slots(manager, persona_id, plan_date)
        return {"action": "reschedule", "pushed": pushed, "lost": lost}

    return {"action": "none"}
