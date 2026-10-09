"""自律行動 v2 の本番配線 (活性化) — 判断点の恒久起動と watchdog。

``saiverse.judgment_points`` は「判断点そのもの」(状況テキスト・動的スキーマ・
Playbook 起動) を持つが、**自動起動の配線は持たない** (中間起動の空打ち防止)。
本モジュールがその配線を担う:

- :func:`fire_judgment_point` — 本番共通の起動ゲート。AUTONOMY_ENABLED の
  ペルソナのみ発火し (既存の自律ゲートの流儀)、判断点 Playbook が DB に無ければ
  エラーでなく WARNING + スキップ。MetaLayer の per-persona Lock で他のメタ判断
  (alert 即応等) と直列化する
- 起床・就寝 (PersonaSchedule の META_PLAYBOOK=``judgment_day_open`` /
  ``judgment_day_close`` の行) の発火は**判断点ではない** — ScheduleManager が
  ``saiverse.day_plan.handle_scheduled_life_boundary`` (ライフの確定と節目の
  機械の帳簿処理、LLM なし) へ直接回す (autonomous_behavior_v3.md §6 /
  autonomous_behavior_v04_plan.md 段 1-2)。Playbook 名は行の目印として残って
  いるだけ。起床・就寝時刻の出所は PersonaSchedule 行そのもの
  (:func:`_find_day_schedules`)
- :func:`handle_scheduled_judgment` — スケジュール行に判断点 Playbook 名が
  書かれていた場合の拒否口 (時刻駆動の判断点はもう無い)
- :func:`handle_conversation_end` — 会話終了 (沈黙タイマーの発火)。会話の
  出来事を閉じる (機械の帳簿処理のみ。会話終了判断は
  autonomous_behavior_v3.md §8/§13.3 で退役し、本人の声の捕獲は Metabolism の
  スルースへ一本化された)。発火の入口は
  ``saiverse.user_conversation.handle_conversation_timeout``
- :func:`handle_user_utterance_conflict` — 別の活動中に届いたユーザー発話の
  仲裁 (track_retirement.md §7.4 の直結化)。on_event 判断点を流用し、
  engage_now のときだけ会話を開始する
- :func:`handle_external_event` — 実イベント (inject_persona_event) の入口。
  自律 ON かつユーザー会話中でなければ **on_event** 判断を撃ち、判断が
  engage_now を選んだときだけ従来の応対 Pulse を起動する。自律 OFF のペルソナは
  従来どおり直接応対 (非自律ペルソナのイベント応答を壊さない)
- :func:`watchdog_tick` — AutonomyManager の定期 tick の縮退先 (v2 §4.2)。
  正常時は何もしない。「自律 ON・起床時間帯・今日のライフが無い or コマ予約が
  途絶」のときだけ起床の帳簿処理の火入れ直し / コマ予約の再 push を行う保守的な
  見張り

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。

駆動するかどうかは :func:`is_autonomy_on` 一箇所で、ペルソナごとの
``AUTONOMY_ENABLED`` だけで決まる。v0.3 (develop) にはこの判定を常に False に
する全体の止め具 (定数 ``AUTONOMOUS_DRIVING_SHIPPED``) があるが、develop-v0.4
では 2026-09-25 に定数ごと撤去した (正典は
``docs/intent/autonomous_behavior_v3.md`` §11.1)。
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import date, timedelta
from typing import Any, Callable, Dict, Optional

from saiverse import clock
from saiverse.judgment_points import (
    JUDGMENT_PLAYBOOK_MAP,
    KIND_DAY_CLOSE,
    KIND_DAY_OPEN,
    KIND_ON_EVENT,
    KIND_POST_SESSION,
    OUTCOME_ABORTED,
    OUTCOME_INDETERMINATE,
    OUTCOME_RAN,
    _abandon_seat,
    direct_fallback_allowed,
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

#: 起床・就寝スケジュール行の Playbook 名 → ライフの節目の種類
#: (``saiverse.day_plan.handle_scheduled_life_boundary`` の ``boundary``)。
#: この 2 つの行の発火は判断点ではなく機械の帳簿処理 (LLM なし) —
#: ScheduleManager がこの表で振り分ける。Playbook 名は行の目印として残る。
LIFE_BOUNDARY_PLAYBOOKS: Dict[str, str] = {
    JUDGMENT_PLAYBOOK_MAP[KIND_DAY_OPEN]: "start",
    JUDGMENT_PLAYBOOK_MAP[KIND_DAY_CLOSE]: "end",
}

#: handle_scheduled_judgment に起床・就寝の Playbook 名が届いたときの拒否理由
#: (ScheduleManager は LIFE_BOUNDARY_PLAYBOOKS で先に振り分けるので、届くのは
#: 配線ミスのときだけ)。
REASON_LIFE_BOUNDARY_MACHINE_ONLY = "life boundary is machine-only"

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
#: 判断のメタレーンは走ったが、成功の証跡なしに戻った (unknown / finalize 証跡
#: なし)。finalize が既に決定を適用しているかもしれないので、**代替経路を
#: 走らせない** — 走らせると判断の決定を上書きして応答してしまう (指摘 F3)。
ROUTE_NONE_JUDGMENT_RAN = "none:judgment_ran"
#: 刺激の ID (stimulus_id) が無いイベントが届いた。ID は供給源の義務なので
#: 代理採番せず、応対もしない (fail-closed — ERROR で表に出す)。
ROUTE_NONE_MISSING_STIMULUS_ID = "none:missing_stimulus_id"
#: 同じ刺激の再配送 (受領記録 stimulus_receipt に同じ ID が既にある)。
#: 直接応対も判断も起動しない。ラベルを返すのは入口の
#: ``inject_persona_event`` (受領の照合はそこで行う)。
ROUTE_NONE_DUPLICATE_STIMULUS = "none:duplicate_stimulus"
#: 別行動中のユーザー発話に発話の ID (message_id 由来の stimulus_id) が
#: 付いていなかった。判断を冪等にできないので仲裁を経ずに直接会話を始める
#: (ユーザーの呼びかけを配線の不備で黙殺しない)。ERROR で表に出す。
ROUTE_DIRECT_MISSING_STIMULUS_ID = "direct:missing_stimulus_id"


# ---------------------------------------------------------------------------
# 共通ゲート
# ---------------------------------------------------------------------------


def _get_persona(manager: Any, persona_id: str) -> Optional[Any]:
    return (getattr(manager, "personas", None) or {}).get(persona_id)


def is_autonomy_on(manager: Any, persona_id: str) -> bool:
    """このペルソナの自律の駆動を回してよいか — **自律ゲートの唯一の判定**。

    判断点・watchdog・起動時のコマ再予約・実イベントの判断経由は、すべてこの
    関数を通ってから駆動する。

    判定:

    1. ペルソナが manager に居なければ False
    2. ペルソナの ``AUTONOMY_ENABLED`` (属性欠落は False 扱い)

    ⚠️ 設定値を読んで表示・保存するだけの場所 (設定 API・inspect_world・clone
    スクリプト) はこの関数を通さないこと — ここは「駆動するか」の判定の口で、
    設定値そのものの読み書きの口ではない。
    """
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
    """MetaLayer の per-persona Lock。判断 Pulse の直列化を共有する。

    Lock が取れない世界で無 Lock (nullcontext) に倒すと、同一ペルソナの判断
    Pulse の直列化が黙って外れる。``manager.meta_layer`` は SAIVerseManager が
    無条件に持つので、壊れていれば例外で止まる方が正しい (fail-closed)。
    """
    return manager.meta_layer._get_lock(persona_id)


# ---------------------------------------------------------------------------
# 実行台帳との結線 (W1 Chunk A: A2 の重複抑止 + A7 の durable queue)
# ---------------------------------------------------------------------------


def _day_open_plan_date() -> str:
    """起床の plan_date (暦日)。ライフ確定
    (``day_plan.handle_scheduled_life_boundary`` の start) と冪等キー
    (:func:`_judgment_idempotency_key`) で必ず同源を使う (D1)。"""
    return clock.now().date().isoformat()


def _day_close_plan_date(manager: Any, persona_id: str) -> str:
    """就寝の営業日 (覚醒日)。ライフ終了
    (``day_plan.handle_scheduled_life_boundary`` の end) と冪等キーで同源。
    judgment_points.build_judgment_args の KIND_DAY_CLOSE 分岐と同じ規則
    (深夜跨ぎリズムでは 01:00 の就寝は前日が営業日)。"""
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
    - post_session: ``{persona}:{episode_ref}``。
      episode_ref が無ければ None (一意性なし)
    - on_event: ``{persona}:{stimulus_id}`` — 刺激 (外部イベント / 別行動中の
      ユーザー発話) の供給源が発行した永続 ID から作る。同じ刺激の再配送が
      別の席を取って判断を二度走らせる穴を塞ぐ
      (docs/issues/on_event_judgment_has_no_idempotency_key.md)。入口
      (:func:`handle_external_event` / :func:`handle_user_utterance_conflict`)
      が ID の無い刺激をここへ通さないので、None になるのは ID の義務化より
      前に作られた台帳行の再発火 (resume はキーを計算しない) か配線ミスだけ
      — 後者は WARNING で表に出して従来どおり一意性なしで走らせる。
      **prepared 行が durable queue** (A7/D5) なのは変わらない
    """
    if kind == KIND_DAY_OPEN:
        return f"{persona_id}:{_day_open_plan_date()}"
    if kind == KIND_DAY_CLOSE:
        return f"{persona_id}:{_day_close_plan_date(manager, persona_id)}"
    if kind == KIND_POST_SESSION:
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
    if kind == KIND_ON_EVENT:
        stimulus_id = context.get("stimulus_id") if isinstance(context, dict) else None
        if isinstance(stimulus_id, str) and stimulus_id.strip():
            return f"{persona_id}:{stimulus_id}"
        LOGGER.warning(
            "[autonomy-wiring] on_event judgment without a stimulus_id "
            "(persona=%s); claiming a seat without idempotency", persona_id,
        )
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
    4. **実行台帳の claim** (W1 Chunk A / A2): Lock 内・precondition より前に
       ``(judgment.{kind}, 冪等キー)`` の席を取る。既存
       running/applied/completed/unknown なら ``duplicate:<status>`` で即
       return。``force=True`` は
       キーを None に落とす (debug 明示発火の口)。``resume_execution_id`` は
       回復 refire 用 — claim せず既存 prepared 行を使う。台帳の無い manager
       (旧テストスタブ) は WARN 一回で従来挙動に degrade
    5. ``precondition`` (あれば) を **Lock 取得後に** 再評価 — Lock を待って
       いる間に状況が変わった発火を二重に走らせない。
       失敗時は claim 済みの席を放棄する (prepared 限定 CAS — 別 claimant が
       既に走らせている running 台帳は壊さない。放棄できなければ結果の
       ``outcome`` が indeterminate になり、呼び出し側は代替経路を走らせない)

    ライフ (活動区間) の確定と起床・就寝の節目処理はここでは行わない —
    2026-10 (autonomous_behavior_v04_plan.md 段 1-2) に
    ``saiverse.day_plan.handle_scheduled_life_boundary`` (機械の帳簿処理) へ
    切り出した。本番で day_open / day_close をここへ撃つ経路はもう無い
    (ScheduleManager・watchdog・回復 tick のいずれも撃たない)。

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

    if not is_autonomy_on(manager, persona_id):
        LOGGER.debug(
            "[autonomy-wiring] %s skipped (persona=%s autonomy disabled)", kind, persona_id,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "persona autonomy disabled",
                "outcome": OUTCOME_ABORTED}

    if not playbook_available(manager, playbook_name):
        LOGGER.warning(
            "[autonomy-wiring] judgment playbook %r is not in DB; skipping %s "
            "(persona=%s). Import it with: python scripts/import_playbook.py "
            "--file builtin_data/playbooks/public/%s.json",
            playbook_name, kind, persona_id, playbook_name,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "playbook not imported",
                "outcome": OUTCOME_ABORTED}

    with _judgment_lock(manager, persona_id):
        # 排他の層構造 (2026-07-31 席競合案件・九巡目裁定): 判断の直列化は
        # ①この per-persona Lock (本番の判断起動は全てここを通る — 同一
        # ペルソナの claim〜実行が interleave しない) と ②runtime_marker
        # (同一 DB の他プロセスを起動時に排除) が担う。台帳の CAS
        # (try_mark_running / abandon_prepared) はその下の**契約レベルの砦**
        # (直接呼び出し・将来のマルチプロセスへの防御) であって、Lock の
        # 代替ではない。
        # --- 実行台帳の claim (precondition より前、A2/D3) ---
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
                        "outcome": OUTCOME_INDETERMINATE,
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
                        "outcome": OUTCOME_INDETERMINATE,
                        "execution_id": resume_execution_id}
            execution_id = resume_execution_id
        else:
            idempotency_key = (
                None if force
                else _judgment_idempotency_key(manager, persona_id, kind, context)
            )
            try:
                execution_id, runnable, existing_status = ledger.claim_execution(
                    f"judgment.{kind}",
                    idempotency_key=idempotency_key,
                    persona_id=persona_id,
                    payload=_serialize_judgment_context(context),
                )
            except Exception:
                # 台帳の claim が例外 (DB ロック競合・接続断等)。裸で上げると
                # 呼び出し側は結末を受け取れず、代替経路の可否を判定できないまま
                # 例外処理へ落ちる (2026-08-14 Codex 指摘)。**行が作られたかは
                # 分からない** — 作られていれば回復 tick が prepared を拾って
                # 判断を走らせるので、ここで代替応対すると二重になる。だから
                # indeterminate = 応答しない、として結果化する。
                LOGGER.error(
                    "[autonomy-wiring] ledger claim failed for %s (persona=%s); "
                    "cannot tell whether the row was created — treating as "
                    "indeterminate (no direct fallback)",
                    kind, persona_id, exc_info=True,
                )
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False, "reason": "ledger claim failed",
                        "outcome": OUTCOME_INDETERMINATE}
            if not runnable:
                LOGGER.info(
                    "[autonomy-wiring] %s duplicate (status=%s); skipping "
                    "(persona=%s execution=%s)",
                    kind, existing_status, persona_id, execution_id,
                )
                # running = 別の claimant が走行中 (席が残っている) /
                # applied・completed・unknown = その判断はもう走った。
                # どちらも呼び出し側の代替経路は禁止 (二重処理・決定の上書き)。
                return {"kind": kind, "playbook": playbook_name,
                        "submitted": False,
                        "reason": f"duplicate:{existing_status}",
                        "outcome": (OUTCOME_INDETERMINATE
                                    if existing_status == "running"
                                    else OUTCOME_RAN),
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


# ---------------------------------------------------------------------------
# 判断点 Playbook 名のスケジュール行 (ScheduleManager が呼ぶ拒否口)
# ---------------------------------------------------------------------------


def handle_scheduled_judgment(
    manager: Any,
    persona_id: str,
    playbook_name: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """PersonaSchedule の META_PLAYBOOK に判断点 Playbook 名が書かれた行の発火口。

    **時刻駆動の判断点はもう無い** — 起床・就寝 (``judgment_day_open`` /
    ``judgment_day_close``) の行は判断点ではなく機械の帳簿処理で、
    ScheduleManager が :data:`LIFE_BOUNDARY_PLAYBOOKS` で振り分けて
    ``saiverse.day_plan.handle_scheduled_life_boundary`` へ直接回す
    (autonomous_behavior_v3.md §6 / autonomous_behavior_v04_plan.md 段 1-2)。
    ここへ起床・就寝の名前が届くのは配線ミスなので、黙って LLM の判断へ
    流さず WARNING で拒否する (委ねると、同じ節目の入口が二本になる)。

    それ以外の判断点 (on_event / post_session) は文脈必須で、スケジュールから
    撃つと偽前提になるので WARNING + スキップ。

    Args:
        params: 使わない (呼び出し側の形を保つために残る)。
    """
    kind = PLAYBOOK_TO_KIND.get(playbook_name)
    if kind is None:
        LOGGER.warning(
            "[autonomy-wiring] scheduled playbook %r is not a judgment playbook; "
            "skipping (persona=%s)", playbook_name, persona_id,
        )
        return {"kind": None, "submitted": False, "reason": "not a judgment playbook"}
    if playbook_name in LIFE_BOUNDARY_PLAYBOOKS:
        LOGGER.warning(
            "[autonomy-wiring] scheduled %r reached the judgment entry; the life "
            "boundary is machine-only now and is routed to "
            "day_plan.handle_scheduled_life_boundary — refusing to run an LLM "
            "judgment (persona=%s)", playbook_name, persona_id,
        )
        return {"kind": kind, "submitted": False,
                "reason": REASON_LIFE_BOUNDARY_MACHINE_ONLY}
    LOGGER.warning(
        "[autonomy-wiring] judgment kind %r cannot be fired from a schedule; "
        "skipping (persona=%s)", kind, persona_id,
    )
    return {"kind": kind, "submitted": False, "reason": "kind not schedulable"}


# ---------------------------------------------------------------------------
# 会話終了 (沈黙タイマーの発火) の帳簿処理
# 発火の入口は saiverse.user_conversation.handle_conversation_timeout。
# ---------------------------------------------------------------------------


def handle_conversation_end(
    manager: Any, persona_id: str,
    *, expected_conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """対ユーザー会話の終了処理: 会話状態を落とす (機械の帳簿処理のみ)。

    会話終了判断 (post_conversation) は 2026-08-16 の裁定で退役した
    (autonomous_behavior_v3.md §8 / §13.3)。会話に切れ目は定義できず、
    「30 分沈黙 = 会話の終わり」という恣意的な仮定の上に立つ席だったため。
    本人の声の捕獲 (約束・やりたいこと・コア記憶) は Metabolism のスルースの
    一手へ一本化され、ここに残るのは待ちを閉じる帳簿処理だけになった。

    器は 2026-08-22 (束 6c) に「出来事の行を閉じる」から「メモリ内の会話状態を
    落とす」へ変わった (v3 §7)。条件付き解除の意味論はそのまま持ち越している。
    終わりを記録に残さないのは 2026-08-23 の裁定 (会話に区切りは保存しない =
    episode.md の不変条件)。

    **全体を会話ロック (``user_conversation.conversation_lock``) の中で行う**
    (2026-08-22 指摘 1)。二手 (状態を落とす → 予約の解除) の途中で新しい会話が
    開くと、後片付けがその会話の予約を消す並びが残るため、
    会話の開始と同じロックで直列化する。ロックが会話開始側の Pulse を含まない
    のは意図的で、詳細は ``user_conversation`` の「会話の開始と終了の排他」節。

    Args:
        expected_conversation_id: 呼び出し元が「この会話を終える」と決めた時点で
            見ていた会話。**指定すると、その会話が現行のままのときだけ落とす
            条件付き解除**になる。呼び出し元の検査から実際の実行までに間が空く
            経路 (debug の切り上げは背景スレッドで走る / 沈黙タイマーの callback は
            EventScheduler のスレッドで走る) で、その間に別経路が会話を閉じ /
            新しい会話を開いていたら、ここで閉じるのは**別の会話**になる
            (2026-08-14 Codex 二巡目・三巡目)。None なら「いま開いている会話」を
            閉じる。

    Returns:
        ``{"closed": bool, "conversation_id": str | None, "reason": str | None}``。
    """
    from saiverse import user_conversation as uc

    with uc.conversation_lock(persona_id):
        # 照合と解除は 1 手 (clear_open_conversation がロック内で行う) —— 「照合して
        # から落とす」間に別経路が閉じて新しい会話を開いたときに、別の会話を落とす窓を
        # 無くす (2026-08-14 Codex 三巡目の規律を器ごと引き継ぐ)。
        closed = uc.clear_open_conversation(
            manager, persona_id, expected_conversation_id=expected_conversation_id,
        )

        if closed is None:
            LOGGER.info(
                "[autonomy-wiring] no open conversation to end "
                "(persona=%s expected=%s)", persona_id, expected_conversation_id,
            )
            return {"closed": False, "conversation_id": None,
                    "reason": "no open conversation to end"}

        conversation_id = closed.get("conversation_id")

        # 会話が閉じた = 待ちも終わった。沈黙タイマーは一回限りの予約なので、
        # 自然発火した経路では既に消費されている。debug の切り上げのように外から
        # 閉じた経路では生きた予約が残るため、ここで解除する (残すと次の会話の
        # 途中で発火して、始まったばかりの会話を閉じてしまう)。
        # **いま閉じた会話の予約だけ**を対象にする — 別の会話の予約が生きている
        # なら、それはこの後片付けが触れてよい相手ではない。
        try:
            uc.cancel_conversation_timeout(
                manager, persona_id, expected_conversation_id=conversation_id,
            )
        except Exception:
            LOGGER.warning(
                "[autonomy-wiring] failed to cancel the conversation timeout "
                "(persona=%s)", persona_id, exc_info=True,
            )

    LOGGER.info(
        "[autonomy-wiring] closed the conversation %s (persona=%s)",
        conversation_id, persona_id,
    )
    return {"closed": True, "conversation_id": conversation_id, "reason": None}


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
    stimulus_id: Optional[str],
    dispatch_direct: Callable[[], None],
    is_alert: bool = False,
    dispatch_envelope: Optional[Dict[str, Any]] = None,
) -> str:
    """実イベントの本番入口 (inject_persona_event の既定経路)。

    ``stimulus_id`` (刺激の永続 ID) は**必須**。None / 空なら ERROR を出して
    応対も判断も起動せずに :data:`ROUTE_NONE_MISSING_STIMULUS_ID` を返す
    (ID は供給源の義務で、ここで代理採番しない — fail-closed)。ID は on_event
    判断の冪等キー (``{persona}:{stimulus_id}``) になる。

    同じ刺激の再配送を止める受領の照合 (``saiverse.stimulus_receipt``) は
    この関数ではなく、呼び出し元の入口 ``inject_persona_event`` の先頭で行う —
    この関数を通らない経路 (meta_playbook を明示した直接応対) と、ここより前に
    走る persona_event_log への記録まで含めて守るため。この関数を新しい場所から
    呼ぶときは、先に :func:`saiverse.stimulus_receipt.claim_stimulus` を通すこと。

    経路の判断基準:

    - **自律 OFF のペルソナ**: 従来どおり即応対 (``dispatch_direct``)。
      非自律ペルソナのイベント応答 (X メンション等) は v2 の管轄外で、
      従来挙動を壊さない
    - **ユーザー会話中**: on_event は撃たない (会話の至上性、judgment_points.md
      §7)。イベントは従来経路で応対 Pulse として submit され、PulseController の
      priority 制御 (user 優先) に従う
    - **自律 ON かつ手すき**: on_event 判断を撃つ。判断が ``engage_now`` を
      選んだときだけ従来の応対 Pulse を起動する。add_task / note_only /
      ignore は finalize が適用済みなので応対は起動しない
    - 判断が LLM へ渡る前に止まった / 副作用ゼロ確定で失敗した (Playbook
      未 import・関所閉鎖・LLM エラー等) 場合はイベントを落とさないよう従来経路へ
      フォールバックする。判定は :func:`~saiverse.judgment_points.
      direct_fallback_allowed` (結末の無い結果は拒否側に倒す)
    - 判断が走った後、成功の証跡なく戻った場合は応対を起動しない — finalize が
      決定を適用済みかもしれず、応対するとそれを上書きする (2026-08-14 F3)
    - 判断は走ったが reaction が読めなかった場合は応対を起動しない
      (二重応対の方が害が大きい。WARNING で観察可能にする)

    Returns:
        経路ラベル (``direct:*`` / ``judged:*``)。ログ・テストの観察用。
    """
    if not isinstance(stimulus_id, str) or not stimulus_id.strip():
        LOGGER.error(
            "[autonomy-wiring] external event without a stimulus_id reached "
            "persona %s; not responding (the source must issue a durable ID)",
            persona_id,
        )
        return ROUTE_NONE_MISSING_STIMULUS_ID

    if not is_autonomy_on(manager, persona_id):
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

    context: Dict[str, Any] = {
        "event_text": event_text,
        "is_alert": is_alert,
        # 冪等キー (_judgment_idempotency_key) とタスク帳の出どころ参照に使う
        "stimulus_id": stimulus_id,
    }
    if dispatch_envelope:
        # 応対の材料 (user_input / meta_playbook / args / event_type) を判断の
        # 台帳 payload に凍結する。LLM に渡る judgment_context には入らない
        # (build_judgment_args の on_event は選別したキーだけ組む) — 回復 tick の
        # 回収が engage_now の応対を初回と同じ入力で再構成するためだけの同乗
        context["dispatch_envelope"] = dispatch_envelope
    result = fire_judgment_point(manager, persona_id, KIND_ON_EVENT, context)
    if not result.get("submitted"):
        if direct_fallback_allowed(result):
            LOGGER.info(
                "[autonomy-wiring] on_event judgment unavailable (%s); "
                "falling back to direct dispatch (persona=%s)",
                result.get("reason"), persona_id,
            )
            dispatch_direct()
            return ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
        if result.get("outcome") == OUTCOME_RAN:
            # 判断は走った後で証跡なく戻った。finalize が note_only 等を適用済み
            # かもしれないので、ここで応対すると決定を上書きする。
            LOGGER.warning(
                "[autonomy-wiring] on_event judgment ran without evidence of "
                "success (%s); not dispatching to avoid overriding the "
                "judgment (persona=%s execution=%s)",
                result.get("reason"), persona_id, result.get("execution_id"),
            )
            return ROUTE_NONE_JUDGMENT_RAN
        # 席を放棄できていない = 判断がこの後 (別 claimant / 回復 tick で)
        # 走りうる。ここで応対すると同じイベントを二度処理する。
        LOGGER.warning(
            "[autonomy-wiring] on_event judgment left an unresolved "
            "execution (%s); not dispatching to avoid double handling "
            "(persona=%s execution=%s)",
            result.get("reason"), persona_id, result.get("execution_id"),
        )
        return ROUTE_NONE_INDETERMINATE

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


def handle_user_utterance_conflict(
    manager: Any,
    persona_id: str,
    utterance_text: str,
    *,
    engage: Callable[[], None],
    user_id: str,
    stimulus_id: Optional[str],
) -> str:
    """別の活動中に届いたユーザー発話の仲裁 (track_retirement.md §7.4 の直結化)。

    旧経路 (set_alert → MetaLayer の v1 メタ判断) の置き換え。ユーザー発話を
    「別行動中に外から届いた刺激」の一種として **on_event** 判断点へ直結し、
    判断が engage_now を選んだときだけ ``engage()`` (会話の開始 = 会話の出来事を
    開いて main_line を走らせる) を呼ぶ。engage_now 以外なら応答しない
    (旧 alert 経路で activate されなかったときと同じ挙動)。

    経路の判断基準 (:func:`handle_external_event` と同じ流儀):

    - **自律 OFF のペルソナ**: 判断を経ず直接 ``engage()`` (常に応答)
    - **判断が LLM へ渡る前に止まった / 副作用ゼロ確定で失敗した**
      (Playbook 未 import・関所閉鎖・LLM エラー等): 直接 ``engage()``。
      ユーザーの呼びかけを機構の不備で黙殺する方が害が大きい
    - **判断は走ったが成功の証跡なく戻った** (ran): 応答しない。finalize が
      note_only 等を適用済みかもしれず、応答すると決定を上書きする
    - **判断は受け付けられたが席が未解決** (indeterminate): 応答しない。
      回復 tick が同じ判断を後で走らせうるため、ここで応答すると二重になる
    - **判断は走ったが reaction が読めなかった**: 応答しない (二重応対の回避)

    「起動できなかった」かどうかは :func:`~saiverse.judgment_points.
    direct_fallback_allowed` が結末 (``outcome``) から決める — **結末の無い結果は
    拒否側に倒す**。この判定を呼び出し側でやり直さないこと (2026-08-14 F3)。

    Args:
        engage: 会話を開始する closure
            (``saiverse.user_conversation.start_conversation``)。
        user_id: 応対先のユーザー。``engage`` が閉じ込んでいるのと同じ相手を
            **台帳 payload にも凍結する** — 判断が席を残したまま落ちて回復 tick が
            後から engage_now を出したとき、回収側はこのユーザー相手の会話を
            開いて応対する。凍結が無いと回収側は応対先を知らず、ユーザーの発話を
            「外部イベント通知」の形で流し込むしかない (会話の出来事も開かない =
            帳簿の乖離。2026-08-14 Codex 指摘 F4)。
        stimulus_id: 発話の永続 ID (``"msg:<building_messages の message_id>"``)。
            on_event 判断の冪等キーになる。**受領記録 (stimulus_receipt) は
            取らない** — 発話の再送は building_messages の ``client_message_id``
            UNIQUE が入口で止めるため、ここへ届く時点で再送ではない。ID が無い
            (配線ミス) ときは判断を冪等にできないので、ERROR を出して仲裁を経ずに
            直接 ``engage()`` する (ユーザーの呼びかけを黙殺しない)。

    Returns:
        経路ラベル (``direct:*`` / ``judged:*`` / ``none:*``)。ログ・テスト用。
    """
    if not is_autonomy_on(manager, persona_id):
        engage()
        return ROUTE_DIRECT_AUTONOMY_DISABLED

    if not isinstance(stimulus_id, str) or not stimulus_id.strip():
        LOGGER.error(
            "[autonomy-wiring] utterance-conflict without a stimulus_id "
            "(persona=%s); the judgment cannot be made idempotent — starting "
            "the conversation directly", persona_id,
        )
        engage()
        return ROUTE_DIRECT_MISSING_STIMULUS_ID

    context: Dict[str, Any] = {
        "event_text": f"ユーザーがあなたに話しかけました:\n{utterance_text}",
        "is_alert": False,
        # 回収側が「これは外部イベントではなくユーザー発話の仲裁」と判るための
        # 種別と応対先。LLM へ渡る judgment_context には入らない
        # (build_judgment_args の on_event は選別したキーだけ組む) — 台帳 payload
        # に凍結して回復経路が読むためだけの同乗。
        "utterance_conflict": True,
        "conversation_user_id": user_id,
        # 冪等キー (_judgment_idempotency_key) とタスク帳の出どころ参照に使う
        "stimulus_id": stimulus_id,
    }
    result = fire_judgment_point(manager, persona_id, KIND_ON_EVENT, context)
    if not result.get("submitted"):
        if direct_fallback_allowed(result):
            LOGGER.info(
                "[autonomy-wiring] utterance-conflict judgment unavailable (%s); "
                "starting the conversation directly (persona=%s)",
                result.get("reason"), persona_id,
            )
            engage()
            return ROUTE_DIRECT_JUDGMENT_UNAVAILABLE
        if result.get("outcome") == OUTCOME_RAN:
            LOGGER.warning(
                "[autonomy-wiring] utterance-conflict judgment ran without "
                "evidence of success (%s); not responding to avoid overriding "
                "the judgment (persona=%s execution=%s)",
                result.get("reason"), persona_id, result.get("execution_id"),
            )
            return ROUTE_NONE_JUDGMENT_RAN
        LOGGER.warning(
            "[autonomy-wiring] utterance-conflict judgment left an "
            "unresolved execution (%s); not responding to avoid double "
            "handling (persona=%s execution=%s)",
            result.get("reason"), persona_id, result.get("execution_id"),
        )
        return ROUTE_NONE_INDETERMINATE

    reaction = _extract_reaction(result)
    if reaction is None:
        reaction = _reaction_from_ledger(manager, result.get("execution_id"))
    if reaction == "engage_now":
        LOGGER.info(
            "[autonomy-wiring] utterance-conflict judged engage_now; "
            "starting the conversation (persona=%s)", persona_id,
        )
        engage()
        return ROUTE_JUDGED_ENGAGE_NOW
    if reaction is None:
        LOGGER.warning(
            "[autonomy-wiring] utterance-conflict judgment ran but reaction "
            "could not be read; NOT responding to avoid double handling "
            "(persona=%s)", persona_id,
        )
        return ROUTE_JUDGED_UNKNOWN
    LOGGER.info(
        "[autonomy-wiring] utterance-conflict judged %s (persona=%s); "
        "no immediate response", reaction, persona_id,
    )
    return f"judged:{reaction}"


#: 回収応対の結末。再試行してよいのは SAFE_FAILURE (副作用ゼロ確定) だけ —
#: UNKNOWN (LLM が動いたか不明) を再試行すると、発話・記憶更新まで進んだ応対を
#: もう一度走らせて二重応対になる (Codex 六巡目 high1。台帳の unknown =
#: 自動再実行禁止と同じ規律)。UNROUTABLE は応対先が決まらない状態で、待っても
#: 直らない (再試行しても同じ) — ERROR で残して打ち切る。
RECOVERED_DISPATCH_OK = "dispatched"
RECOVERED_DISPATCH_SAFE_FAILURE = "safe_failure"
RECOVERED_DISPATCH_UNKNOWN = "unknown"
RECOVERED_DISPATCH_UNROUTABLE = "unroutable"


def _conversation_already_answered(manager: Any, persona_id: str) -> bool:
    """いまの会話区間で、このペルソナの応答が既に出ているか。

    証跡は「開いている会話の ``started_at`` 以降の assistant メッセージ」で、
    これを回収の重複判定に使う。

    倒し方に注意 (fail 方向が用途で決まる):

    - **会話が開いていない** → 応答済みではない (False)。会話は生きていないので、
      ここで会話を開いても二重にならない。
    - **会話は開いているが証跡が読めない** → 応答済みに倒す (True)。ライブ経路が
      既に応対している可能性があり、重ねて起動すると二重応対になる。
    """
    from saiverse.user_conversation import get_open_conversation

    conv = get_open_conversation(manager, persona_id)
    if conv is None:
        return False

    since = conv.get("started_at")
    persona = _get_persona(manager, persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    checker = getattr(adapter, "has_assistant_message_since", None)
    if not isinstance(since, int) or not callable(checker):
        LOGGER.warning(
            "[autonomy-wiring] cannot read the assistant-message evidence for "
            "persona %s; assuming the conversation was answered", persona_id,
        )
        return True
    try:
        answered = checker(since)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] assistant-message lookup failed for persona %s; "
            "assuming the conversation was answered", persona_id, exc_info=True,
        )
        return True
    if answered is None:
        return True
    return bool(answered)


def _engage_recovered_user_conversation(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """回収経路で engage_now と判断された**ユーザー発話の仲裁**に応対する。

    応対は初回と同じ入口 —— :func:`saiverse.user_conversation.start_conversation`
    が会話の出来事を開き、main_line Pulse を起動し、沈黙タイマーを張る。初回発火
    (``handle_user_utterance_conflict`` の ``engage`` callback) と同じ手を通すので
    帳簿が一致する。

    外部イベント用の再構成 (:func:`_dispatch_recovered_event_response`) をここで
    使ってはいけない —— ユーザーの発話が ``<system>[外部イベント通知]`` として
    流し込まれ、応答は届くのに会話の出来事が開かない (2026-08-14 Codex 指摘 F4)。

    **既にこの会話区間で応答が出ている**なら何もしない —— ライブ経路が先に応対
    していれば、ここで起動すると main_line がもう一度走る。判定材料は「開いて
    いる会話の出来事の開始以降に assistant メッセージが実在するか」
    (:func:`_conversation_already_answered`)。⚠ **出来事が開いていることを
    応答済みの根拠にしてはいけない** —— 「出来事は開いているが応答は無い」状態は
    実在する (main_line が転んだ場合など。2026-08-14 Codex 指摘 — この誤認は
    ユーザーの発話を捨てる)。
    """
    user_id = context.get("conversation_user_id")

    if _conversation_already_answered(manager, persona_id):
        LOGGER.info(
            "[autonomy-wiring] the recovered utterance conflict was already "
            "answered in this conversation episode; not engaging again "
            "(persona=%s)", persona_id,
        )
        return RECOVERED_DISPATCH_OK

    try:
        from saiverse.user_conversation import start_conversation

        start_conversation(manager, persona_id, user_id)
    except Exception:
        LOGGER.warning(
            "[autonomy-wiring] recovered utterance-conflict engage failed "
            "(persona=%s); safe to retry", persona_id, exc_info=True,
        )
        return RECOVERED_DISPATCH_SAFE_FAILURE
    LOGGER.info(
        "[autonomy-wiring] recovered utterance-conflict started the user "
        "conversation (persona=%s user=%s)", persona_id, user_id,
    )
    return RECOVERED_DISPATCH_OK


def _dispatch_recovered_response(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """回収経路の応対 —— 台帳 payload に凍結された**種別**で入口を選ぶ。

    ユーザー発話の仲裁は会話の開始、それ以外の外部イベントは応対 Pulse の
    再構成。初回発火と同じ入口を通すのが原則で、種別を落として一律に外部
    イベント形へ流すと帳簿が食い違う (F4)。
    """
    if context.get("utterance_conflict"):
        return _engage_recovered_user_conversation(manager, persona_id, context)
    return _dispatch_recovered_event_response(manager, persona_id, context)


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
        outcome = _dispatch_recovered_response(manager, persona_id, context)
        if outcome == RECOVERED_DISPATCH_OK:
            LOGGER.info(
                "[autonomy-wiring] recovered on_event response dispatched on "
                "retry %d (persona=%s execution=%s)",
                attempt, persona_id, execution_id,
            )
            return
        if outcome in (RECOVERED_DISPATCH_UNKNOWN, RECOVERED_DISPATCH_UNROUTABLE):
            # unknown = LLM が動いたか不明 (再試行すると二重応対になりうる)。
            # unroutable = 応対先が決まらない (待っても直らない)。どちらも
            # ここで打ち切る (ERROR は応対関数が出している)
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


def _dispatch_recovered_response_with_retry(
    manager: Any,
    persona_id: str,
    context: Dict[str, Any],
    execution_id: Optional[str],
) -> str:
    """回収の応対を 1 回起動し、副作用ゼロ確定の失敗だけ bounded backoff へ回す。

    判断台帳は既に終端しているため prepared 回収では拾えない — 揮発予約の
    再試行がこの応対の唯一の救済路 (Codex 五巡目 high3)。unknown (実行中の例外)
    と unroutable (応対先が決まらない) は再試行しない。
    """
    outcome = _dispatch_recovered_response(manager, persona_id, context)
    if outcome == RECOVERED_DISPATCH_OK:
        LOGGER.info(
            "[autonomy-wiring] recovered on_event response dispatched from the "
            "frozen payload (persona=%s execution=%s)", persona_id, execution_id,
        )
    elif outcome == RECOVERED_DISPATCH_SAFE_FAILURE:
        _schedule_recovered_response_retry(
            manager, persona_id, context, execution_id, attempt=1,
        )
    return outcome


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

    応対の入口は payload に凍結された種別で選ぶ (:func:`_dispatch_recovered_response`)
    — ユーザー発話の仲裁は会話 Track の activate、外部イベントは応対 Pulse の
    再構成 (同 ④、2026-08-14)。

    旧 kind の ``judgment.day_open`` / ``judgment.day_close`` (起床・就寝が判断点
    だった頃の席) を拾った場合は**再発火せず**、席を放棄して閉じる — 起床・就寝は
    機械の帳簿処理になり (``day_plan.handle_scheduled_life_boundary``)、LLM の
    判断として撃ち直す先が無い。ライフの確定・節目は schedule と watchdog が
    帳簿処理の側で拾う。
    """
    if judgment_kind in (KIND_DAY_OPEN, KIND_DAY_CLOSE):
        ledger = getattr(manager, "execution_ledger", None)
        outcome = _release_claimed_seat(
            ledger, execution_id,
            "retired: life boundary is machine-only now (no LLM refire)",
        )
        LOGGER.info(
            "[autonomy-wiring] recovered legacy judgment.%s seat abandoned — the "
            "life boundary is machine-only now (persona=%s execution=%s "
            "outcome=%s)", judgment_kind, persona_id, execution_id, outcome,
        )
        return {"kind": judgment_kind,
                "playbook": JUDGMENT_PLAYBOOK_MAP.get(judgment_kind),
                "submitted": False,
                "reason": REASON_LIFE_BOUNDARY_MACHINE_ONLY,
                "outcome": outcome, "execution_id": execution_id}
    result = fire_judgment_point(
        manager, persona_id, judgment_kind,
        context=context, resume_execution_id=execution_id,
    )
    if judgment_kind != KIND_ON_EVENT:
        return result
    if not result.get("submitted"):
        # 通常入口 (handle_external_event / handle_user_utterance_conflict) は
        # 「LLM へ渡る前に止まった / 副作用ゼロ確定で失敗した」を代替応対へ落とす。
        # 回収入口だけ無条件に return すると、同じ失敗でイベントが消える —
        # しかも runtime exception で台帳が終端した行は prepared 回収にも戻らない
        # ので、そこで永久に失われる (2026-08-14 Codex 指摘)。判定は通常入口と
        # 同じ direct_fallback_allowed を使う (結末の無い結果は拒否側)。
        if direct_fallback_allowed(result):
            LOGGER.info(
                "[autonomy-wiring] recovered %s did not start (%s); running the "
                "typed fallback once (persona=%s execution=%s)",
                judgment_kind, result.get("reason"), persona_id, execution_id,
            )
            _dispatch_recovered_response_with_retry(
                manager, persona_id, context or {}, execution_id,
            )
        return result
    reaction = _extract_reaction(result)
    if reaction is None:
        reaction = _reaction_from_ledger(manager, result.get("execution_id"))
    if reaction != "engage_now":
        return result
    _dispatch_recovered_response_with_retry(
        manager, persona_id, context or {}, execution_id,
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

    - 自律 ON・起床時間帯 (起床スケジュールの時刻〜就寝スケジュールの時刻)・
      **今日のライフがまだ確定していない** → 起床の帳簿処理
      (``day_plan.handle_scheduled_life_boundary`` の start — ライフ確定と開始の
      節目、LLM なし) を発火し直す (起床時刻にサーバーが落ちていた / 途中で
      自律 ON になった等)。確定済みなら撃たない。就寝スケジュールが無い設定は
      ライフを定義できないので撃たない
    - ライフはあるが pending / deferred コマの EventScheduler 予約が消えている
      (再起動等でインメモリ予約が失われた) → コマ予約を再 push する

    day_open / day_close の PersonaSchedule が無いペルソナ (v2 の一日リズム
    未設定) では何もしない。発火時は必ず INFO ログを残す。

    見張る対象の営業日は :func:`day_plan.resolve_business_day` が決める (現在
    時刻を含む確定ライフ優先。ライフを読めなければ ``skip`` して次の tick へ
    委ねる)。「いま何かすべき時間帯か」を決める窓・曜日のゲートは、走っている
    確定ライフが無いときだけ現行 PersonaSchedule で判定する — 走っているライフ
    があるなら、その区間そのものがそのペルソナの起きている時間帯。

    Returns:
        ``{"action": "none"|"skip"|"life_start_refire"|"reschedule", ...}``
        (観察・テスト用)。
    """
    if not is_autonomy_on(manager, persona_id):
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
    refire_blocked_reason: Optional[str] = None
    # 「今日のライフがまだ確定していない」の判定は解決器が読んだライフ
    # (basis.lives — persona_life、行の無い日付だけ旧 meta_json.lives の互換読み)
    # をそのまま使う。営業日を決めた読みと同じ世代を見るため。就寝スケジュールが
    # 無い設定はライフをそもそも定義できない (confirm_life_for_today がライフ
    # 無しの日として決着する) ので、撃ち直しの対象にしない — 毎 tick の空撃ちと、
    # その日のコマ予約の見張りの取りこぼしを防ぐ。
    if not basis.lives and close:
        # 再発火の制約: 見ている営業日が**暦日と同じとき**だけ撃つ。違うとき =
        # 前の営業日がまだ終わっていない (深夜跨ぎの尻尾、または日付を跨いで
        # 続いている確定ライフ)。起床の帳簿処理が確定するのは**暦日**のライフ
        # (_day_open_plan_date) なので、ここで撃つと起きなかった日の深夜に
        # 翌日のライフを始めてしまう。撃たない場合も、その営業日のコマ予約の
        # 途絶は下で見張る。
        if today == now.date().isoformat():
            LOGGER.info(
                "[watchdog] no life confirmed for today; re-firing the life start "
                "(machine bookkeeping, no LLM) (persona=%s date=%s wake=%s)",
                persona_id, today, wake,
            )
            settled = day_plan.handle_scheduled_life_boundary(
                manager, persona_id, day_plan.LIFE_BOUNDARY_START,
                sched.get("day_open_params"),
            )
            return {"action": "life_start_refire", "settled": settled}
        LOGGER.debug(
            "[watchdog] previous business day (%s) still in effect and has no "
            "life; skipping life-start refire (persona=%s basis=%s)",
            today, persona_id, basis.source,
        )
        refire_blocked_reason = (
            "previous business day still in effect: no life-start refire"
        )

    # v0.5 (life.md §11.2): 専用のライフ境界イベント予約は廃止した — ライフの
    # 開始/終了処理は起床・就寝スケジュールの発火 (day_plan.
    # handle_scheduled_life_boundary) が持つため、ここで見張るのはコマ予約の
    # 途絶だけでよい。
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

    if refire_blocked_reason is not None:
        return {"action": "none", "reason": refire_blocked_reason}
    return {"action": "none"}
