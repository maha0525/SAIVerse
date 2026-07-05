"""時間割 (day plan) の保存とコマ発火配線 (自律行動 v2 §4.2)。

「駆動の実体は、朝、自分で組む時間割」— 起床判断 (day_open) の成果物として
編成された一日のコマ配列を保存し、各コマの開始時刻を EventScheduler へ push
する。以後の駆動は決定論であり、**コマ開始は判断点ではない = LLM を呼ばない**
(docs/intent/persona_cognition/judgment_points.md §2、設計原理 6)。

コマ発火 (``_fire_slot``) の処理:
1. ユーザー会話中なら繰り下げ (10 分後に再 push、上限 3 回で skipped)
2. 予算ゲート (v2 §4.5): セッション系 (consumes_budget) コマは日次予算台帳の
   残高でラウンドを切り詰める (残高 0 なら skipped + WARN、ハンドラ実行しない)
3. facility が現在地と違えば OccupancyManager で移動 (失敗は WARN + 続行)
4. kind 別ハンドラ実行 → 実 rounds_used を台帳へ積算 → status 更新

日次予算台帳 (v2 §4.5) は persona_day_plan.meta_json の
``{"budget_total_rounds": N, "budget_used_rounds": M}``。total は起床判断
(day_open) の finalize が編成時に書き (``init_budget_ledger``)、used は発火後の
``consume_budget`` が実測値で積算する。台帳の無い日 (day_open 前 / 旧データ)
はゲート無効 = 従来挙動 (後方互換)。

「ユーザー会話中」の判定は running Track が user_conversation 種別であること
(``TrackManager.get_running``)。対ユーザー Track は wait_response 型で、会話が
続く間 running を保ち、無応答タイムアウト (既定 30 分、AI.USER_CONV_TIMEOUT_MINUTES)
で pending に落ちる — その pending 遷移が v2 の「会話終了」に相当するため、
running user_conversation = 会話中 が最も設計に整合する述語である。

kind 別ハンドラはレジストリ方式 (``register_slot_handler``)。本モジュールが
組み込みで登録するのは:
- 六型 (話す/聞く/作る/知る/経験する/自分を更新する): 型別の決定論テンプレート
  (v2 §9.2-8) で指示書を組み ``run_work_session`` を運転 (予算ゲート対象)。
  社交機構 (対ペルソナ会話) が未実装の「話す」「聞く」は、伝えたいことの文章化・
  読む/調べる、という現時点で実際にできる接地行動に指示書を限定する
- 「暮らし」「休む」: ログのみのスタブ (暮らし Pulse / 判断点は後続フェーズ)
未登録 kind のコマは WARN + skipped (``skip_reason='no_handler'``)。

コマの skipped はシステム都合とペルソナ判断を区別して記録する (``skip_reason``)。
システム都合のスキップ (ハンドラ未登録 / 予算切れ / 会話優先の流れ) を
「見送り」= 本人の判断としてペルソナに提示すると、就寝判断がしてもいない判断の
理由を捏造する (接地原則違反、2026-07-05 実 LLM シムで実証)。実績ラベルは
:func:`slot_result_label` が skip_reason 込みで返す。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
EventScheduler の同一 key 再 push は既存の再スケジュール挙動 (古い予約の
cancel + 上書き) に従うため、``schedule_day_plan`` / ``reschedule_pending_slots``
の二重呼び出しで二重発火しない (冪等)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, List, Optional

from saiverse import clock

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# 六型 (autonomous_behavior_v2.md §5.1)
KIND_TALK = "話す"
KIND_LISTEN = "聞く"
KIND_CREATE = "作る"
KIND_LEARN = "知る"
KIND_EXPERIENCE = "経験する"
KIND_SELF_UPDATE = "自分を更新する"

SIX_KINDS = (
    KIND_TALK, KIND_LISTEN, KIND_CREATE,
    KIND_LEARN, KIND_EXPERIENCE, KIND_SELF_UPDATE,
)

# 六型以外のコマ (judgment_points.md §3.2)
KIND_LIVING = "暮らし"
KIND_REST = "休む"

ALL_KINDS = SIX_KINDS + (KIND_LIVING, KIND_REST)

# コマ status
STATUS_PENDING = "pending"
STATUS_FIRED = "fired"
STATUS_DEFERRED = "deferred"
STATUS_SKIPPED = "skipped"
STATUS_DONE = "done"

SLOT_STATUSES = (
    STATUS_PENDING, STATUS_FIRED, STATUS_DEFERRED, STATUS_SKIPPED, STATUS_DONE,
)

# skipped の理由 (slot の skip_reason フィールド)。システム都合のスキップを
# 「見送り」= 本人の判断としてペルソナ/ユーザーに提示しないための区別。
SKIP_REASON_NO_HANDLER = "no_handler"          # kind の実行手段が未登録 (システム側)
SKIP_REASON_BUDGET_EXHAUSTED = "budget_exhausted"  # 日次予算の残高ゼロ
SKIP_REASON_DEFERRAL_LIMIT = "deferral_limit"  # 会話優先の繰り下げ上限で流れた

#: コマ status → 実績ラベル (skipped 以外)。skipped は skip_reason で細分化する
#: ため :func:`slot_result_label` を使うこと。
SLOT_STATUS_LABELS = {
    STATUS_PENDING: "未実施",
    STATUS_FIRED: "実行した（完了記録なし）",
    STATUS_DEFERRED: "繰り下げのまま",
    STATUS_DONE: "実行済み",
}

#: skip_reason → 実績ラベル。ペルソナが自分の判断でないものを自分の判断として
#: 振り返らされないよう、システム都合であることを明示する文言にする。
SKIP_REASON_LABELS = {
    SKIP_REASON_NO_HANDLER: "実行できず（システム側の問題: このコマ種別の実行手段が未実装）",
    SKIP_REASON_BUDGET_EXHAUSTED: "実行できず（作業ラウンドの日次予算切れ）",
    SKIP_REASON_DEFERRAL_LIMIT: "流れた（ユーザーとの会話を優先したため）",
}


def slot_result_label(slot: Dict[str, Any]) -> str:
    """コマの実績ラベル (就寝判断の状況テキストと一日新聞が共用する語彙)。

    skipped はシステム都合 (skip_reason) を明示して返す。「見送り」のような
    本人判断を示唆する語は、実際に本人が判断した記録があるときにしか使わない
    (現状、本人判断でコマを skipped にする機構は無い — 残り時間割の全置換で
    コマ自体が消える)。理由の無い旧データは中立の「実行されず」に倒す。
    """
    status = str(slot.get("status") or STATUS_PENDING)
    if status == STATUS_SKIPPED:
        reason = str(slot.get("skip_reason") or "")
        return SKIP_REASON_LABELS.get(reason, "実行されず（理由の記録なし）")
    return SLOT_STATUS_LABELS.get(status, status)

REF_NONE = "none"
FACILITY_OWN_ROOM = "own_room"

#: ユーザー会話中の繰り下げ幅 (分) と上限回数 (v2 §4.2「割り込み」)
DEFER_MINUTES = 10
MAX_DEFERRALS = 3

#: budget_rounds が 0 / 未指定の作業コマに使う既定ラウンド予算
DEFAULT_BUDGET_ROUNDS = 8

#: 日次予算台帳 (persona_day_plan.meta_json) のキー (v2 §4.5)
META_BUDGET_TOTAL = "budget_total_rounds"
META_BUDGET_USED = "budget_used_rounds"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REF_RE = re.compile(r"^(task|desire):(\d+)$")

# ---------------------------------------------------------------------------
# kind 別ハンドラのレジストリ
# ---------------------------------------------------------------------------

#: ハンドラ signature: fn(manager, persona_id, plan_date, slot, index) -> Optional[int]
#: 戻り値は「実際に消費したラウンド数」(None / 0 = 予算消費なし)。
#: consumes_budget=True で登録された kind は、戻り値が予算台帳へ積算される。
SlotHandler = Callable[[Any, str, str, Dict[str, Any], int], Optional[int]]

_SLOT_HANDLERS: Dict[str, SlotHandler] = {}

#: 予算ゲート (v2 §4.5) の対象 kind (register_slot_handler の consumes_budget)
_BUDGET_GATED_KINDS: set = set()


def register_slot_handler(kind: str, fn: SlotHandler, *, consumes_budget: bool = False) -> None:
    """kind に対するコマ発火ハンドラを登録する (同 kind は上書き)。

    組み込みでは六型すべてが作業セッション運転、暮らし/休む がスタブとして
    登録される (モジュール末尾)。後続フェーズ (対ペルソナ社交・暮らし Pulse) は
    ここへ上書き登録することで配線に乗る。

    Args:
        consumes_budget: True なら予算ゲートの対象 (v2 §4.5)。発火前に日次
            残高で budget_rounds が切り詰められ (残高 0 なら skipped)、
            ハンドラの戻り値 (実 rounds_used) が台帳へ積算される。
    """
    if kind in _SLOT_HANDLERS:
        LOGGER.info("[day_plan] slot handler overridden: kind=%s", kind)
    _SLOT_HANDLERS[kind] = fn
    if consumes_budget:
        _BUDGET_GATED_KINDS.add(kind)
    else:
        _BUDGET_GATED_KINDS.discard(kind)


# ---------------------------------------------------------------------------
# バリデーションと保存
# ---------------------------------------------------------------------------


def _normalize_plan_date(plan_date: Any) -> str:
    """plan_date を "YYYY-MM-DD" 文字列へ正規化する。"""
    if isinstance(plan_date, datetime):
        return plan_date.date().isoformat()
    if isinstance(plan_date, date):
        return plan_date.isoformat()
    if isinstance(plan_date, str):
        try:
            return date.fromisoformat(plan_date.strip()).isoformat()
        except ValueError as exc:
            raise ValueError(f"invalid plan_date: {plan_date!r} (expected YYYY-MM-DD)") from exc
    raise ValueError(f"invalid plan_date type: {type(plan_date).__name__}")


def _validate_and_normalize_slots(
    slots: Any, *, ascending_from: int = 0
) -> List[Dict[str, Any]]:
    """コマ配列を検証し、正規化したコピーを返す。不正は ValueError。

    検証項目 (judgment_points.md §3.2 の finalize 検証のうち保存時に決まるもの):
    - start は "HH:MM" で厳密に昇順 (同時刻も不可)
    - kind は六型 + 暮らし/休む のみ
    - ref は "task:N" / "desire:N" / "none"。暮らし/休む は "none" 必須
    - facility は非空文字列 (building_id or "own_room")
    - budget_rounds は非負 int (bool は不可)
    - title は文字列 (「○○をする」という短い表題。旧データは無いので省略可 = "")
    - status は既知の値のみ (省略時 pending)

    Args:
        ascending_from: 厳密昇順を検証する開始 index。残りコマの全置換
            (:func:`replace_remaining_slots`) では消化済みコマ (帳簿) を先頭に
            残すため、昇順検証は **新コマ区間のみ** に適用する — 消化済み区間との
            境界を跨いだ比較はしない (直前に消化したコマと同時刻・過去時刻の
            新コマは正当な組み替えであり、EventScheduler は過去時刻を即時扱い
            する。予約 key は index ベースなので同時刻でも衝突しない)。
            index < ascending_from のコマもフィールド検証は全て受ける。
            2026-07-05 実 LLM シム 3回目: 消化済み 13:30 コマの直後に 13:30 の
            新コマを置く組み替えが「昇順でない」で全却下された不具合の修正。
    """
    if not isinstance(slots, list) or not slots:
        raise ValueError("slots must be a non-empty list")

    normalized: List[Dict[str, Any]] = []
    prev_minutes = -1
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise ValueError(f"slot[{i}] must be a dict (got {type(slot).__name__})")

        start = slot.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            raise ValueError(f"slot[{i}].start must be 'HH:MM' (got {start!r})")
        if i >= ascending_from:
            minutes = int(start[:2]) * 60 + int(start[3:])
            if minutes <= prev_minutes:
                raise ValueError(
                    f"slot[{i}].start={start!r} is not strictly ascending "
                    "(slots must be sorted by start time)"
                )
            prev_minutes = minutes

        kind = slot.get("kind")
        if kind not in ALL_KINDS:
            raise ValueError(f"slot[{i}].kind={kind!r} is not a valid kind {ALL_KINDS}")

        ref = slot.get("ref", REF_NONE)
        if not isinstance(ref, str) or (ref != REF_NONE and not _REF_RE.match(ref)):
            raise ValueError(
                f"slot[{i}].ref={ref!r} must be 'task:N', 'desire:N' or 'none'"
            )
        if kind in (KIND_LIVING, KIND_REST) and ref != REF_NONE:
            raise ValueError(
                f"slot[{i}]: kind={kind!r} must have ref='none' (got {ref!r})"
            )

        facility = slot.get("facility")
        if not isinstance(facility, str) or not facility.strip():
            raise ValueError(
                f"slot[{i}].facility must be a building_id or 'own_room' (got {facility!r})"
            )

        budget = slot.get("budget_rounds", 0)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(
                f"slot[{i}].budget_rounds must be a non-negative int (got {budget!r})"
            )

        title = slot.get("title", "")
        if not isinstance(title, str):
            raise ValueError(f"slot[{i}].title must be a string (got {type(title).__name__})")

        note = slot.get("note", "")
        if not isinstance(note, str):
            raise ValueError(f"slot[{i}].note must be a string (got {type(note).__name__})")

        status = slot.get("status", STATUS_PENDING)
        if status not in SLOT_STATUSES:
            raise ValueError(f"slot[{i}].status={status!r} is not one of {SLOT_STATUSES}")

        defer_count = slot.get("defer_count", 0)
        if isinstance(defer_count, bool) or not isinstance(defer_count, int) or defer_count < 0:
            raise ValueError(
                f"slot[{i}].defer_count must be a non-negative int (got {defer_count!r})"
            )

        skip_reason = slot.get("skip_reason", "")
        if not isinstance(skip_reason, str):
            raise ValueError(
                f"slot[{i}].skip_reason must be a string (got {type(skip_reason).__name__})"
            )

        normalized_slot = {
            "start": start,
            "kind": kind,
            "ref": ref,
            "facility": facility.strip(),
            "budget_rounds": budget,
            "title": title.strip(),
            "note": note,
            "status": status,
            "defer_count": defer_count,
        }
        # skipped の理由は帳簿の一部 — 消化済みコマを残す全置換
        # (replace_remaining_slots) の再検証を通っても保持する。
        if skip_reason:
            normalized_slot["skip_reason"] = skip_reason
        normalized.append(normalized_slot)
    return normalized


def save_day_plan(manager: Any, persona_id: str, plan_date: Any, slots: List[Dict[str, Any]]) -> None:
    """時間割を検証して upsert する (1 ペルソナ 1 日 1 行)。

    Raises:
        ValueError: persona_id 空 / plan_date 不正 / コマ配列の検証失敗。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    plan_date_str = _normalize_plan_date(plan_date)
    normalized = _validate_and_normalize_slots(slots)
    _upsert_plan_slots(manager, persona_id, plan_date_str, normalized)


