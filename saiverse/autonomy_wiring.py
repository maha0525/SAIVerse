"""自律行動 v2 の本番配線 (活性化) — 判断点の恒久起動と watchdog。

``saiverse.judgment_points`` は「判断点そのもの」(状況テキスト・動的スキーマ・
Playbook 起動) を持つが、**自動起動の配線は持たない** (中間起動の空打ち防止)。
本モジュールがその配線を担う:

- :func:`fire_judgment_point` — 本番共通の起動ゲート。AUTONOMY_ENABLED の
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
  自律 ON かつユーザー会話中でなければ **on_event** 判断を撃ち、判断が
  engage_now を選んだときだけ従来の応対 Pulse を起動する。自律 OFF のペルソナは
  従来どおり直接応対 (非自律ペルソナのイベント応答を壊さない)
- :func:`watchdog_tick` — AutonomyManager の定期 tick の縮退先 (v2 §4.2)。
  正常時は何もしない。「自律 ON・起床時間帯・今日の day_plan が無い (行が無い、
  または行はあってもコマが 0 件) or コマ予約が途絶」のときだけ day_open の
  火入れ直し / コマ予約の再 push を行う保守的な見張り

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import dataclasses
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
    KIND_POST_SESSION,
    OUTCOME_ABORTED,
    OUTCOME_INDETERMINATE,
    _abandon_seat,
    run_judgment_point,
    validate_judgment_context,
)

LOGGER = logging.getLogger(__name__)

#: manager に execution_ledger が無い環境 (旧テストスタブ等) への WARN を
#: persona ごとに一度だけ出すための既知セット (台帳なし degrade は許すが黙らせない)。
_LEDGER_MISSING_WARNED: set = set()

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
ROUTE_DIRECT_AUTONOMY_DISABLED = "direct:autonomy_disabled"
ROUTE_DIRECT_IN_CONVERSATION = "direct:in_conversation"
ROUTE_DIRECT_JUDGMENT_UNAVAILABLE = "direct:judgment_unavailable"
ROUTE_JUDGED_ENGAGE_NOW = "judged:engage_now"
ROUTE_JUDGED_UNKNOWN = "judged:unknown_reaction"
#: 判断は起動できなかったが、実行台帳の席を放棄できなかった (= 回復 tick に
#: 再発火されうる / 別の claimant が走らせている)。**代替経路を走らせない** —
#: 二重応対の方が害が大きい (下の unknown_reaction と同じ判断)。
ROUTE_NONE_INDETERMINATE = "none:judgment_indeterminate"


# ---------------------------------------------------------------------------
# 共通ゲート
# ---------------------------------------------------------------------------


def _get_persona(manager: Any, persona_id: str) -> Optional[Any]:
    return (getattr(manager, "personas", None) or {}).get(persona_id)


def _is_active(manager: Any, persona_id: str) -> bool:
    """AUTONOMY_ENABLED か (属性欠落は False 扱い)。"""
    persona = _get_persona(manager, persona_id)
    if persona is None:
        return False
    return bool(getattr(persona, "autonomy_enabled", False))


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


# ---------------------------------------------------------------------------
# 実行台帳との結線 (W1 Chunk A: A2 の重複抑止 + A7 の durable queue)
# ---------------------------------------------------------------------------


def _day_open_plan_date() -> str:
    """day_open の plan_date (暦日)。ライフ確定 (:func:`_confirm_life_at_day_open`)
    と冪等キー (:func:`_judgment_idempotency_key`) で必ず同源を使う (D1)。"""
    return clock.now().date().isoformat()


def _day_close_plan_date(manager: Any, persona_id: str) -> str:
    """day_close の営業日 (覚醒日)。ライフ終了 (:func:`_apply_life_end_at_day_close`)
    と冪等キーで同源。judgment_points.build_judgment_args の KIND_DAY_CLOSE 分岐と
    同じ規則 (深夜跨ぎリズムでは 01:00 発火の day_close は前日が営業日)。"""
    sched = _find_day_schedules(manager, persona_id)
    return effective_plan_date(
        clock.now(), sched.get("wake"), sched.get("close"),
    ).isoformat()


def _judgment_idempotency_key(
    manager: Any, persona_id: str, kind: str, context: Optional[Dict[str, Any]]
) -> Optional[str]:
    """判断点 kind ごとの冪等キー (D1 の表)。None = 一意性なし (毎回新規行)。

    - day_open:  ``{persona}:{plan_date}`` (暦日)
    - day_close: ``{persona}:{effective_plan_date}`` (営業日)
    - post_session / post_conversation: ``{persona}:{episode_ref}``。
      episode_ref が無ければ None (一意性なし)
    - on_event: None — 毎イベント新規行。**prepared 行が durable queue** (A7/D5)
    """
    if kind == KIND_DAY_OPEN:
        return f"{persona_id}:{_day_open_plan_date()}"
    if kind == KIND_DAY_CLOSE:
        return f"{persona_id}:{_day_close_plan_date(manager, persona_id)}"
    if kind in (KIND_POST_SESSION, KIND_POST_CONVERSATION):
        episode_ref = None
        if isinstance(context, dict):
            episode_ref = context.get("episode_ref")
            if not episode_ref:
                sr = context.get("session_result")
                if isinstance(sr, dict):
                    episode_ref = sr.get("episode_ref")
                elif sr is not None:
                    episode_ref = getattr(sr, "episode_ref", None)
        if episode_ref:
            return f"{persona_id}:{episode_ref}"
        return None
    return None


def _serialize_judgment_context(
    context: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """context を JSON 化可能な形に正規化して台帳 payload に凍結する (D3)。

    dataclass (WorkSessionResult 等) は asdict、シリアライズ不能値は str() に
    落とす。回復 refire はこの dict をそのまま context として復元する
    (judgment_points 側の ``_ws_get`` は dict も読める)。
    """
    if not isinstance(context, dict) or not context:
        return None

    def _norm(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _norm(dataclasses.asdict(value))
        if isinstance(value, dict):
            return {str(k): _norm(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_norm(v) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    return _norm(dict(context))


def _release_claimed_seat(
    ledger: Any, execution_id: Optional[str], reason: str
) -> str:
    """LLM 開始前の離脱で claim 済みの席を放棄し、結末 (outcome) を返す。

    放棄は prepared 限定 CAS (:func:`saiverse.judgment_points._abandon_seat`) で
    行う — 無条件 ``mark_failed`` だと、二重 claim で同じ execution_id を共有した
    別の claimant が既に running へ進めた勝者の台帳まで failed に壊す
    (``run_judgment_point`` 側の離脱と同じ規律。
    docs/issues/judgment_seat_contention_and_event_loss.md ②)。

    Returns:
        :data:`~saiverse.judgment_points.OUTCOME_ABORTED` = 席は残っていない
        (呼び出し側は代替経路・backoff 再試行へ進んでよい)。
        :data:`~saiverse.judgment_points.OUTCOME_INDETERMINATE` = 放棄できな
        かった (勝者の所有 / 台帳が応答しない) — 代替経路を走らせてはいけない。
    """
    released = _abandon_seat(ledger, execution_id, reason)
    return OUTCOME_ABORTED if released else OUTCOME_INDETERMINATE


def fire_judgment_point(
    manager: Any,
    persona_id: str,
    kind: str,
    context: Optional[Dict[str, Any]] = None,
    *,
    precondition: Optional[Callable[[], bool]] = None,
    force: bool = False,
    resume_execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """判断点を本番ゲート付きで 1 回起動する。

    ゲート (順に):
    1. AUTONOMY_ENABLED のペルソナのみ (既存の自律ゲートの流儀)
    2. 判断点 Playbook が DB に無ければ WARNING + スキップ (エラーにしない。
       import は運用手順: ``python scripts/import_playbook.py --file
       builtin_data/playbooks/public/judgment_*.json``)
    3. MetaLayer の per-persona Lock で直列化 (alert 即応メタ判断と同じ列)
    4. **実行台帳の claim** (W1 Chunk A / A2): Lock 内・precondition と境界
       副作用より前に ``(judgment.{kind}, 冪等キー)`` の席を取る。既存
       running/applied/completed/unknown なら ``duplicate:<status>`` で即
       return — 境界副作用 (ライフ確定/終了) も走らない。``force=True`` は
       キーを None に落とす (debug 明示発火の口)。``resume_execution_id`` は
       回復 refire 用 — claim せず既存 prepared 行を使う。台帳の無い manager
       (旧テストスタブ) は WARN 一回で従来挙動に degrade
    5. ``precondition`` (あれば) を **Lock 取得後に** 再評価 — watchdog の
       day_open 再発火が、待っている間に済んだ本物の day_open と二重にならない。
       失敗時は claim 済みの席を放棄する (prepared 限定 CAS — 別 claimant が
       既に走らせている running 台帳は壊さない。放棄できなければ結果の
       ``outcome`` が indeterminate になり、呼び出し側は代替経路を走らせない)

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

    # 呼び出し側の契約検査は**席を取る前**に済ませる。claim の後で ValueError を
    # 出すと、その席は誰にも放棄されずに prepared のまま残り、回復 tick が
    # 同じ不正な payload で再発火しては同じ例外を繰り返す (2026-07-30 Codex
    # 五巡目)。配線ミスは台帳に触れる前に落とす。
    validate_judgment_context(kind, context)

    if not _is_active(manager, persona_id):
        LOGGER.debug(
            "[autonomy-wiring] %s skipped (persona=%s autonomy disabled)", kind, persona_id,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "persona autonomy disabled"}

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
        # 排他の層構造 (2026-07-31 席競合案件・九巡目裁定): 判断の直列化は
        # ①この per-persona Lock (本番の判断起動は全てここを通る — 同一
        # ペルソナの claim〜境界〜実行が interleave しない) と ②runtime_marker
        # (同一 DB の他プロセスを起動時に排除) が担う。台帳の CAS
        # (try_mark_running / abandon_prepared) はその下の**契約レベルの砦**
        # (直接呼び出し・将来のマルチプロセスへの防御) であって、Lock の
        # 代替ではない。ライフ境界処理が CAS より前にあるのはこの層構造の
        # 帰結 — 境界は Lock が直列化し、かつ confirm_life_for_today の冪等
        # (先勝ち) と節目の永続マーカーで多重実行にも耐える。
        # --- 実行台帳の claim (precondition・境界副作用より前、A2/D3) ---
        ledger = getattr(manager, "execution_ledger", None)
        execution_id: Optional[str] = None
        if ledger is None:
            if persona_id not in _LEDGER_MISSING_WARNED:
                _LEDGER_MISSING_WARNED.add(persona_id)
                LOGGER.warning(
                    "[autonomy-wiring] manager has no execution_ledger; "
                    "judgment points run without ledger tracking (persona=%s)",
                    persona_id,
                )
            execution_id = resume_execution_id
        elif resume_execution_id is not None:
            # 回復 refire (D5): claim せず既存 prepared 行を使う
            try:
                row = ledger.get_execution(resume_execution_id)
            except Exception:
                LOGGER.warning(
                    "[autonomy-wiring] resume target %s could not be read; "
                    "skipping %s (persona=%s)",
                    resume_execution_id, kind, persona_id, exc_info=True,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": "resume execution not found",
                        "execution_id": resume_execution_id}
            if row.get("status") != "prepared":
                LOGGER.info(
                    "[autonomy-wiring] resume target %s is %s (not prepared); "
                    "skipping %s (persona=%s)",
                    resume_execution_id, row.get("status"), kind, persona_id,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": f"resume target not prepared: {row.get('status')}",
                        "execution_id": resume_execution_id}
            execution_id = resume_execution_id
        else:
            idempotency_key = (
                None if force
                else _judgment_idempotency_key(manager, persona_id, kind, context)
            )
            execution_id, runnable, existing_status = ledger.claim_execution(
                f"judgment.{kind}",
                idempotency_key=idempotency_key,
                persona_id=persona_id,
                payload=_serialize_judgment_context(context),
            )
            if not runnable:
                LOGGER.info(
                    "[autonomy-wiring] %s duplicate (status=%s); skipping "
                    "(persona=%s execution=%s)",
                    kind, existing_status, persona_id, execution_id,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": f"duplicate:{existing_status}",
                        "execution_id": execution_id}

        if precondition is not None:
            try:
                still_needed = bool(precondition())
            except Exception:
                LOGGER.warning(
                    "[autonomy-wiring] precondition for %s raised; skipping "
                    "(persona=%s)", kind, persona_id, exc_info=True,
                )
                outcome = _release_claimed_seat(
                    ledger, execution_id, "precondition raised"
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False, "reason": "precondition raised",
                        "outcome": outcome, "execution_id": execution_id}
            if not still_needed:
                LOGGER.info(
                    "[autonomy-wiring] %s no longer needed at dispatch; skipping "
                    "(persona=%s)", kind, persona_id,
                )
                outcome = _release_claimed_seat(
                    ledger, execution_id, "precondition rejected"
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False, "reason": "precondition not met",
                        "outcome": outcome, "execution_id": execution_id}

        if kind == KIND_DAY_OPEN:
            if not _confirm_life_at_day_open(manager, persona_id, context or {}):
                # ライフ確定 / 開始の節目が決着していない (Codex W3 第八陣 —
                # day_close の失敗伝播の鏡像)。判断を走らせず失敗を発火結果へ
                # 伝播し、schedule 側 backoff / watchdog に再試行させる。
                LOGGER.warning(
                    "[autonomy-wiring] day_open aborted: life-start boundary "
                    "failed; leaving retry to the schedule backoff "
                    "(persona=%s)", persona_id,
                )
                outcome = _release_claimed_seat(
                    ledger, execution_id, "life-start boundary failed"
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": "life-start boundary failed",
                        "outcome": outcome, "execution_id": execution_id}
        elif kind == KIND_DAY_CLOSE:
            if not _apply_life_end_at_day_close(manager, persona_id):
                # ライフ終了の節目が決着していない (Codex W3 第五陣 P2)。
                # ここで判断を走らせて成功すると schedule と判断台帳が
                # completed になり、同一営業日の再試行が二度と来ない —
                # 失敗を発火結果へ伝播し、schedule 側 backoff に再試行させる。
                # 判断行は放棄 (prepared 限定 CAS → failed、副作用ゼロ) し、
                # 再試行の claim がキーを退避して新しい prepared を取れるように
                # する。
                LOGGER.warning(
                    "[autonomy-wiring] day_close aborted: life-end boundary "
                    "failed; leaving retry to the schedule backoff "
                    "(persona=%s)", persona_id,
                )
                outcome = _release_claimed_seat(
                    ledger, execution_id, "life-end boundary failed"
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": "life-end boundary failed",
                        "outcome": outcome, "execution_id": execution_id}

        result = run_judgment_point(
            manager, persona_id, kind, context, execution_id=execution_id,
        )

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
) -> bool:
    """day_open 発火経路でのライフ確定 (life.md v0.5 §4/§11.2)。

    区間はユーザーが設定した起床・就寝 (PersonaSchedule、
    :func:`_find_day_schedules` が解決)、予算とモード上書きは ``context``
    経由のユーザー設定値 (``daily_budget_pulses``・``life_mode_override``、
    無ければそれぞれ最低値・自動判定)。確定は冪等
    (:func:`~saiverse.day_plan.confirm_life_for_today` が既存確定を保持する)。

    節目処理 (:func:`~saiverse.day_plan._handle_life_start`) の一度きり保証は
    lives[0] の永続マーカー ``started`` が持つ (Codex W3 第八陣 —
    :func:`_apply_life_end_at_day_close` の ``ended`` の鏡像)。従来の
    「当日はじめての確定のときだけ」ガードは、**確定は済んだが節目が失敗した**
    場合に TTL override と「（活動開始）」通知を永久スキップしてしまう —
    確認 → 適用 → マークの順で、部分失敗は再試行で回復できる形にする。

    Returns:
        境界が決着したか。True = 確定+節目適用済み (今回適用 / マーカーあり /
        ライフの無い設定)。False = 失敗 — 呼び出し元 (day_open 分岐) は判断を
        走らせずに ``submitted=False`` で戻し、schedule 側 backoff に再試行
        させる (day_close と同じ失敗伝播)。
    """
    from saiverse import day_plan

    plan_date = _day_open_plan_date()
    existing = day_plan.get_lives(manager, persona_id, plan_date)
    already_started = bool(existing) and bool(existing[0].get("started"))
    sched = _find_day_schedules(manager, persona_id)
    budget = context.get("daily_budget_pulses") if isinstance(context, dict) else None
    mode_override = context.get("life_mode_override") if isinstance(context, dict) else None
    try:
        life = day_plan.confirm_life_for_today(
            manager, persona_id, plan_date,
            sched.get("wake"), sched.get("close"),
            requested_budget_pulses=budget,
            mode_override=mode_override,
        )
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to confirm today's life at day_open "
            "(persona=%s date=%s)", persona_id, plan_date, exc_info=True,
        )
        return False
    if life is None:
        return True  # 起床・就寝未設定 = ライフ無しの日 (決着)
    if already_started:
        LOGGER.info(
            "[autonomy-wiring] life start already applied for this business "
            "day; skipping boundary side effects (persona=%s date=%s)",
            persona_id, plan_date,
        )
        return True
    # W5: 節目の適用は day_plan.apply_life_boundary が実行台帳の下で決着させる
    # — マーカーと「（活動開始）」通知 outbox が world DB の単一 commit になり、
    # 旧「マーク失敗の無条件 True + 即時リトライ」暫定 (W3 第六陣裁定) は撤去。
    try:
        return day_plan.apply_life_boundary(
            manager, persona_id, plan_date, life, boundary="start",
        )
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] life-start processing failed "
            "(persona=%s date=%s)", persona_id, plan_date, exc_info=True,
        )
        return False


