"""時間割 (day plan) の保存とコマ発火配線 (自律行動 v2 §4.2)。

「駆動の実体は、朝、自分で組む時間割」— 起床判断 (day_open) の成果物として
編成された一日のコマ配列を保存し、各コマの開始時刻を EventScheduler へ push
する。以後の駆動は決定論であり、**コマ開始は判断点ではない = LLM を呼ばない**
(docs/intent/persona_cognition/judgment_points.md §2、設計原理 6)。

コマ発火 (``_fire_slot``) の処理:
1. ユーザー会話中なら繰り下げ (10 分後に再 push、上限 3 回で skipped)
2. facility が現在地と違えば OccupancyManager で移動 (失敗は WARN + 続行)
3. kind 別ハンドラ実行 → status 更新

「ユーザー会話中」の判定は running Track が user_conversation 種別であること
(``TrackManager.get_running``)。対ユーザー Track は wait_response 型で、会話が
続く間 running を保ち、無応答タイムアウト (既定 30 分、AI.USER_CONV_TIMEOUT_MINUTES)
で pending に落ちる — その pending 遷移が v2 の「会話終了」に相当するため、
running user_conversation = 会話中 が最も設計に整合する述語である。

kind 別ハンドラはレジストリ方式 (``register_slot_handler``)。本モジュールが
組み込みで登録するのは:
- 「作る」「知る」: 決定論テンプレートで指示書を組み ``run_work_session`` を運転
- 「暮らし」「休む」: ログのみのスタブ (暮らし Pulse / 判断点は後続フェーズ)
未登録 kind のコマは WARN + skipped。

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

REF_NONE = "none"
FACILITY_OWN_ROOM = "own_room"

#: ユーザー会話中の繰り下げ幅 (分) と上限回数 (v2 §4.2「割り込み」)
DEFER_MINUTES = 10
MAX_DEFERRALS = 3

#: budget_rounds が 0 / 未指定の作業コマに使う既定ラウンド予算
DEFAULT_BUDGET_ROUNDS = 8

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REF_RE = re.compile(r"^(task|desire):(\d+)$")

# ---------------------------------------------------------------------------
# kind 別ハンドラのレジストリ
# ---------------------------------------------------------------------------

#: ハンドラ signature: fn(manager, persona_id, plan_date, slot, index) -> None
SlotHandler = Callable[[Any, str, str, Dict[str, Any], int], None]

_SLOT_HANDLERS: Dict[str, SlotHandler] = {}


def register_slot_handler(kind: str, fn: SlotHandler) -> None:
    """kind に対するコマ発火ハンドラを登録する (同 kind は上書き)。

    後続フェーズ (話す/聞く/経験する/自分を更新する の社交・経験系、
    暮らし Pulse) はここへ登録することで配線に乗る。
    """
    if kind in _SLOT_HANDLERS:
        LOGGER.info("[day_plan] slot handler overridden: kind=%s", kind)
    _SLOT_HANDLERS[kind] = fn


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


def _validate_and_normalize_slots(slots: Any) -> List[Dict[str, Any]]:
    """コマ配列を検証し、正規化したコピーを返す。不正は ValueError。

    検証項目 (judgment_points.md §3.2 の finalize 検証のうち保存時に決まるもの):
    - start は "HH:MM" で厳密に昇順 (同時刻も不可 — 同 key 空間で衝突するため)
    - kind は六型 + 暮らし/休む のみ
    - ref は "task:N" / "desire:N" / "none"。暮らし/休む は "none" 必須
    - facility は非空文字列 (building_id or "own_room")
    - budget_rounds は非負 int (bool は不可)
    - status は既知の値のみ (省略時 pending)
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

        normalized.append({
            "start": start,
            "kind": kind,
            "ref": ref,
            "facility": facility.strip(),
            "budget_rounds": budget,
            "note": note,
            "status": status,
            "defer_count": defer_count,
        })
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


def _move_to_facility(manager: Any, persona_id: str, slot: Dict[str, Any]) -> None:
    """facility が現在地と違えば OccupancyManager で移動する。失敗は WARN + 続行。"""
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
        return
    if not ok:
        LOGGER.warning(
            "[day_plan] facility move failed (persona=%s %s -> %s): %s — continuing in place",
            persona_id, current, target, msg,
        )
    else:
        LOGGER.info(
            "[day_plan] moved for slot: persona=%s %s -> %s", persona_id, current, target
        )


def _fire_slot(manager: Any, persona_id: str, plan_date_str: str, index: int) -> None:
    """コマ発火。判断点ではないため LLM を呼ばない (judgment_points.md §2)。

    1. ユーザー会話中 → 繰り下げ (10 分後に同 key 再 push、上限 3 回で skipped)
    2. facility が現在地と違えば移動 (失敗は WARN + 続行)
    3. kind 別ハンドラ実行 → status 更新 (fired → done / 未登録 kind は skipped)
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
            _update_slot(manager, persona_id, plan_date_str, index, status=STATUS_SKIPPED)
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
        _update_slot(manager, persona_id, plan_date_str, index, status=STATUS_SKIPPED)
        LOGGER.warning(
            "[day_plan] no handler registered for kind=%r; slot skipped "
            "(persona=%s date=%s index=%d)",
            kind, persona_id, plan_date_str, index,
        )
        return

    # (b) 施設へ移動 (型 → 行き先の実行。移動自体が接地した行動 — v2 §6.1)
    _move_to_facility(manager, persona_id, slot)

    # (c) kind 別ハンドラ実行。fired を先に永続化することで、ハンドラ実行中の
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
        handler(manager, persona_id, plan_date_str, slot, index)
    except Exception:
        # 失敗コマは fired のまま残す (再発火しない安全側)。就寝判断の
        # 予定 vs 実績で「実行したが完了記録が無い」として観察できる。
        LOGGER.exception(
            "[day_plan] slot handler failed (persona=%s date=%s index=%d kind=%s); "
            "slot left as 'fired'",
            persona_id, plan_date_str, index, kind,
        )
        return
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

# 決定論テンプレート (v2 §9.2-8 の先行 2 種)。「実際に起きたこと以外を
# 書かせない」文言を含める (接地原則 §3-1)。
_WORKER_INSTRUCTION_TEMPLATES = {
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
}

_NO_REF_TARGET = "(参照タスクなし。目的の記述に従うこと)"


def _handle_worker_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「作る」「知る」コマ: 決定論テンプレートで指示書を組み run_work_session を運転する。"""
    kind = slot["kind"]
    template = _WORKER_INSTRUCTION_TEMPLATES[kind]
    ref = slot.get("ref") or REF_NONE
    target = _resolve_ref(manager, persona_id, ref) or _NO_REF_TARGET
    note = (slot.get("note") or "").strip() or "(記載なし)"
    instruction = template.format(note=note, target=target)

    budget = int(slot.get("budget_rounds") or 0)
    if budget < 1:
        LOGGER.info(
            "[day_plan] slot budget_rounds=%d < 1; using default %d "
            "(persona=%s date=%s index=%d)",
            budget, DEFAULT_BUDGET_ROUNDS, persona_id, plan_date_str, index,
        )
        budget = DEFAULT_BUDGET_ROUNDS

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


register_slot_handler(KIND_CREATE, _handle_worker_slot)
register_slot_handler(KIND_LEARN, _handle_worker_slot)
register_slot_handler(KIND_LIVING, _handle_living_slot)
register_slot_handler(KIND_REST, _handle_rest_slot)