def _upsert_plan_slots(
    manager: Any, persona_id: str, plan_date_str: str, normalized: List[Dict[str, Any]]
) -> None:
    """検証済みコマ配列を upsert する (save_day_plan / replace_remaining_slots 共用)。"""
    from database.models import PersonaDayPlan

    now = clock.now()
    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        payload = json.dumps(normalized, ensure_ascii=False)
        if row is None:
            db.add(PersonaDayPlan(
                persona_id=persona_id,
                plan_date=plan_date_str,
                slots_json=payload,
                created_at=now,
                updated_at=now,
            ))
        else:
            row.slots_json = payload
            row.updated_at = now
        db.commit()
    finally:
        db.close()
    LOGGER.info(
        "[day_plan] saved: persona=%s date=%s slots=%d",
        persona_id, plan_date_str, len(normalized),
    )


def load_day_plan(manager: Any, persona_id: str, plan_date: Any) -> Optional[List[Dict[str, Any]]]:
    """保存済み時間割のコマ配列を返す。無ければ None。"""
    plan_date_str = _normalize_plan_date(plan_date)
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None:
            return None
        return json.loads(row.slots_json)
    finally:
        db.close()


def _write_slots(manager: Any, persona_id: str, plan_date_str: str, slots: List[Dict[str, Any]]) -> None:
    """コマ配列を書き戻す (行が無い場合は WARN のみ — 発火中の行削除は異常系)。"""
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None:
            LOGGER.warning(
                "[day_plan] cannot write slots: plan row missing (persona=%s date=%s)",
                persona_id, plan_date_str,
            )
            return
        row.slots_json = json.dumps(slots, ensure_ascii=False)
        row.updated_at = clock.now()
        db.commit()
    finally:
        db.close()