def _apply_life_end_at_day_close(manager: Any, persona_id: str) -> bool:
    """day_close 発火経路でのライフ終了処理 (life.md v0.5 §4.1/§11.2)。

    「ライフ終了 = 就寝判断 (day_close) の発火」そのもの — 専用のライフ境界
    イベント予約は v0.5 で廃止した。営業日 (覚醒日) の算出は
    ``judgment_points.build_judgment_args`` の KIND_DAY_CLOSE 分岐と同じ規則
    (深夜跨ぎリズムでは 01:00 発火の day_close は前日が営業日)。

    冪等ガード (2026-07-20 Codex W3 第二陣 P1 → W5 で恒久化): 節目処理は
    非冪等 (keep-alive cancel + TTL 同期 + 「（活動終了）」通知) なので、
    lives[0] の永続マーカー ``ended`` で **(persona, 営業日) につき一度**を
    保証する。W5 以降、適用の実体は
    :func:`~saiverse.day_plan.apply_life_boundary` — 実行台帳の claim の下で
    マーカーと通知 outbox を world DB の単一 commit で確定し (旧「適用〜
    マーク間 crash の at-least-once 窓」の解消)、配送は outbox_id 冪等。
    fast-path ガードをこの関数に置くのは、schedule 再試行・watchdog・手動
    発火のどの入口から来ても守られる位置だから。

    Returns:
        境界が決着したか (Codex W3 第五陣 P2)。True = 適用済み (今回適用 /
        適用済みマーカーあり / lives の無い日)。False = 今回の適用が失敗 —
        呼び出し元 (:func:`fire_judgment_point` の day_close 分岐) は**判断を
        走らせずに** ``submitted=False`` で戻ること。判断が成功してしまうと
        schedule と判断台帳が completed になり同一営業日の再試行が二度と来ず、
        失敗した節目 (活動終了通知等) が永久に欠落する — 「再試行が回復する」
        は、失敗をここで発火結果まで伝播させて初めて成立する。
    """
    from saiverse import day_plan

    plan_date = _day_close_plan_date(manager, persona_id)
    lives = day_plan.get_lives(manager, persona_id, plan_date)
    if not lives:
        return True
    if lives[0].get("ended"):
        LOGGER.info(
            "[autonomy-wiring] life end already applied for this business day; "
            "skipping boundary side effects (persona=%s date=%s)",
            persona_id, plan_date,
        )
        return True
    # W5: 節目の適用は day_plan.apply_life_boundary が実行台帳の下で決着させる
    # — マーカーと「（活動終了）」通知 outbox が world DB の単一 commit になり、
    # W3 第六陣 P2 の恒久解 (at-least-once 窓とマーク失敗の無条件 True の撤去)。
    # False は従来どおり呼び出し元 (day_close 分岐) が判断を打ち切って
    # ``submitted=False`` で schedule backoff に乗せる。
    try:
        return day_plan.apply_life_boundary(
            manager, persona_id, plan_date, lives[0], boundary="end",
        )
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] life-end processing failed (persona=%s date=%s)",
            persona_id, plan_date, exc_info=True,
        )
        return False


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
            life.md v0.5 §4.2) と ``life_mode_override``
            ("even"/"free"、モードの明示上書き。ライフ設定 UI の高度な設定
            からの脱出口。§5.1) を context に透過する。
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
        from saiverse.day_plan import LIFE_MODES

        budget = params.get("daily_budget_rounds")
        if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1:
            context["daily_budget_rounds"] = budget
        budget_pulses = params.get("daily_budget_pulses")
        if isinstance(budget_pulses, int) and not isinstance(budget_pulses, bool) \
                and budget_pulses >= 1:
            context["daily_budget_pulses"] = budget_pulses
        mode_override = params.get("life_mode_override")
        if isinstance(mode_override, str) and mode_override in LIFE_MODES:
            context["life_mode_override"] = mode_override

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