def _update_slot(
    manager: Any, persona_id: str, plan_date_str: str, index: int, **changes: Any
) -> Optional[Dict[str, Any]]:
    """slot[index] に changes を適用して永続化し、更新後の slot を返す。"""
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None or index >= len(slots):
        LOGGER.warning(
            "[day_plan] cannot update slot: not found (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        return None
    slots[index].update(changes)
    _write_slots(manager, persona_id, plan_date_str, slots)
    return slots[index]


# ---------------------------------------------------------------------------
# 日付付帯情報 (meta_json): tomorrow_memo (明日の自分へのメモ) 等の置き場
# ---------------------------------------------------------------------------


def load_plan_meta(manager: Any, persona_id: str, plan_date: Any) -> Dict[str, Any]:
    """plan 行の付帯情報 (meta_json) を dict で返す。行なし / 不正 JSON は空 dict。

    就寝判断 (day_close) が書いた ``tomorrow_memo`` 等を、翌朝の起床判断
    (day_open) が読む入口 (judgment_points.md §4/§8)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    from database.models import PersonaDayPlan

    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None or not row.meta_json:
            return {}
        try:
            meta = json.loads(row.meta_json)
        except (TypeError, ValueError):
            LOGGER.warning(
                "[day_plan] meta_json is not valid JSON (persona=%s date=%s); returning {}",
                persona_id, plan_date_str,
            )
            return {}
        return meta if isinstance(meta, dict) else {}
    finally:
        db.close()


def update_plan_meta(
    manager: Any, persona_id: str, plan_date: Any, updates: Dict[str, Any]
) -> Dict[str, Any]:
    """plan 行の付帯情報 (meta_json) へ updates をマージして永続化する。

    行が無ければ meta のみの行 (slots_json="[]") を作る — 就寝判断が「時間割の
    無かった日」にも明日の自分へのメモを残せるようにするため。slots_json="[]" は
    ``load_day_plan`` では空配列、``schedule_day_plan`` では push 0 件として
    無害に振る舞う (save_day_plan で本物の時間割が上書きされたら meta は残る)。

    Returns:
        マージ後の meta dict。
    """
    if not isinstance(updates, dict):
        raise ValueError(f"updates must be a dict (got {type(updates).__name__})")
    plan_date_str = _normalize_plan_date(plan_date)
    from database.models import PersonaDayPlan

    now = clock.now()
    db = manager.SessionLocal()
    try:
        row = (
            db.query(PersonaDayPlan)
            .filter_by(persona_id=persona_id, plan_date=plan_date_str)
            .first()
        )
        if row is None:
            merged = dict(updates)
            db.add(PersonaDayPlan(
                persona_id=persona_id,
                plan_date=plan_date_str,
                slots_json="[]",
                meta_json=json.dumps(merged, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            ))
        else:
            try:
                merged = json.loads(row.meta_json) if row.meta_json else {}
            except (TypeError, ValueError):
                merged = {}
            if not isinstance(merged, dict):
                merged = {}
            merged.update(updates)
            row.meta_json = json.dumps(merged, ensure_ascii=False)
            row.updated_at = now
        db.commit()
        return merged
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 日次予算台帳 (v2 §4.5): meta_json の budget_total_rounds / budget_used_rounds
# ---------------------------------------------------------------------------


def _read_nonneg_int(value: Any) -> Optional[int]:
    """非負 int なら int を、そうでなければ None を返す (bool は不可)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def init_budget_ledger(
    manager: Any, persona_id: str, plan_date: Any, total_rounds: int
) -> Dict[str, int]:
    """日次予算台帳を初期化する (起床判断 day_open の finalize が編成時に呼ぶ)。

    total を書き、used は既存値を保持する (起床判断のやり直しで消費済み分は
    リセットされない — 使った予算は使ったまま)。

    Returns:
        :func:`get_budget_state` と同形の ``{"total", "used", "remaining"}``。
    """
    total = _read_nonneg_int(total_rounds)
    if total is None:
        raise ValueError(f"total_rounds must be a non-negative int (got {total_rounds!r})")
    meta = load_plan_meta(manager, persona_id, plan_date)
    used = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
    update_plan_meta(manager, persona_id, plan_date, {
        META_BUDGET_TOTAL: total,
        META_BUDGET_USED: used,
    })
    LOGGER.info(
        "[day_plan] budget ledger initialized: persona=%s date=%s total=%d used=%d",
        persona_id, _normalize_plan_date(plan_date), total, used,
    )
    return {"total": total, "used": used, "remaining": max(0, total - used)}


def get_budget_state(
    manager: Any, persona_id: str, plan_date: Any
) -> Optional[Dict[str, int]]:
    """日次予算の残高 ``{"total", "used", "remaining"}`` を返す。

    台帳が無い日 (day_open がまだ走っていない / 本機能より前のデータ) は
    None — 予算ゲートは無効 = 従来挙動 (後方互換)。
    """
    meta = load_plan_meta(manager, persona_id, plan_date)
    total = _read_nonneg_int(meta.get(META_BUDGET_TOTAL))
    if total is None:
        return None
    used = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
    return {"total": total, "used": used, "remaining": max(0, total - used)}


def consume_budget(
    manager: Any, persona_id: str, plan_date: Any, rounds: int
) -> Optional[Dict[str, int]]:
    """実際に消費したラウンド数を台帳へ積算する (発火後の実測値)。

    台帳 (total) がまだ無い日でも used だけは記録する — 後から day_open が
    :func:`init_budget_ledger` で total を書いたとき、既消費分が保持される。

    Returns:
        積算後の :func:`get_budget_state` (total 未設定の日は None)。
    """
    inc = _read_nonneg_int(rounds)
    if inc is None:
        LOGGER.warning(
            "[day_plan] consume_budget: rounds=%r is not a non-negative int; ignored",
            rounds,
        )
        return get_budget_state(manager, persona_id, plan_date)
    if inc == 0:
        return get_budget_state(manager, persona_id, plan_date)
    meta = load_plan_meta(manager, persona_id, plan_date)
    used = (_read_nonneg_int(meta.get(META_BUDGET_USED)) or 0) + inc
    update_plan_meta(manager, persona_id, plan_date, {META_BUDGET_USED: used})
    state = get_budget_state(manager, persona_id, plan_date)
    LOGGER.info(
        "[day_plan] budget consumed: persona=%s date=%s +%d rounds (used=%d remaining=%s)",
        persona_id, _normalize_plan_date(plan_date), inc, used,
        state["remaining"] if state else "?",
    )
    return state


# ---------------------------------------------------------------------------
# EventScheduler への push
# ---------------------------------------------------------------------------


def _slot_key(persona_id: str, plan_date_str: str, index: int) -> str:
    return f"day_plan:{persona_id}:{plan_date_str}:{index}"


def _slot_fire_at(plan_date_str: str, slot: Dict[str, Any]) -> datetime:
    """コマの開始時刻 (naive datetime)。過去時刻は EventScheduler が即時扱いする。"""
    d = date.fromisoformat(plan_date_str)
    hh, mm = slot["start"].split(":")
    return datetime.combine(d, dt_time(int(hh), int(mm)))


def _push_slot(
    manager: Any, persona_id: str, plan_date_str: str, index: int, fire_at: datetime
) -> None:
    manager.event_scheduler.schedule(
        fire_at=fire_at,
        callback=lambda: _fire_slot(manager, persona_id, plan_date_str, index),
        key=_slot_key(persona_id, plan_date_str, index),
    )


def schedule_day_plan(manager: Any, persona_id: str, plan_date: Any) -> int:
    """pending コマを EventScheduler に push し、push した数を返す。

    key は ``day_plan:{persona_id}:{plan_date}:{index}``。同 key の再 push は
    EventScheduler の既存挙動 (古い予約 cancel + 上書き) に従うため冪等。
    過去時刻のコマは即時扱い (EventScheduler.schedule の仕様)。
    保存済みの時間割が無ければ WARN + 0 を返す (watchdog 経路で安全)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        LOGGER.warning(
            "[day_plan] schedule_day_plan: no plan saved (persona=%s date=%s)",
            persona_id, plan_date_str,
        )
        return 0

    pushed = 0
    for index, slot in enumerate(slots):
        if slot.get("status") != STATUS_PENDING:
            continue
        _push_slot(manager, persona_id, plan_date_str, index, _slot_fire_at(plan_date_str, slot))
        pushed += 1
    LOGGER.info(
        "[day_plan] scheduled: persona=%s date=%s pushed=%d/%d",
        persona_id, plan_date_str, pushed, len(slots),
    )
    return pushed


def reschedule_pending_slots(manager: Any, persona_id: str) -> int:
    """当日 plan の pending / deferred コマを再 push する (watchdog / 再起動後の再接続)。

    deferred コマも開始時刻 (過去なら即時扱い) で再 push する。繰り下げ待ちの
    残り時間は再起動を跨いで保持しない — 即時に発火し、まだ会話中なら
    ``_fire_slot`` が改めて繰り下げる (defer_count は永続化済みなので上限 3 回の
    帳簿は保たれる)。

    同 key 上書きなので二重呼び出ししても二重発火しない (冪等)。
    plan が無ければ 0。
    """
    plan_date_str = clock.now().date().isoformat()
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        return 0

    pushed = 0
    for index, slot in enumerate(slots):
        if slot.get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            continue
        _push_slot(manager, persona_id, plan_date_str, index, _slot_fire_at(plan_date_str, slot))
        pushed += 1
    if pushed:
        LOGGER.info(
            "[day_plan] rescheduled pending slots: persona=%s date=%s pushed=%d",
            persona_id, plan_date_str, pushed,
        )
    return pushed


def cancel_scheduled_slots(manager: Any, persona_id: str, plan_date: Any) -> int:
    """保存済み plan の全コマ分の EventScheduler 予約を cancel し、数を返す。

    時間割の全置換 (起床判断のやり直し / remaining_timetable の置換) の前処理。
    key は index ベースなので、コマ数が減る置換では旧 index の予約が残留し、
    新 plan の別コマ (または範囲外 index) を誤発火させる。置換前に旧 plan の
    全 key を cancel することでこれを防ぐ (発火済み key の cancel は no-op)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return 0
    slots = load_day_plan(manager, persona_id, plan_date_str) or []
    cancelled = 0
    for index in range(len(slots)):
        if scheduler.cancel(_slot_key(persona_id, plan_date_str, index)):
            cancelled += 1
    if cancelled:
        LOGGER.info(
            "[day_plan] cancelled scheduled slots: persona=%s date=%s cancelled=%d",
            persona_id, plan_date_str, cancelled,
        )
    return cancelled


def replace_remaining_slots(
    manager: Any, persona_id: str, plan_date: Any, new_slots: List[Dict[str, Any]]
) -> int:
    """残りコマ (pending / deferred) を new_slots で全置換する (judgment_points.md §3.3)。

    消化済みコマ (fired / done / skipped) は帳簿として残し、残りコマだけを
    new_slots に差し替える。検証は保存時と同一 (``_validate_and_normalize_slots``)
    で、失敗時は ValueError を投げて **plan も予約も一切変更しない**。

    厳密昇順の検証は **new_slots の区間のみ** に適用する
    (``ascending_from=len(kept)``)。消化済みコマは書き換え不可の歴史であり、
    「直前に消化したコマと同時刻・過去時刻から始まる組み替え」(直近コマの
    やり直し等) は正当な意志なので、消化済み区間との境界比較で全却下しない
    (2026-07-05 実 LLM シム 3回目の不具合)。過去時刻の新コマは
    EventScheduler が即時扱いする。

    Returns:
        置換後に EventScheduler へ push した pending コマ数。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    current = load_day_plan(manager, persona_id, plan_date_str) or []
    kept = [
        s for s in current
        if s.get("status") not in (STATUS_PENDING, STATUS_DEFERRED)
    ]
    candidate = kept + [
        {**slot, "status": STATUS_PENDING, "defer_count": 0} for slot in new_slots
    ]
    # 失敗時はここで raise (昇順検証は新コマ区間のみ — docstring 参照)
    normalized = _validate_and_normalize_slots(candidate, ascending_from=len(kept))

    # 検証が通ってから旧予約を落とし、保存 → 再 push する。
    cancel_scheduled_slots(manager, persona_id, plan_date_str)
    _upsert_plan_slots(manager, persona_id, plan_date_str, normalized)
    return schedule_day_plan(manager, persona_id, plan_date_str)


# ---------------------------------------------------------------------------
# コマ発火 (LLM を呼ばない決定論処理)
# ---------------------------------------------------------------------------


def _is_in_user_conversation(manager: Any, persona_id: str) -> bool:
    """ユーザー会話中か。running Track が user_conversation 種別なら True。

    対ユーザー Track は wait_response 型で、会話継続中は running を保ち、
    無応答タイムアウトで pending に落ちる (saiverse_manager.py
    ``_wait_response_timeout_provider``)。running でなくなった瞬間が v2 の
    「会話終了」に相当するため、この述語が設計に最も整合する。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return False
    try:
        running = track_manager.get_running(persona_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] get_running failed (persona=%s); treating as not in conversation",
            persona_id, exc_info=True,
        )
        return False
    return running is not None and getattr(running, "track_type", None) == "user_conversation"


def _building_display_name(manager: Any, building_id: Any) -> str:
    """building_id を表示名へ解決する (building_map が無い / 未登録なら ID のまま)。"""
    building = (getattr(manager, "building_map", {}) or {}).get(building_id)
    name = getattr(building, "name", None)
    return str(name or building_id)


def _record_move_failure(
    manager: Any, persona: Any, slot: Dict[str, Any],
    current: Any, target: Any, reason: Any,
) -> None:
    """施設移動の失敗をペルソナに見える形で記録する (SAIMemory system 通知)。

    移動失敗時のフォールバックは「移動せず現在地で実行」だが、それが黙って
    起きるとペルソナは「予定の場所で作業した」つもりのまま現在地の文脈で
    振る舞う (接地原則違反の温床)。event_message タグ付きで通知を挿入し、
    次の head 構築時にペルソナの context へ乗るようにする。
    """
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None or not hasattr(adapter, "append_persona_message"):
        return
    target_name = _building_display_name(manager, target)
    current_name = _building_display_name(manager, current)
    title = str(slot.get("title") or slot.get("kind") or "").strip()
    reason_text = str(reason or "理由不明")
    message = {
        "role": "user",
        "content": (
            "<system>[システム通知] "
            f"時間割のコマ「{title}」で予定していた場所「{target_name}」へ"
            f"移動できませんでした（{reason_text}）。"
            f"このコマは現在地「{current_name}」で行います。</system>"
        ),
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }
    try:
        adapter.append_persona_message(message)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to record move failure notice (persona=%s)",
            getattr(persona, "persona_id", "?"), exc_info=True,
        )