def _reaction_from_ledger(manager: Any, execution_id: Optional[str]) -> Optional[str]:
    """callback で reaction が読めなかったときのフォールバック: 台帳 RESULT_JSON。

    D6 の RESULT_JSON 標準 (on_event は ``reaction`` を含む) を読む口。
    NOTE: finalize の台帳化 (W1 Chunk B) までは RESULT_JSON は常に None なので、
    実際に値が返るのは Chunk B 以降 — ここは分岐だけ先に用意しておく。
    """
    if not execution_id:
        return None
    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None:
        return None
    try:
        row = ledger.get_execution(execution_id)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] failed to read ledger result for reaction "
            "fallback (execution=%s)", execution_id, exc_info=True,
        )
        return None
    result = row.get("result")
    if isinstance(result, dict):
        reaction = result.get("reaction")
        if isinstance(reaction, str) and reaction:
            LOGGER.info(
                "[autonomy-wiring] reaction read from ledger RESULT_JSON "
                "fallback: %s (execution=%s)", reaction, execution_id,
            )
            return reaction
    return None


def handle_external_event(
    manager: Any,
    persona_id: str,
    event_text: str,
    *,
    dispatch_direct: Callable[[], None],
    is_alert: bool = False,
    dispatch_envelope: Optional[Dict[str, Any]] = None,
) -> str:
    """実イベントの本番入口 (inject_persona_event の既定経路)。

    経路の判断基準:

    - **自律 OFF のペルソナ**: 従来どおり即応対 (``dispatch_direct``)。
      非自律ペルソナのイベント応答 (X メンション等) は v2 の管轄外で、
      従来挙動を壊さない
    - **ユーザー会話中**: on_event は撃たない (会話の至上性、judgment_points.md
      §7)。イベントは従来経路で応対 Pulse として submit され、PulseController の
      priority 制御 (user 優先) に従う
    - **自律 ON かつ手すき**: on_event 判断を撃つ。判断が ``engage_now`` を
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
        return ROUTE_DIRECT_AUTONOMY_DISABLED

    try:
        from saiverse.day_plan import is_in_user_conversation

        in_conversation = is_in_user_conversation(manager, persona_id)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] conversation check failed (persona=%s); "
            "treating as not in conversation", persona_id, exc_info=True,
        )
        in_conversation = False
    if in_conversation:
        dispatch_direct()
        return ROUTE_DIRECT_IN_CONVERSATION

    context: Dict[str, Any] = {"event_text": event_text, "is_alert": is_alert}
    if dispatch_envelope:
        # 応対の材料 (user_input / meta_playbook / args / event_type) を判断の
        # 台帳 payload に凍結する。LLM に渡る judgment_context には入らない
        # (build_judgment_args の on_event は選別したキーだけ組む) — 回復 tick の
        # 回収が engage_now の応対を初回と同じ入力で再構成するためだけの同乗
        context["dispatch_envelope"] = dispatch_envelope
    result = fire_judgment_point(manager, persona_id, KIND_ON_EVENT, context)
    if not result.get("submitted"):
        from saiverse.judgment_points import OUTCOME_INDETERMINATE

        if result.get("outcome") == OUTCOME_INDETERMINATE:
            # 席を放棄できていない = 判断がこの後 (別 claimant / 回復 tick で)
            # 走りうる。ここで応対すると同じイベントを二度処理する。
            LOGGER.warning(
                "[autonomy-wiring] on_event judgment left an unresolved "
                "execution (%s); not dispatching to avoid double handling "
                "(persona=%s execution=%s)",
                result.get("reason"), persona_id, result.get("execution_id"),
            )
            return ROUTE_NONE_INDETERMINATE
        LOGGER.info(
            "[autonomy-wiring] on_event judgment unavailable (%s); "
            "falling back to direct dispatch (persona=%s)",
            result.get("reason"), persona_id,
        )
        dispatch_direct()
        return ROUTE_DIRECT_JUDGMENT_UNAVAILABLE

    reaction = _extract_reaction(result)
    if reaction is None:
        # callback 消失時のフォールバック (D6): 台帳 RESULT_JSON から読む
        reaction = _reaction_from_ledger(manager, result.get("execution_id"))
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


#: :func:`_dispatch_recovered_event_response` の結末。再試行してよいのは
#: SAFE_FAILURE (副作用ゼロ確定) だけ — UNKNOWN (LLM が動いたか不明) を再試行
#: すると、発話・記憶更新まで進んだ応対をもう一度走らせて二重応対になる
#: (Codex 六巡目 high1。台帳の unknown = 自動再実行禁止と同じ規律)。
RECOVERED_DISPATCH_OK = "dispatched"
RECOVERED_DISPATCH_SAFE_FAILURE = "safe_failure"
RECOVERED_DISPATCH_UNKNOWN = "unknown"


def _dispatch_recovered_event_response(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """回収経路で engage_now と判断されたイベントの応対 Pulse を再構成して起動する。

    初回発火の応対経路 (inject_persona_event の ``_dispatch_direct`` closure) は
    回収側に存在しない。台帳 payload に凍結された ``dispatch_envelope``
    (組み立て済みの user_input / meta_playbook / args / event_type) をそのまま
    再送する — 初回とまったく同じ入力で応対が走る。envelope の無い古い payload
    (この機構の導入前の行) だけ、``event_text`` から同じ形の
    ``<system>[外部イベント通知]`` Pulse へ縮退する。建物はペルソナの現在地 —
    イベント到着時から移動していても、応対は「いま居る場所」で行うのが自然。

    submit は型付き経路 (``dispatch_schedule_fire``) で行い、顛末を
    ``ScheduleManager._classify_dispatch_outcome`` (確立済みの分類) で読む。

    Returns:
        :data:`RECOVERED_DISPATCH_OK` = 応対が完走した / queue に受付されて
        消えない。:data:`RECOVERED_DISPATCH_SAFE_FAILURE` = 副作用ゼロ確定で
        起動できなかった (dispatcher / persona / 現在地 / pulse_controller の
        不在、Beat 関所閉鎖、受付前の例外) — 再試行してよい。
        :data:`RECOVERED_DISPATCH_UNKNOWN` = 実行中の例外で LLM が動いたか
        不明 — 再試行してはいけない。
    """
    dispatcher = getattr(manager, "pulse_dispatcher", None)
    personas = getattr(manager, "all_personas", None) or getattr(
        manager, "personas", None
    ) or {}
    persona = personas.get(persona_id)
    building_id = getattr(persona, "current_building_id", None)
    if dispatcher is None or persona is None or not building_id:
        LOGGER.warning(
            "[autonomy-wiring] recovered on_event judged engage_now but the "
            "response could not be dispatched (dispatcher=%s persona=%s "
            "building=%s)", dispatcher is not None, persona is not None,
            building_id,
        )
        return RECOVERED_DISPATCH_SAFE_FAILURE
    envelope = context.get("dispatch_envelope")
    if isinstance(envelope, dict) and envelope.get("user_input"):
        user_input = str(envelope["user_input"])
        event_type = envelope.get("event_type")
        meta_playbook = envelope.get("meta_playbook") or "track_user_conversation"
        args = envelope.get("args")
    else:
        event_text = str(context.get("event_text") or "")
        user_input = f"""<system>