def _move_to_facility(manager: Any, persona_id: str, slot: Dict[str, Any]) -> None:
    """facility が現在地と違えば OccupancyManager で移動する。

    移動の実体 (occupancy / DB / host メッセージ) は ``move_entity`` に集約
    されているが、``persona.current_building_id`` の更新は設計上 **呼び出し側の
    責務** (manager/runtime.py summon_persona / builtin_data/tools/move_persona.py
    と同じパターン)。ここで更新しないと、コマの作業セッション
    (sea/work_session.py は current_building_id から head / audience を組む) と
    成果物の配置先 (manager/items.py) が終日 stale な旧建物の文脈で走り、
    次の head 構築時に occupancy 記録の無い幻の「戻った」diff 通知まで発生する
    (2026-07-05 実 LLM シム 異常 #1)。

    移動失敗 (満員等) は「移動せず現在地で実行」に倒すが、黙って現在地に
    ならないよう、その事実を WARN + ペルソナへの system 通知で記録する。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    if persona is None:
        LOGGER.warning("[day_plan] persona %s not loaded; skipping facility move", persona_id)
        return

    target = slot.get("facility")
    if target == FACILITY_OWN_ROOM:
        target = getattr(persona, "private_room_id", None)
        if not target:
            LOGGER.warning(
                "[day_plan] persona %s has no private_room_id; skipping facility move",
                persona_id,
            )
            return
    current = getattr(persona, "current_building_id", None)
    if not target or target == current:
        return

    occupancy = getattr(manager, "occupancy_manager", None)
    if occupancy is None:
        LOGGER.warning("[day_plan] manager has no occupancy_manager; skipping facility move")
        return
    try:
        ok, msg = occupancy.move_entity(persona_id, "ai", current, target)
    except Exception:
        LOGGER.warning(
            "[day_plan] move_entity raised (persona=%s %s -> %s); continuing",
            persona_id, current, target, exc_info=True,
        )
        _record_move_failure(manager, persona, slot, current, target, "内部エラー")
        return
    if not ok:
        LOGGER.warning(
            "[day_plan] facility move failed (persona=%s %s -> %s): %s — continuing in place",
            persona_id, current, target, msg,
        )
        _record_move_failure(manager, persona, slot, current, target, msg)
        return

    # move_entity は persona.current_building_id を書き換えない (呼び出し側責務)。
    # on_building_entered (move_entity 内) は「属性がまだ旧 Building」を前提に
    # 走るため、更新は必ず move_entity 成功の後に行う。
    persona.current_building_id = target
    for hook_name, hook_args in (("_mark_entry", (target,)), ("_save_session_metadata", ())):
        hook = getattr(persona, hook_name, None)
        if callable(hook):
            try:
                hook(*hook_args)
            except Exception:
                LOGGER.warning(
                    "[day_plan] %s failed after facility move (persona=%s)",
                    hook_name, persona_id, exc_info=True,
                )
    LOGGER.info(
        "[day_plan] moved for slot: persona=%s %s -> %s", persona_id, current, target
    )


def _effective_budget_rounds(slot: Dict[str, Any]) -> int:
    """コマの実効ラウンド予算 (0 / 未指定は既定値 DEFAULT_BUDGET_ROUNDS)。"""
    budget = int(slot.get("budget_rounds") or 0)
    return budget if budget >= 1 else DEFAULT_BUDGET_ROUNDS


def _apply_budget_gate(
    manager: Any, persona_id: str, plan_date_str: str, index: int, slot: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """予算ゲート (v2 §4.5)。切り詰め後の slot を返す。残高 0 なら None (発火中止)。

    - 台帳が無い日 (day_open 前 / 旧データ) はゲート無効 = slot をそのまま返す
    - 残高 < 実効予算 → 残高まで切り詰め (slots_json に永続化) + WARN
    - 残高 0 → status='skipped' + WARN、ハンドラは実行しない
    """
    requested = _effective_budget_rounds(slot)
    state = get_budget_state(manager, persona_id, plan_date_str)
    if state is None:
        return slot
    remaining = state["remaining"]
    if remaining <= 0:
        _update_slot(
            manager, persona_id, plan_date_str, index,
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_BUDGET_EXHAUSTED,
        )
        LOGGER.warning(
            "[day_plan] slot skipped: daily budget exhausted "
            "(persona=%s date=%s index=%d kind=%s used=%d/%d)",
            persona_id, plan_date_str, index, slot.get("kind"),
            state["used"], state["total"],
        )
        return None
    if remaining < requested:
        LOGGER.warning(
            "[day_plan] slot budget clamped to remaining daily budget: %d -> %d "
            "(persona=%s date=%s index=%d kind=%s used=%d/%d)",
            requested, remaining, persona_id, plan_date_str, index,
            slot.get("kind"), state["used"], state["total"],
        )
        requested = remaining
    if requested != int(slot.get("budget_rounds") or 0):
        # 切り詰め (または既定値の具体化) を slots_json に永続化する — 就寝判断の
        # 予定 vs 実績が「実際に許可された予算」を見られるようにするため。
        updated = _update_slot(
            manager, persona_id, plan_date_str, index, budget_rounds=requested,
        )
        if updated is not None:
            return updated
        slot = {**slot, "budget_rounds": requested}
    return slot


def _fire_slot(manager: Any, persona_id: str, plan_date_str: str, index: int) -> None:
    """コマ発火。判断点ではないため LLM を呼ばない (judgment_points.md §2)。

    1. ユーザー会話中 → 繰り下げ (10 分後に同 key 再 push、上限 3 回で skipped)
    2. 予算ゲート (consumes_budget な kind のみ): 残高で切り詰め / 残高 0 は skipped
    3. facility が現在地と違えば移動 (失敗は WARN + 続行)
    4. kind 別ハンドラ実行 → 実 rounds_used を台帳へ積算 →
       status 更新 (fired → done / 未登録 kind は skipped)
    """
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None or index >= len(slots):
        LOGGER.warning(
            "[day_plan] fire: slot not found (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        return
    slot = slots[index]
    status = slot.get("status")
    if status not in (STATUS_PENDING, STATUS_DEFERRED):
        LOGGER.info(
            "[day_plan] fire: slot already %s; ignoring (persona=%s date=%s index=%d)",
            status, persona_id, plan_date_str, index,
        )
        return

    # (a) ユーザー会話中なら繰り下げ (v2 §4.2「割り込み」/ 会話の至上性)
    if _is_in_user_conversation(manager, persona_id):
        defer_count = int(slot.get("defer_count", 0))
        if defer_count >= MAX_DEFERRALS:
            _update_slot(
                manager, persona_id, plan_date_str, index,
                status=STATUS_SKIPPED, skip_reason=SKIP_REASON_DEFERRAL_LIMIT,
            )
            LOGGER.info(
                "[day_plan] slot skipped after %d deferrals (persona=%s date=%s index=%d kind=%s)",
                defer_count, persona_id, plan_date_str, index, slot.get("kind"),
            )
            return
        _update_slot(
            manager, persona_id, plan_date_str, index,
            status=STATUS_DEFERRED, defer_count=defer_count + 1,
        )
        retry_at = clock.now() + timedelta(minutes=DEFER_MINUTES)
        _push_slot(manager, persona_id, plan_date_str, index, retry_at)
        LOGGER.info(
            "[day_plan] slot deferred (user conversation in progress): persona=%s date=%s "
            "index=%d kind=%s defer=%d/%d retry_at=%s",
            persona_id, plan_date_str, index, slot.get("kind"),
            defer_count + 1, MAX_DEFERRALS, retry_at.isoformat(timespec="seconds"),
        )
        return

    kind = slot.get("kind")
    handler = _SLOT_HANDLERS.get(kind)
    if handler is None:
        _update_slot(
            manager, persona_id, plan_date_str, index,
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_NO_HANDLER,
        )
        LOGGER.warning(
            "[day_plan] no handler registered for kind=%r; slot skipped "
            "(persona=%s date=%s index=%d)",
            kind, persona_id, plan_date_str, index,
        )
        return

    # (b) 予算ゲート (v2 §4.5): セッション系コマのみ。残高 0 なら skipped で終了
    gated = kind in _BUDGET_GATED_KINDS
    if gated:
        gated_slot = _apply_budget_gate(manager, persona_id, plan_date_str, index, slot)
        if gated_slot is None:
            return
        slot = gated_slot

    # (c) 施設へ移動 (型 → 行き先の実行。移動自体が接地した行動 — v2 §6.1)
    _move_to_facility(manager, persona_id, slot)

    # (d) kind 別ハンドラ実行。fired を先に永続化することで、ハンドラ実行中の
    # クラッシュ後に watchdog (reschedule_pending_slots) が同じコマを二重発火
    # させない (pending/deferred のみ再 push されるため)。
    updated = _update_slot(manager, persona_id, plan_date_str, index, status=STATUS_FIRED)
    if updated is not None:
        slot = updated
    LOGGER.info(
        "[day_plan] slot fired: persona=%s date=%s index=%d kind=%s ref=%s facility=%s",
        persona_id, plan_date_str, index, kind, slot.get("ref"), slot.get("facility"),
    )

    # desire 参照コマの発火 = 欲求への再訪。帳簿 (touch_count / 鮮度) に記録する
    # (v2 §5.3「何度も選ばれ再訪される欲求は関心に深まる」)。ハンドラの成否に
    # 依らず「取り組みに向かった」事実を記録するため、実行前に付ける。
    ref = slot.get("ref") or REF_NONE
    if ref.startswith("desire:"):
        try:
            from saiverse.desire_engine import touch_desire
            touch_desire(manager, persona_id, ref)
        except Exception:
            LOGGER.warning(
                "[day_plan] touch_desire failed (persona=%s ref=%s); continuing",
                persona_id, ref, exc_info=True,
            )

    try:
        used_rounds = handler(manager, persona_id, plan_date_str, slot, index)
    except Exception:
        # 失敗コマは fired のまま残す (再発火しない安全側)。就寝判断の
        # 予定 vs 実績で「実行したが完了記録が無い」として観察できる。
        # NOTE: raise 経路では消費ラウンドが不明のため台帳へ積算できない
        # (run_work_session は raise しない契約なので、通常この経路は通らない)。
        LOGGER.exception(
            "[day_plan] slot handler failed (persona=%s date=%s index=%d kind=%s); "
            "slot left as 'fired'",
            persona_id, plan_date_str, index, kind,
        )
        return

    # (e) 実測の消費ラウンドを日次予算台帳へ積算する (v2 §4.5)
    if gated and isinstance(used_rounds, int) and not isinstance(used_rounds, bool) \
            and used_rounds > 0:
        try:
            consume_budget(manager, persona_id, plan_date_str, used_rounds)
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_budget failed (persona=%s date=%s index=%d); "
                "continuing",
                persona_id, plan_date_str, index,
            )
    _update_slot(manager, persona_id, plan_date_str, index, status=STATUS_DONE)


# ---------------------------------------------------------------------------
# ref 解決 (task:N / desire:N → タイトル等)
# ---------------------------------------------------------------------------


def _resolve_ref(manager: Any, persona_id: str, ref: str) -> Optional[str]:
    """ref を人間可読なタイトル/目標へ解決する。none / 解決不能は None。

    desire は persona_task の parent_kind='note' 行 (desire ノート紐付き) であり、
    task:N と同じ short_id 参照空間を共有する (persona_task_manager.py)。
    したがって "desire:N" も同じ短縮参照 N で解決する。
    """
    if not ref or ref == REF_NONE:
        return None
    m = _REF_RE.match(ref)
    if m is None:
        LOGGER.warning("[day_plan] unresolvable ref format: %r", ref)
        return None

    from saiverse.persona_task_manager import (
        PARENT_NOTE,
        PersonaTaskManager,
        TaskNotFoundError,
    )

    task_manager = PersonaTaskManager(manager.SessionLocal)
    try:
        task_id = task_manager.resolve_task_ref(persona_id, f"task:{m.group(2)}")
        task = task_manager.get_task(task_id, persona_id=persona_id)
    except TaskNotFoundError:
        LOGGER.warning(
            "[day_plan] ref %r not found for persona=%s; instruction will use note only",
            ref, persona_id,
        )
        return None

    if m.group(1) == "desire" and task.get("parent_kind") != PARENT_NOTE:
        LOGGER.warning(
            "[day_plan] ref %r resolved to a non-desire task (parent_kind=%r); using it anyway",
            ref, task.get("parent_kind"),
        )

    title = task.get("title") or "(無題)"
    goal = (task.get("goal") or "").strip()
    return f"{title}（目標: {goal}）" if goal else title


# ---------------------------------------------------------------------------
# 組み込みハンドラ
# ---------------------------------------------------------------------------

# 型別の決定論テンプレート (v2 §9.2-8 の 6 種)。「実際に起きたこと以外を
# 書かせない」文言を含める (接地原則 §3-1)。社交機構 (対ペルソナ会話) が
# 未実装の「話す」「聞く」は、現時点で実際にできる接地行動 (文章化 / 読む) に
# 指示書を限定し、していない対話を「した」と書かせない。
_WORKER_INSTRUCTION_TEMPLATES = {
    KIND_TALK: (
        "目的: {note}。対象: {target}。"
        "相手とその場で直接話す手段はまだありません。伝えたいことを実際に文章に"
        "整えること (必要なら document_create 等のスペルで実際に書き残すこと)。"
        "完成条件: 伝えたい内容が読み返せる形で残っていること。"
        "実際に話していない相手に「話した」「伝えた」と書かないこと。"
    ),
    KIND_LISTEN: (
        "目的: {note}。対象: {target}。"
        "memory_recall や searxng_search、その場に置かれた文書の読み込み等の"
        "スペルで実際に読む・調べること。"
        "完成条件: 実際に読んで得られた内容だけを短い覚え書きにまとめてあること。"
        "読めていない・聞けていない内容を「聞いた」と書かないこと。"
    ),
    KIND_CREATE: (
        "目的: {note}。対象: {target}。"
        "成果物を document_create で実際に作成すること。"
        "完成条件: 成果物が実在し、読み直して整えてあること。"
        "実際に作成・確認できたこと以外を「やった」と書かないこと。"
    ),
    KIND_LEARN: (
        "目的: {note}。対象: {target}。"
        "memory_recall や searxng_search 等のスペルで実際に調べること。"
        "完成条件: 実際に調べて得られた内容だけを短い覚え書きにまとめてあること。"
        "調べていないこと・確認できていないことを書かないこと。"
    ),
    KIND_EXPERIENCE: (
        "目的: {note}。対象: {target}。"
        "その場でスペルにより実際に確認・実行できたことだけを行うこと。"
        "完成条件: 実際に見聞き・実行できたことだけを短い覚え書きに残してあること。"
        "実際に起きていない体験を「した」と書かないこと。"
    ),
    KIND_SELF_UPDATE: (
        "目的: {note}。対象: {target}。"
        "memory_recall 等のスペルで実際に記憶を確かめ、得られた気づきを整理すること。"
        "整理した内容は document_create 等のスペルで実際に書き残すこと。"
        "完成条件: 実際に確かめた記憶に基づく覚え書きが読み返せる形で残っていること。"
        "実際に確かめ・書き残したこと以外を「更新した」と書かないこと。"
    ),
}

#: 作業セッション運転で処理する kind (= 六型すべて。予算ゲート対象)。
#: ScenarioPlayer のセッション終了判断ラップもこの集合を使う。
WORKER_SESSION_KINDS = SIX_KINDS
assert set(WORKER_SESSION_KINDS) == set(_WORKER_INSTRUCTION_TEMPLATES), (
    "worker session kinds and instruction templates must stay in sync"
)

_NO_REF_TARGET = "(参照タスクなし。目的の記述に従うこと)"


def run_worker_slot_session(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Any:
    """六型の作業コマの作業セッション 1 本を運転し、結果をそのまま返す。

    型別の決定論テンプレートで指示書を組み ``run_work_session`` を呼ぶ実体。組み込み
    ハンドラ (:func:`_handle_worker_slot`) と、セッション終了判断へ接続する
    上位層 (``saiverse.day_scenario.ScenarioPlayer`` のラップハンドラ) が共有する
    — 後者は post_session 判断の入力として ``WorkSessionResult`` 全体が要る。

    Returns:
        ``sea.work_session.WorkSessionResult`` (raise しない契約)。
    """
    kind = slot["kind"]
    template = _WORKER_INSTRUCTION_TEMPLATES[kind]
    ref = slot.get("ref") or REF_NONE
    target = _resolve_ref(manager, persona_id, ref) or _NO_REF_TARGET
    note = (slot.get("note") or "").strip() or "(記載なし)"
    instruction = template.format(note=note, target=target)

    budget = int(slot.get("budget_rounds") or 0)
    if budget < 1:
        # 予算ゲート (_apply_budget_gate) が実効値を永続化済みのはずだが、
        # 台帳の無い日 / 直接呼び出しのフォールバックとして既定値を保つ。
        budget = _effective_budget_rounds(slot)
        LOGGER.info(
            "[day_plan] slot budget_rounds < 1; using default %d "
            "(persona=%s date=%s index=%d)",
            budget, persona_id, plan_date_str, index,
        )

    from sea.work_session import run_work_session

    result = run_work_session(
        persona_id,
        instruction,
        budget,
        task_ref=ref if ref != REF_NONE else None,
        metadata={"day_plan": {"plan_date": plan_date_str, "slot_index": index, "kind": kind}},
        manager=manager,
    )
    LOGGER.info(
        "[day_plan] work session for slot finished: persona=%s date=%s index=%d kind=%s "
        "ended_reason=%s rounds=%d artifacts=%d",
        persona_id, plan_date_str, index, kind,
        result.ended_reason, result.rounds_used, len(result.artifacts),
    )
    return result


def worker_session_rounds_used(result: Any) -> int:
    """WorkSessionResult から予算台帳へ積算する実測ラウンド数を安全に読む。"""
    rounds_used = getattr(result, "rounds_used", 0) or 0
    if isinstance(rounds_used, bool) or not isinstance(rounds_used, int):
        return 0
    return int(rounds_used)


def _handle_worker_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Optional[int]:
    """六型の作業コマの組み込みハンドラ。

    Returns:
        実際に消費したラウンド数 (``_fire_slot`` が予算台帳へ積算する)。
    """
    result = run_worker_slot_session(manager, persona_id, plan_date_str, slot, index)
    return worker_session_rounds_used(result)


def _handle_living_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「暮らし」コマ: ログのみのスタブ。暮らし Pulse は後続フェーズで刺さる。"""
    LOGGER.info(
        "[day_plan] living slot (stub — 暮らし Pulse は後続フェーズ): "
        "persona=%s date=%s index=%d note=%r",
        persona_id, plan_date_str, index, slot.get("note"),
    )


def _handle_rest_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「休む」コマ: 何もしない。不作為の可視化として INFO で記録する (v2 §4.2)。"""
    LOGGER.info(
        "[day_plan] rest slot: persona=%s date=%s index=%d — 何もしない "
        "(コマとして明示的に選ばれた休息) note=%r",
        persona_id, plan_date_str, index, slot.get("note"),
    )


for _kind in WORKER_SESSION_KINDS:
    register_slot_handler(_kind, _handle_worker_slot, consumes_budget=True)
register_slot_handler(KIND_LIVING, _handle_living_slot)
register_slot_handler(KIND_REST, _handle_rest_slot)