[外部イベント通知]
{event_text}
</system>"""
        event_type = None
        meta_playbook = "track_user_conversation"
        args = None
    result = dispatcher.dispatch_schedule_fire(
        persona_id=persona_id,
        building_id=building_id,
        user_input=user_input,
        metadata={"source": "external_event", "event_type": event_type,
                  "recovered": True},
        meta_playbook=meta_playbook,
        args=args,
    )
    # 遅延 import (schedule_manager とは疎結合のまま、確立済みの分類だけ借りる)
    from saiverse.schedule_manager import ScheduleManager

    outcome, detail = ScheduleManager._classify_dispatch_outcome(result)
    if outcome in ("executed", "accepted"):
        return RECOVERED_DISPATCH_OK
    if outcome == "failed":
        LOGGER.warning(
            "[autonomy-wiring] recovered on_event response did not start "
            "(%s); safe to retry (persona=%s)", detail, persona_id,
        )
        return RECOVERED_DISPATCH_SAFE_FAILURE
    LOGGER.error(
        "[autonomy-wiring] recovered on_event response ended unknown (%s); "
        "NOT retrying to avoid double handling (persona=%s)",
        detail, persona_id,
    )
    return RECOVERED_DISPATCH_UNKNOWN


#: 回収応対 (engage_now) の submit が明示失敗したときの in-process 再試行。
#: 判断台帳は既に終端しているため prepared 回収では拾えない — 凍結 envelope を
#: 抱えた揮発予約で再試行する (crash を跨ぐ永続化は C 案スコープの既知の限界)。
RECOVERED_RESPONSE_RETRY_BACKOFF_SECONDS = 120.0
RECOVERED_RESPONSE_MAX_ATTEMPTS = 3


def _schedule_recovered_response_retry(
    manager: Any,
    persona_id: str,
    context: Dict[str, Any],
    execution_id: Optional[str],
    attempt: int,
) -> None:
    """回収応対の明示失敗 (dispatcher 不在 / 関所閉鎖 / submit 例外) を再試行する。

    判断は completed 済みで台帳側の再試行装置が無いので、EventScheduler の揮発
    予約で bounded backoff を回す。上限で ERROR (イベント応対の消失を黙らせない)。
    """
    if attempt > RECOVERED_RESPONSE_MAX_ATTEMPTS:
        LOGGER.error(
            "[autonomy-wiring] recovered on_event response could not be "
            "dispatched after %d attempts; the event response is lost "
            "(persona=%s execution=%s)",
            RECOVERED_RESPONSE_MAX_ATTEMPTS, persona_id, execution_id,
        )
        return
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        LOGGER.error(
            "[autonomy-wiring] no event_scheduler to retry recovered response; "
            "the event response is lost (persona=%s execution=%s)",
            persona_id, execution_id,
        )
        return

    def _retry() -> None:
        outcome = _dispatch_recovered_event_response(manager, persona_id, context)
        if outcome == RECOVERED_DISPATCH_OK:
            LOGGER.info(
                "[autonomy-wiring] recovered on_event response dispatched on "
                "retry %d (persona=%s execution=%s)",
                attempt, persona_id, execution_id,
            )
            return
        if outcome == RECOVERED_DISPATCH_UNKNOWN:
            # LLM が動いたか不明 — 再試行すると二重応対になりうる。ここで打ち切る
            # (ERROR は _dispatch_recovered_event_response が出している)
            return
        _schedule_recovered_response_retry(
            manager, persona_id, context, execution_id, attempt + 1,
        )

    scheduler.schedule(
        fire_at=clock.now() + timedelta(
            seconds=RECOVERED_RESPONSE_RETRY_BACKOFF_SECONDS
        ),
        callback=_retry,
        key=f"recovered_response:{execution_id}",
    )
    LOGGER.warning(
        "[autonomy-wiring] recovered on_event response submit failed; retry "
        "%d/%d scheduled (persona=%s execution=%s)",
        attempt, RECOVERED_RESPONSE_MAX_ATTEMPTS, persona_id, execution_id,
    )


def refire_judgment_from_recovery(
    manager: Any,
    persona_id: str,
    judgment_kind: str,
    context: Optional[Dict[str, Any]],
    execution_id: str,
) -> Dict[str, Any]:
    """回復 tick の prepared 回収からの refire 入口 (execution_ledger_wiring 用)。

    ``fire_judgment_point`` を resume で呼び、**on_event の判断が engage_now を
    選んだときは応対まで起動する** — 判断だけ成功させて応対を落とすと、alert
    (engage_now しか選べない) では「回収成功に見えて通知への応答が必ず欠落する」
    (docs/issues/judgment_seat_contention_and_event_loss.md ③、Codex 三巡目)。
    reaction が読めなかった場合は応対を起動しない (handle_external_event の
    unknown_reaction と同じ裁定 — 二重応対の方が害が大きい)。
    """
    result = fire_judgment_point(
        manager, persona_id, judgment_kind,
        context=context, resume_execution_id=execution_id,
    )
    if judgment_kind != KIND_ON_EVENT or not result.get("submitted"):
        return result
    reaction = _extract_reaction(result)
    if reaction is None:
        reaction = _reaction_from_ledger(manager, result.get("execution_id"))
    if reaction != "engage_now":
        return result
    outcome = _dispatch_recovered_event_response(manager, persona_id, context or {})
    if outcome == RECOVERED_DISPATCH_OK:
        LOGGER.info(
            "[autonomy-wiring] recovered on_event judged engage_now; response "
            "re-dispatched from frozen payload (persona=%s execution=%s)",
            persona_id, execution_id,
        )
    elif outcome == RECOVERED_DISPATCH_SAFE_FAILURE:
        # 副作用ゼロ確定の明示失敗 (dispatcher 不在 / 関所閉鎖 / 受付前の例外) —
        # 判断台帳は completed 済みで prepared 回収に戻れないため、揮発予約の
        # bounded backoff で応対だけ再試行する (Codex 五巡目 high3)。unknown
        # (実行中の例外) は再試行しない — 二重応対の方が害が大きい
        _schedule_recovered_response_retry(
            manager, persona_id, context or {}, execution_id, attempt=1,
        )
    return result


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

    - 自律 ON・起床時間帯 (day_open スケジュールの時刻〜day_close の時刻)・
      **今日の day_plan 行が無い、または行はあるがコマが 1 件も無い**
      → day_open を発火し直す (起床時刻にサーバーが落ちていた / 途中で
      自律 ON になった等に加え、``confirm_life_for_today`` がライフ確定で
      day_plan 行を先に作った後、day_open の時間割編成が全滅してコマ 0 件の
      まま終わったケースも含む — 行の有無だけでは「未編成」を見落とす
      2026-07-14 実機の教訓)
    - plan はあるが pending / deferred コマの EventScheduler 予約が消えている
      (再起動等でインメモリ予約が失われた) → コマ予約を再 push する

    day_open / day_close の PersonaSchedule が無いペルソナ (v2 の一日リズム
    未設定) では何もしない。発火時は必ず INFO ログを残す。

    見張る対象の営業日は :func:`day_plan.resolve_business_day` が決める (現在
    時刻を含む確定ライフ優先。ライフを読めなければ ``skip`` して次の tick へ
    委ねる)。「いま何かすべき時間帯か」を決める窓・曜日のゲートは、走っている
    確定ライフが無いときだけ現行 PersonaSchedule で判定する — 走っているライフ
    があるなら、その区間そのものがそのペルソナの起きている時間帯。

    Returns:
        ``{"action": "none"|"skip"|"day_open_refire"|"reschedule", ...}``
        (観察・テスト用)。
    """
    if not _is_active(manager, persona_id):
        return {"action": "skip", "reason": "autonomy disabled"}

    sched = _find_day_schedules(manager, persona_id)
    wake = sched.get("wake")
    if not wake:
        return {"action": "skip", "reason": "no day_open schedule"}

    now = clock.now()
    hhmm = now.strftime("%H:%M")
    close = sched.get("close")

    from saiverse import day_plan

    # 営業日 (覚醒日) と、その日の暦日補正の起点を同じ解決器から取る
    # (day_plan.resolve_business_day — 現在時刻を含む確定ライフ優先)。起床設定を
    # 日中に変えた日でも「いま駆動中の時間割」を取り違えないため、かつ再起動
    # 回復 (reschedule_pending_slots の自己解決) と同じ答えを使うため
    # (Codex 八巡目 #1)。
    basis = day_plan.resolve_business_day(manager, persona_id, now=now)
    if basis is None:
        # ライフを読めなかった = どの営業日を見ているか分からない。plan の
        # 判定も予約の再 push もせず次の tick へ委ねる (Codex 八巡目 #2)。
        LOGGER.warning(
            "[watchdog] lives unreadable; skipping this tick (persona=%s)",
            persona_id,
        )
        return {"action": "skip", "reason": "lives unreadable"}

    # 窓・曜日のゲートは**確定ライフが走っていない場合だけ**現行 PersonaSchedule
    # で判定する。走っている確定ライフがあるなら、その区間こそがそのペルソナの
    # 起きている時間帯 — 日中に起床設定を変えた日は、まだ続いている前日のライフ
    # (例: 確定 23:00〜06:00 / 変更後の設定 07:00〜22:00 の深夜 00:30) が現行設定
    # の窓の外に落ち、その営業日の予約途絶を**二度と**検出できなくなる (次に窓が
    # 開く 07:00 にはライフが終わっていて解決器も当日へ退く。Codex 九巡目 #1)。
    if basis.source != "life":
        if not in_waking_window(hhmm, wake, close):
            # 起きていない時間帯 — before wake か after close か
            reason = "before wake" if hhmm < wake else "after close"
            return {"action": "none", "reason": reason}

        wake_days = sched.get("wake_days")
        if wake_days is not None:
            # 跨ぎリズムの深夜帯 (hhmm < wake) は「前日の weekday」が正しい対照日
            check_date = effective_plan_date(now, wake, close)
            if check_date.weekday() not in wake_days:
                return {"action": "none", "reason": "not a scheduled day"}

    today = basis.plan_date
    plan = day_plan.load_day_plan(manager, persona_id, today)
    if not plan:
        # 行そのものが無い (None) だけでなく、行はあるがコマ 0 件 ([]) も
        # 「未編成」— confirm_life_for_today がライフ確定時に meta 行を先に
        # 作るため、day_open の時間割編成が (丸めても救済できず) 全滅した日は
        # 行が存在したまま slots_json="[]" で残る。plan is None だけを見ると
        # この日は永久にリカバリ経路が無くなる (2026-07-14 実機の教訓)。
        #
        # day_open 再発火の制約: 見ている営業日が**暦日と同じとき**だけ撃つ。
        # 違うとき = 前の営業日がまだ終わっていない (深夜跨ぎの尻尾、または
        # 日付を跨いで続いている確定ライフ)。そこで新しい day_open を 00:30 に
        # 撃つのは誤り — 起きなかった日の深夜に plan を作れば時間割が即発火
        # するうえ、day_open が編成するのは**暦日**の時間割 (_day_open_plan_date)
        # なので、前日の空は埋まらないまま毎 tick 撃ち続けることになる。
        if today != now.date().isoformat():
            LOGGER.debug(
                "[watchdog] previous business day (%s) still in effect and has no "
                "plan; skipping day_open refire (persona=%s basis=%s)",
                today, persona_id, basis.source,
            )
            return {
                "action": "none",
                "reason": "previous business day still in effect: no day_open refire",
            }

        LOGGER.info(
            "[watchdog] no day plan (or zero organized slots) for today; "
            "re-firing day_open (persona=%s date=%s wake=%s)",
            persona_id, today, wake,
        )
        context: Dict[str, Any] = {}
        params = sched.get("day_open_params")
        if isinstance(params, dict):
            from saiverse.day_plan import LIFE_MODES

            budget = params.get("daily_budget_rounds")
            if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1:
                context["daily_budget_rounds"] = budget
            budget_pulses = params.get("daily_budget_pulses")
            if isinstance(budget_pulses, int) and not isinstance(budget_pulses, bool) \
                    and budget_pulses >= 1:
                context["daily_budget_pulses"] = budget_pulses
            mode_override = params.get("life_mode_override")
            if isinstance(mode_override, str) and mode_override in LIFE_MODES:
                context["life_mode_override"] = mode_override
        result = fire_judgment_point(
            manager, persona_id, KIND_DAY_OPEN, context,
            # Lock 待ちの間に本物の day_open が済んでいたら撃たない (二重編成防止)。
            # ここも「行が無い」でなく「コマが無い」で判定する (上と同じ理由)。
            precondition=lambda: not day_plan.load_day_plan(
                manager, persona_id, today,
            ),
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
        pushed = day_plan.reschedule_pending_slots(
            manager, persona_id, today, wake=basis.wake,
        )
        return {"action": "reschedule", "pushed": pushed, "lost": lost}

    return {"action": "none"}
