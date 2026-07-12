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

「ユーザー会話中」の判定は開いている kind='conversation' の出来事 (Episode)
の有無 (``saiverse.episodes.get_open_episode``)。無応答タイムアウト (既定 30 分、
AI.USER_CONV_TIMEOUT_MINUTES) が会話の出来事を閉じる瞬間が v2 の「会話終了」に
相当する (life.md §7 案 Y, 2026-07-13)。旧実装は running Track が
user_conversation 種別かで判定していたが、Track はもう時間経過で状態を
動かさない (running のまま残り続けうる) ため、この述語には使えなくなった —
「いま」の真実は Track ではなく開いている出来事が持つ。

kind 別ハンドラはレジストリ方式 (``register_slot_handler``)。本モジュールが
組み込みで登録するのは:
- 六型 (話す/聞く/作る/知る/経験する/自分を更新する): 型別の決定論テンプレート
  (v2 §9.2-8) で指示書を組み ``run_work_session`` を運転 (予算ゲート対象)。
  社交機構 (対ペルソナ会話) が未実装の「話す」「聞く」は、伝えたいことの文章化・
  読む/調べる、という現時点で実際にできる接地行動に指示書を限定する
- 「暮らし」「休む」: ログのみのスタブ (暮らし Pulse / 判断点は後続フェーズ)。
  施設への実移動 (presence) だけは本物なので status=done とするが、完了時に
  slot へ ``record_level='presence_only'`` を永続化し、表示側 (一日新聞 /
  就寝判断の状況テキスト) が「実行済み」でなく「時間を過ごした（詳細な記録
  なし）」と正直に提示する — していない活動の詳細をペルソナがふりかえりで
  捏造しないため (soft-confabulation、2026-07-05 実 LLM シム 異常 #4)
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

#: slot の record_level: 完了記録の詳しさ。presence_only は「その場に居た
#: (施設への実移動) 以外の詳細な実行記録が無い」— 暮らし/休む のスタブ
#: ハンドラが完了時に付ける。マーカーの無い done (旧データ / セッション系) は
#: 従来どおり「実行済み」(後方互換)。
RECORD_LEVEL_PRESENCE_ONLY = "presence_only"

#: record_level='presence_only' な done コマの実績ラベル。「実行済み」と
#: 提示すると、ペルソナが就寝ふりかえりで具体的な活動内容 (食事の選定等) を
#: 捏造する (soft-confabulation、2026-07-05 実 LLM シムで観測)。
LABEL_DONE_PRESENCE_ONLY = "時間を過ごした（詳細な記録なし）"


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
    if status == STATUS_DONE \
            and str(slot.get("record_level") or "") == RECORD_LEVEL_PRESENCE_ONLY:
        # 詳細な実行記録の無い done (暮らし/休む スタブ)。「実行済み」と提示
        # するとペルソナが活動内容を捏造してふりかえる (soft-confabulation)。
        return LABEL_DONE_PRESENCE_ONLY
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

# ---------------------------------------------------------------------------
# ライフ (life.md Phase 2「ライフの器」)
# ---------------------------------------------------------------------------

#: ライフのモード (life.md §5.1): 均等 = 標準パルスの間隔を TTL 内に保つ
#: (Anthropic/OpenAI 系のキャッシュ延命に最適)。自由 = 間隔制約なし (Gemini 等)。
LIFE_MODE_EVEN = "even"
LIFE_MODE_FREE = "free"
LIFE_MODES = (LIFE_MODE_EVEN, LIFE_MODE_FREE)

#: 均等モードで許容するコマ間隔の上限 (分)。TTL (Anthropic 1h) ちょうどは
#: 遅延で割れるため安全マージンを引いた初期値 (life.md §12-2)。
LIFE_EVEN_MAX_GAP_MINUTES = 50

#: ラウンド消費 → パルス予算換算の減衰係数 κ (life.md §8.1)。
#: 消費 = used_pulses + used_rounds × LIFE_ROUND_BUDGET_FACTOR。
LIFE_ROUND_BUDGET_FACTOR = 0.2

#: persona_day_plan.meta_json の lives 配列のキー (life.md §11.2)。
META_LIVES = "lives"

#: DEFAULT_MODEL の provider がこの集合に属せば既定モードは均等 (life.md §5.1)。
_EVEN_MODE_PROVIDERS = frozenset({"anthropic", "openai"})

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# コマは目的ノードを任意階層で指せる (P5, life_concept_map.md §3.1):
# task:N (採用済み) / desire:N (候補=お試し採用) / track:N (大枝=関心そのもの)
_REF_RE = re.compile(r"^(task|desire|track):(\d+)$")
_TRACK_REF_RE = re.compile(r"^track:(\d+)$")

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
                f"slot[{i}].ref={ref!r} must be 'task:N', 'desire:N', 'track:N' or 'none'"
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

        record_level = slot.get("record_level", "")
        if not isinstance(record_level, str):
            raise ValueError(
                f"slot[{i}].record_level must be a string (got {type(record_level).__name__})"
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
        # skipped の理由と完了記録の詳しさは帳簿の一部 — 消化済みコマを残す
        # 全置換 (replace_remaining_slots) の再検証を通っても保持する。
        if skip_reason:
            normalized_slot["skip_reason"] = skip_reason
        if record_level:
            normalized_slot["record_level"] = record_level
        normalized.append(normalized_slot)
    return normalized


def save_day_plan(manager: Any, persona_id: str, plan_date: Any, slots: List[Dict[str, Any]]) -> None:
    """時間割を検証して upsert する (1 ペルソナ 1 日 1 行)。

    Raises:
        ValueError: persona_id 空 / plan_date 不正 / コマ配列の検証失敗 /
            宣言済みライフの外にコマがある場合 (life.md §4.3「谷にコマは置けない」。
            ライフが宣言されていない日は skip = 後方互換)。
    """
    if not persona_id:
        raise ValueError("persona_id is required")
    plan_date_str = _normalize_plan_date(plan_date)
    normalized = _validate_and_normalize_slots(slots)
    _check_slots_within_lives(manager, persona_id, plan_date_str, normalized)
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
) -> Optional[Dict[str, Any]]:
    """日次予算の残高 ``{"total", "used", "remaining"}`` を返す。

    ライフ宣言がある日 (life.md Phase2 §7「ライフの世代交代」): 標準パルス予算
    (Σ lives.budget_pulses) を単位とし、消費はΣ (used_pulses + used_rounds ×
    :data:`LIFE_ROUND_BUDGET_FACTOR`)。**ライフの無い日は台帳が無ければ None**
    (:data:`META_BUDGET_TOTAL` 未設定) — 旧実装 (日次ラウンド台帳) にそのまま
    フォールバックする (後方互換。既存シグネチャ・呼び出し元は不変)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    lives = get_lives(manager, persona_id, plan_date_str)
    if lives:
        total = sum(int(life.get("budget_pulses") or 0) for life in lives)
        used = sum(_life_consumed(life) for life in lives)
        return {"total": total, "used": used, "remaining": max(0.0, total - used)}
    meta = load_plan_meta(manager, persona_id, plan_date_str)
    total_rounds = _read_nonneg_int(meta.get(META_BUDGET_TOTAL))
    if total_rounds is None:
        return None
    used_rounds = _read_nonneg_int(meta.get(META_BUDGET_USED)) or 0
    return {
        "total": total_rounds, "used": used_rounds,
        "remaining": max(0, total_rounds - used_rounds),
    }


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
# ライフ (life.md Phase 2 §11.2「新設」): 宣言の永続化・予算台帳・境界イベント
# ---------------------------------------------------------------------------


def _life_minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def get_lives(manager: Any, persona_id: str, plan_date: Any) -> List[Dict[str, Any]]:
    """保存済みライフ宣言 (meta_json.lives) を返す。無ければ空リスト。

    「lives が無い日 (旧データ・宣言なし) は検証もゲートも従来挙動」の判定は
    すべてこの関数の戻り値が空かどうかで行う (life.md §4.1)。
    """
    meta = load_plan_meta(manager, persona_id, plan_date)
    lives = meta.get(META_LIVES)
    if not isinstance(lives, list):
        return []
    return [life for life in lives if isinstance(life, dict)]


def get_life_for_time(lives: List[Dict[str, Any]], hhmm: str) -> Optional[int]:
    """hhmm ("HH:MM") が属するライフの index を返す (無ければ None)。"""
    for i, life in enumerate(lives):
        if life.get("start") <= hhmm < life.get("end"):
            return i
    return None


def _check_slots_within_lives(
    manager: Any, persona_id: str, plan_date_str: str, slots: List[Dict[str, Any]]
) -> None:
    """全コマの start が宣言済みライフ区間内にあることを検証する (raise ValueError)。

    ライフが宣言されていない日 (:func:`get_lives` が空) は検証しない
    (旧データ・宣言なしの日は「谷」の概念自体が無い — 後方互換最優先)。
    ``save_day_plan`` / ``replace_remaining_slots`` の両方が呼ぶことで、
    起床判断以外の経路 (会話終了判断の残り時間割編集等) からの保存も守る。
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    if not lives:
        return
    for idx, slot in enumerate(slots):
        start = slot.get("start")
        if get_life_for_time(lives, start) is None:
            raise ValueError(
                f"slot[{idx}] (start={start!r}) はどのライフ区間にも属していません "
                f"(谷にコマは置けません。宣言済みライフ: "
                f"{[(l.get('start'), l.get('end')) for l in lives]})"
            )


def _validate_and_normalize_lives(
    lives: Any, slots: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """ライフ配列を検証し、正規化したコピーを返す (bookkeeping フィールドは含まない)。

    検証項目 (life.md §4.1 / §5.1 / §4.3):
    - start/end は "HH:MM" で end が start より後 (深夜跨ぎのライフは未対応)
    - budget_pulses は正の int
    - mode は "even" / "free" のみ
    - ライフ同士は時間帯が重ならない (start 昇順に整列して隣接判定)
    - 全コマの start がいずれかのライフ区間内にあること (谷にコマは置けない)
    - mode="even" のライフはコマ間隔がすべて :data:`LIFE_EVEN_MAX_GAP_MINUTES`
      (既定 50 分) 以内であること (ライフ開始→最初のコマ→隣接コマ間→
      最後のコマ→ライフ終了)

    Raises:
        ValueError: 上記いずれかの違反。
    """
    if not isinstance(lives, list):
        raise ValueError(f"lives must be a list (got {type(lives).__name__})")
    if not lives:
        return []

    normalized: List[Dict[str, Any]] = []
    for i, life in enumerate(lives):
        if not isinstance(life, dict):
            raise ValueError(f"lives[{i}] must be a dict (got {type(life).__name__})")
        start = life.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            raise ValueError(f"lives[{i}].start must be 'HH:MM' (got {start!r})")
        end = life.get("end")
        if not isinstance(end, str) or not _TIME_RE.match(end):
            raise ValueError(f"lives[{i}].end must be 'HH:MM' (got {end!r})")
        if _life_minutes(end) <= _life_minutes(start):
            raise ValueError(
                f"lives[{i}]: end={end!r} must be after start={start!r} "
                "(深夜跨ぎのライフは未対応)"
            )
        budget = life.get("budget_pulses")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError(
                f"lives[{i}].budget_pulses must be a positive int (got {budget!r})"
            )
        mode = life.get("mode")
        if mode not in LIFE_MODES:
            raise ValueError(f"lives[{i}].mode must be one of {LIFE_MODES} (got {mode!r})")
        normalized.append({"start": start, "end": end, "budget_pulses": budget, "mode": mode})

    # start 昇順に整列し、隣接ライフの重なりを検証する (§4.1)。
    normalized.sort(key=lambda life: life["start"])
    for i in range(1, len(normalized)):
        if normalized[i]["start"] < normalized[i - 1]["end"]:
            raise ValueError(
                f"lives[{i}] ({normalized[i]['start']}-{normalized[i]['end']}) が "
                f"直前のライフ ({normalized[i - 1]['start']}-{normalized[i - 1]['end']}) "
                "と重なっています"
            )

    # 谷にコマは置けない (§4.3)。
    for idx, slot in enumerate(slots):
        start = slot.get("start")
        if not isinstance(start, str):
            continue
        if get_life_for_time(normalized, start) is None:
            raise ValueError(
                f"slot[{idx}] (start={start!r}) はどのライフ区間にも属していません "
                "(谷にコマは置けません)"
            )

    # 均等モードの間隔検証 (§5.1、LIFE_EVEN_MAX_GAP_MINUTES 既定 50 分)。
    for life in normalized:
        if life["mode"] != LIFE_MODE_EVEN:
            continue
        in_life_starts = sorted(
            slot["start"] for slot in slots
            if isinstance(slot.get("start"), str)
            and life["start"] <= slot["start"] < life["end"]
        )
        checkpoints = [life["start"], *in_life_starts, life["end"]]
        for a, b in zip(checkpoints, checkpoints[1:]):
            gap = _life_minutes(b) - _life_minutes(a)
            if gap > LIFE_EVEN_MAX_GAP_MINUTES:
                raise ValueError(
                    f"life ({life['start']}-{life['end']}, 均等モード): "
                    f"{a}→{b} の間隔が {gap} 分で上限 "
                    f"{LIFE_EVEN_MAX_GAP_MINUTES} 分を超えています"
                )

    return normalized


def save_lives(
    manager: Any, persona_id: str, plan_date: Any, lives: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """ライフ宣言を検証して保存する (起床判断 day_open の finalize が呼ぶ)。

    既存ライフと (start, end) が一致する行は積算済み消費 (used_pulses /
    used_rounds) と境界発火済みフラグ (start_fired / end_fired) を引き継ぐ
    (init_budget_ledger が used を保持するのと同じ思想 — 起床判断のやり直しで
    消費や通知の重複を作らない)。一致しない (新規・時刻変更の) ライフは
    0/False から始まる。

    Raises:
        ValueError: :func:`_validate_and_normalize_lives` の検証失敗。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    slots = load_day_plan(manager, persona_id, plan_date_str) or []
    normalized = _validate_and_normalize_lives(lives, slots)

    existing = {
        (life.get("start"), life.get("end")): life
        for life in get_lives(manager, persona_id, plan_date_str)
    }
    for life in normalized:
        prev = existing.get((life["start"], life["end"]))
        if prev is not None:
            life["used_pulses"] = int(prev.get("used_pulses") or 0)
            life["used_rounds"] = int(prev.get("used_rounds") or 0)
            life["start_fired"] = bool(prev.get("start_fired"))
            life["end_fired"] = bool(prev.get("end_fired"))
        else:
            life["used_pulses"] = 0
            life["used_rounds"] = 0
            life["start_fired"] = False
            life["end_fired"] = False

    update_plan_meta(manager, persona_id, plan_date_str, {META_LIVES: normalized})
    LOGGER.info(
        "[day_plan] lives saved: persona=%s date=%s lives=%d",
        persona_id, plan_date_str, len(normalized),
    )
    return normalized


def derive_default_life_mode(manager: Any, persona_id: str) -> str:
    """ペルソナの標準モデル (DEFAULT_MODEL) の provider からライフモードの既定を導出する。

    Anthropic/OpenAI 系は均等 (explicit/implicit cache の TTL 再送延命が効く)、
    それ以外 (Gemini/Ollama 等) は自由 (life.md §5.1)。モデル未解決・provider
    不明時は安全側 (自由 = 間隔制約なし) に倒す。
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    model = getattr(persona, "model", None)
    if not model:
        return LIFE_MODE_FREE
    try:
        from saiverse.model_configs import get_model_config
        provider = str(get_model_config(model).get("provider") or "")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve provider for model=%r (persona=%s); "
            "defaulting life mode to free", model, persona_id, exc_info=True,
        )
        return LIFE_MODE_FREE
    return LIFE_MODE_EVEN if provider in _EVEN_MODE_PROVIDERS else LIFE_MODE_FREE


def _resolve_current_plan_date(manager: Any, persona_id: str) -> str:
    """いま現在時刻が属する営業日 (plan_date) を解決する (深夜跨ぎ対応の自己解決)。"""
    try:
        from saiverse.autonomy_wiring import _find_day_schedules, effective_plan_date
        sched = _find_day_schedules(manager, persona_id)
        return effective_plan_date(
            clock.now(), sched.get("wake"), sched.get("close"),
        ).isoformat()
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve current plan_date (persona=%s); "
            "using calendar date", persona_id, exc_info=True,
        )
        return clock.now().date().isoformat()


def consume_life_pulse(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """標準パルス 1 回をその時刻が属するライフへ積算する (life.md Phase2 §7)。

    判断点発火 (autonomy_wiring.fire_judgment_point) と暮らしコマ・セッション系
    コマの発火 (:func:`_fire_slot`) の両方が呼ぶ。lives が無い日 / パルス時刻が
    どのライフにも属さない場合は no-op (None、後方互換)。

    Args:
        plan_date: 省略時は現在時刻が属する営業日を自己解決する。
        at_time: パルス時刻 "HH:MM"。省略時は ``clock.now()``。
    """
    plan_date_str = (
        _normalize_plan_date(plan_date) if plan_date is not None
        else _resolve_current_plan_date(manager, persona_id)
    )
    lives = get_lives(manager, persona_id, plan_date_str)
    if not lives:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    idx = get_life_for_time(lives, hhmm)
    if idx is None:
        LOGGER.info(
            "[day_plan] pulse at %s does not belong to any declared life "
            "(persona=%s date=%s); not counted",
            hhmm, persona_id, plan_date_str,
        )
        return None
    lives[idx]["used_pulses"] = int(lives[idx].get("used_pulses") or 0) + 1
    update_plan_meta(manager, persona_id, plan_date_str, {META_LIVES: lives})
    return lives[idx]


def consume_life_rounds(
    manager: Any,
    persona_id: str,
    plan_date: Any,
    rounds: int,
    *,
    at_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """作業ラウンドの実測値をその時刻が属するライフへ積算する (life.md Phase2 §7)。

    κ 減衰 (:data:`LIFE_ROUND_BUDGET_FACTOR`) は保存時でなく消費計算時
    (:func:`get_budget_state` / ゲート判定) に適用する — ここには生のラウンド数を
    積算する。lives が無い日は no-op (None)。
    """
    inc = _read_nonneg_int(rounds)
    if not inc:
        return None
    plan_date_str = _normalize_plan_date(plan_date)
    lives = get_lives(manager, persona_id, plan_date_str)
    if not lives:
        return None
    hhmm = at_time or clock.now().strftime("%H:%M")
    idx = get_life_for_time(lives, hhmm)
    if idx is None:
        LOGGER.info(
            "[day_plan] work rounds at %s do not belong to any declared life "
            "(persona=%s date=%s); not counted",
            hhmm, persona_id, plan_date_str,
        )
        return None
    lives[idx]["used_rounds"] = int(lives[idx].get("used_rounds") or 0) + inc
    update_plan_meta(manager, persona_id, plan_date_str, {META_LIVES: lives})
    return lives[idx]


def _life_consumed(life: Dict[str, Any]) -> float:
    """ライフ 1 件の消費量 (パルス換算): used_pulses + used_rounds × κ。"""
    used_pulses = int(life.get("used_pulses") or 0)
    used_rounds = int(life.get("used_rounds") or 0)
    return used_pulses + used_rounds * LIFE_ROUND_BUDGET_FACTOR


def _apply_life_budget_gate(
    manager: Any,
    persona_id: str,
    plan_date_str: str,
    index: int,
    slot: Dict[str, Any],
    lives: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """ライフ単位の予算ゲート (life.md Phase2 §7)。

    旧ゲート (:func:`_apply_budget_gate` の日次ラウンド台帳) と異なり、ラウンド数
    の切り詰めはしない二値判定 — そのコマが属するライフの残
    (budget_pulses − 消費) が 0 以下なら skip、それ以外はコマをそのまま通す
    (パルスとラウンドは単位が異なり、ラウンド予算をパルス残高で比例配分しても
    意味を持たないため)。

    Returns:
        通す場合は slot をそのまま返す。skip する場合は None
        (呼び出し側の :func:`_fire_slot` が status=skipped を永続化する)。
    """
    idx = get_life_for_time(lives, slot["start"])
    if idx is None:
        # 谷にコマは置けない検証 (save_lives/save_day_plan) を通っていれば
        # 起こらないはずだが、保存後にライフだけ組み替わった場合の防御。
        LOGGER.warning(
            "[day_plan] slot start=%s doesn't belong to any declared life "
            "(persona=%s date=%s index=%d); life budget gate skipped "
            "(allowing through)",
            slot["start"], persona_id, plan_date_str, index,
        )
        return slot
    life = lives[idx]
    remaining = int(life.get("budget_pulses") or 0) - _life_consumed(life)
    if remaining <= 0:
        _update_slot(
            manager, persona_id, plan_date_str, index,
            status=STATUS_SKIPPED, skip_reason=SKIP_REASON_BUDGET_EXHAUSTED,
        )
        LOGGER.warning(
            "[day_plan] slot skipped: life budget exhausted (persona=%s date=%s "
            "index=%d life=%s-%s used_pulses=%s used_rounds=%s budget_pulses=%s)",
            persona_id, plan_date_str, index, life.get("start"), life.get("end"),
            life.get("used_pulses"), life.get("used_rounds"), life.get("budget_pulses"),
        )
        return None
    return slot


def _life_key(persona_id: str, plan_date_str: str, index: int, boundary: str) -> str:
    return f"life:{persona_id}:{plan_date_str}:{index}:{boundary}"


def _life_fire_at(
    plan_date_str: str, life: Dict[str, Any], boundary: str, *, wake: Optional[str] = None
) -> datetime:
    """ライフ境界 (start/end) の発火時刻 (naive datetime)。深夜跨ぎ対応は
    コマ予約 (:func:`_slot_fire_at`) と同じ規則。"""
    d = date.fromisoformat(plan_date_str)
    hhmm = life[boundary]
    if wake and hhmm < wake:
        d = d + timedelta(days=1)
    hh, mm = hhmm.split(":")
    return datetime.combine(d, dt_time(int(hh), int(mm)))


def _notify_life_boundary(manager: Any, persona_id: str, text: str) -> None:
    """ライフ境界のシステム通知を tail (末尾イベント) として SAIMemory へ書く。

    Track 切替通知と同じ様式 (``<system>`` ラップの user メッセージ、
    event_message タグ、キャッシュ無破壊 — life.md §9.3)。
    """
    persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    if adapter is None or not hasattr(adapter, "append_persona_message"):
        return
    message = {
        "role": "user",
        "content": f"<system>[システム通知] {text}</system>",
        "metadata": {"tags": ["internal", "event_message", "day_plan"]},
    }
    try:
        adapter.append_persona_message(message)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to record life boundary notice (persona=%s)",
            persona_id, exc_info=True,
        )


def _handle_life_start(
    manager: Any, persona_id: str, plan_date_str: str, index: int, life: Dict[str, Any]
) -> None:
    LOGGER.info(
        "[day_plan] life started: persona=%s date=%s index=%d %s-%s "
        "(budget=%dパルス mode=%s)",
        persona_id, plan_date_str, index, life["start"], life["end"],
        life["budget_pulses"], life["mode"],
    )
    _notify_life_boundary(
        manager, persona_id,
        f"(ライフ開始) {life['start']}〜{life['end']} の活動を始めます。",
    )


def _handle_life_end(
    manager: Any, persona_id: str, plan_date_str: str, index: int, life: Dict[str, Any]
) -> None:
    consumed = _life_consumed(life)
    LOGGER.info(
        "[day_plan] life ended: persona=%s date=%s index=%d %s-%s "
        "(消費 %.1f/%d パルス)",
        persona_id, plan_date_str, index, life["start"], life["end"],
        consumed, life["budget_pulses"],
    )
    # TODO(life.md Phase3): Metabolism 連動 (ライフ終端での代謝台帳締め・畳み) は
    # ここに接続する。本フェーズでは台帳締めログ + 通知のみ。
    _notify_life_boundary(
        manager, persona_id,
        f"(ライフ終了) {life['start']}〜{life['end']} の活動を終えました。",
    )


def _fire_life_boundary(
    manager: Any, persona_id: str, plan_date_str: str, index: int, boundary: str
) -> None:
    lives = get_lives(manager, persona_id, plan_date_str)
    if index >= len(lives):
        LOGGER.warning(
            "[day_plan] life boundary fire: life not found (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index,
        )
        return
    life = lives[index]
    if life.get(f"{boundary}_fired"):
        LOGGER.info(
            "[day_plan] life boundary already fired; ignoring "
            "(persona=%s date=%s index=%d boundary=%s)",
            persona_id, plan_date_str, index, boundary,
        )
        return
    life[f"{boundary}_fired"] = True
    update_plan_meta(manager, persona_id, plan_date_str, {META_LIVES: lives})
    if boundary == "start":
        _handle_life_start(manager, persona_id, plan_date_str, index, life)
    else:
        _handle_life_end(manager, persona_id, plan_date_str, index, life)


def schedule_lives(
    manager: Any, persona_id: str, plan_date: Any, *, wake: Optional[str] = None
) -> int:
    """未発火のライフ境界イベント (start/end) を EventScheduler に push する。

    key は ``life:{persona_id}:{plan_date}:{index}:{start|end}``。同 key の
    再 push は EventScheduler の既存挙動 (cancel + 上書き) に従うため冪等
    (:func:`schedule_day_plan` と同じ規律)。既に発火済み (``{boundary}_fired``)
    の境界は push しない。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if wake is None:
        wake = _resolve_wake(manager, persona_id)
    lives = get_lives(manager, persona_id, plan_date_str)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None or not lives:
        return 0

    pushed = 0
    for index, life in enumerate(lives):
        for boundary in ("start", "end"):
            if life.get(f"{boundary}_fired"):
                continue
            fire_at = _life_fire_at(plan_date_str, life, boundary, wake=wake)
            scheduler.schedule(
                fire_at=fire_at,
                callback=(
                    lambda idx=index, b=boundary: _fire_life_boundary(
                        manager, persona_id, plan_date_str, idx, b,
                    )
                ),
                key=_life_key(persona_id, plan_date_str, index, boundary),
            )
            pushed += 1
    if pushed:
        LOGGER.info(
            "[day_plan] life events scheduled: persona=%s date=%s pushed=%d",
            persona_id, plan_date_str, pushed,
        )
    return pushed


def find_lost_life_reservations(
    manager: Any, persona_id: str, plan_date: Any
) -> List[Any]:
    """EventScheduler 予約が消えている (未発火の) ライフ境界の ``(index, boundary)`` を返す。

    watchdog (:func:`~saiverse.autonomy_wiring.watchdog_tick`) の見張り対象
    (:func:`find_lost_slot_reservations` と対称)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return []
    lives = get_lives(manager, persona_id, plan_date_str)
    lost: List[Any] = []
    for index, life in enumerate(lives):
        for boundary in ("start", "end"):
            if life.get(f"{boundary}_fired"):
                continue
            if not scheduler.has_key(_life_key(persona_id, plan_date_str, index, boundary)):
                lost.append((index, boundary))
    return lost


# ---------------------------------------------------------------------------
# EventScheduler への push
# ---------------------------------------------------------------------------


def _slot_key(persona_id: str, plan_date_str: str, index: int) -> str:
    return f"day_plan:{persona_id}:{plan_date_str}:{index}"


def _resolve_wake(manager: Any, persona_id: str) -> Optional[str]:
    """ペルソナの起床時刻 "HH:MM" を PersonaSchedule から解決する (無ければ None)。

    深夜跨ぎリズムの暦日補正 (_slot_fire_at) に使う。呼び出し元が wake を
    明示しなくても、コマ予約の全経路が跨ぎ対応になるようにするための自己解決。
    """
    try:
        from saiverse.autonomy_wiring import _find_day_schedules
        return _find_day_schedules(manager, persona_id).get("wake")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to resolve wake time (persona=%s)", persona_id,
            exc_info=True,
        )
        return None


def _slot_fire_at(
    plan_date_str: str, slot: Dict[str, Any], *, wake: Optional[str] = None
) -> datetime:
    """コマの開始時刻 (naive datetime)。過去時刻は EventScheduler が即時扱いする。

    深夜跨ぎリズム対応: ``wake`` が指定され、かつコマの start < wake のとき、
    そのコマは「同じ営業日の深夜帯 (翌暦日)」に属するため、
    ``plan_date + 1 日`` の日付で combine する。

    例: plan_date="2026-07-04", wake="07:00", slot.start="00:30" → 2026-07-05 00:30
        plan_date="2026-07-04", wake="07:00", slot.start="09:00" → 2026-07-04 09:00

    Args:
        plan_date_str: 営業日の "YYYY-MM-DD"
        slot:          コマ dict (``start`` フィールド必須)
        wake:          起床時刻 "HH:MM"。None なら従来どおり同日で combine
    """
    d = date.fromisoformat(plan_date_str)
    start = slot["start"]
    if wake and start < wake:
        # 深夜帯 (同営業日の翌暦日)
        d = d + timedelta(days=1)
    hh, mm = start.split(":")
    return datetime.combine(d, dt_time(int(hh), int(mm)))


def _push_slot(
    manager: Any, persona_id: str, plan_date_str: str, index: int, fire_at: datetime
) -> None:
    manager.event_scheduler.schedule(
        fire_at=fire_at,
        callback=lambda: _fire_slot(manager, persona_id, plan_date_str, index),
        key=_slot_key(persona_id, plan_date_str, index),
    )


def schedule_day_plan(
    manager: Any, persona_id: str, plan_date: Any, *, wake: Optional[str] = None
) -> int:
    """pending コマを EventScheduler に push し、push した数を返す。

    key は ``day_plan:{persona_id}:{plan_date}:{index}``。同 key の再 push は
    EventScheduler の既存挙動 (古い予約 cancel + 上書き) に従うため冪等。
    過去時刻のコマは即時扱い (EventScheduler.schedule の仕様)。
    保存済みの時間割が無ければ WARN + 0 を返す (watchdog 経路で安全)。

    Args:
        wake: 起床時刻 "HH:MM"。深夜跨ぎリズムのとき、start < wake のコマは
            ``plan_date + 1 日`` の時刻で push される。None (省略) は従来動作。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    if wake is None:
        # 呼び出し元 (起床判断 finalize 等) に配線を強要しない — 深夜跨ぎの
        # 暦日補正はコマ予約の全経路で常に効くべき (検収追加 2026-07-12)
        wake = _resolve_wake(manager, persona_id)
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
        _push_slot(
            manager, persona_id, plan_date_str, index,
            _slot_fire_at(plan_date_str, slot, wake=wake),
        )
        pushed += 1
    LOGGER.info(
        "[day_plan] scheduled: persona=%s date=%s pushed=%d/%d",
        persona_id, plan_date_str, pushed, len(slots),
    )
    return pushed


def reschedule_pending_slots(
    manager: Any,
    persona_id: str,
    plan_date: Any = None,
    *,
    wake: Optional[str] = None,
) -> int:
    """当日 (営業日) plan の pending / deferred コマを再 push する (watchdog / 再起動後の再接続)。

    deferred コマも開始時刻 (過去なら即時扱い) で再 push する。繰り下げ待ちの
    残り時間は再起動を跨いで保持しない — 即時に発火し、まだ会話中なら
    ``_fire_slot`` が改めて繰り下げる (defer_count は永続化済みなので上限 3 回の
    帳簿は保たれる)。

    同 key 上書きなので二重呼び出ししても二重発火しない (冪等)。
    plan が無ければ 0。

    Args:
        plan_date: 引く営業日 (date / datetime / "YYYY-MM-DD")。None のとき
            ``clock.now().date()`` を使う (後方互換)。深夜跨ぎリズムでは呼び
            出し元 (watchdog_tick) が :func:`~autonomy_wiring.effective_plan_date`
            で算出した date を渡すこと。
        wake: 起床時刻 "HH:MM"。_slot_fire_at に透過し、深夜帯コマの暦日補正
            に使う。
    """
    if wake is None:
        wake = _resolve_wake(manager, persona_id)
    if plan_date is None:
        # 深夜跨ぎリズムでは now.date() でなく営業日で引く (自己解決)
        from saiverse.autonomy_wiring import _find_day_schedules, effective_plan_date
        sched = _find_day_schedules(manager, persona_id)
        plan_date_str = effective_plan_date(
            clock.now(), sched.get("wake"), sched.get("close"),
        ).isoformat()
    else:
        plan_date_str = _normalize_plan_date(plan_date)
    slots = load_day_plan(manager, persona_id, plan_date_str)
    if slots is None:
        return 0

    pushed = 0
    for index, slot in enumerate(slots):
        if slot.get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            continue
        _push_slot(
            manager, persona_id, plan_date_str, index,
            _slot_fire_at(plan_date_str, slot, wake=wake),
        )
        pushed += 1
    if pushed:
        LOGGER.info(
            "[day_plan] rescheduled pending slots: persona=%s date=%s pushed=%d",
            persona_id, plan_date_str, pushed,
        )
    return pushed


def find_lost_slot_reservations(
    manager: Any, persona_id: str, plan_date: Any
) -> List[int]:
    """EventScheduler 予約が消えている pending / deferred コマの index を返す。

    watchdog (saiverse/autonomy_wiring.py) の「コマ予約の途絶」判定に使う。
    予約はインメモリなので再起動・EventScheduler 再生成で失われるが、
    slots_json の pending / deferred は残る — その差分が「途絶」。

    deferred コマは繰り下げ再 push (10 分後) の予約 key が同じなので、
    正常な繰り下げ待ち中は「予約あり」と判定される (途絶と誤認しない)。
    """
    plan_date_str = _normalize_plan_date(plan_date)
    scheduler = getattr(manager, "event_scheduler", None)
    if scheduler is None:
        return []
    slots = load_day_plan(manager, persona_id, plan_date_str) or []
    lost: List[int] = []
    for index, slot in enumerate(slots):
        if slot.get("status") not in (STATUS_PENDING, STATUS_DEFERRED):
            continue
        if not scheduler.has_key(_slot_key(persona_id, plan_date_str, index)):
            lost.append(index)
    return lost


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
    _check_slots_within_lives(manager, persona_id, plan_date_str, normalized)

    # 検証が通ってから旧予約を落とし、保存 → 再 push する。
    cancel_scheduled_slots(manager, persona_id, plan_date_str)
    _upsert_plan_slots(manager, persona_id, plan_date_str, normalized)
    return schedule_day_plan(manager, persona_id, plan_date_str)


# ---------------------------------------------------------------------------
# コマ発火 (LLM を呼ばない決定論処理)
# ---------------------------------------------------------------------------


def _is_in_user_conversation(manager: Any, persona_id: str) -> bool:
    """ユーザー会話中か。開いている kind='conversation' の出来事があれば True。

    life.md §7 案 Y (2026-07-13): 「いま」の真実は開いているエピソードが持つ。
    無応答タイムアウトが会話の出来事を閉じた瞬間が v2 の「会話終了」に相当する
    (``autonomy_wiring.handle_conversation_end``)。旧実装 (running Track が
    user_conversation 種別か) は、Track がもう時間経過で pending に落ちない
    (running のまま残り続けうる) ため使えない。
    """
    try:
        from saiverse import episodes

        ep = episodes.get_open_episode(
            manager, persona_id, kind=episodes.KIND_CONVERSATION,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] get_open_episode failed (persona=%s); treating as not in conversation",
            persona_id, exc_info=True,
        )
        return False
    return ep is not None


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


# ---------------------------------------------------------------------------
# コマの出来事 (Episode): 実行区間の記録 (life_concept_map.md §8.1)
# ---------------------------------------------------------------------------


def _episode_kind_for_slot(slot_kind: Any) -> str:
    """コマ種別 → 出来事 kind の写像。

    暮らし/休む は「その場に居た」以上の実行記録が無いスタブなので presence。
    六型の作業コマは中の作業セッションが別の出来事 (kind='work_session') を
    開くため、コマの実行区間そのものは kind='slot' として並存させる
    (セッション側の origin_ref がコマ出来事を指して親子が読める)。
    """
    from saiverse import episodes

    if slot_kind in (KIND_LIVING, KIND_REST):
        return episodes.KIND_PRESENCE
    return episodes.KIND_SLOT


def _slot_origin_ref(persona_id: str, plan_date_str: str, index: int) -> str:
    """コマ参照 (出来事の origin_ref)。EventScheduler の予約 key と同形の
    決定論文字列。コマ参照の統一文法化は P5 (life_concept_map.md §14) で
    再訪する。"""
    return _slot_key(persona_id, plan_date_str, index)


def _open_slot_episode(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> Optional[str]:
    """コマの実行区間の出来事を開き、episode_ref を返す (失敗時 None)。

    呼び出し点は発火チェック (繰り下げ / 予算 / ハンドラ有無) と施設移動を
    抜けた後 — skip されたコマは出来事を作らない。場所は移動後の現在地。
    出来事は記録専用でコマの実行には影響しない (失敗は WARN のみ)。
    """
    try:
        from saiverse import episodes

        persona = (getattr(manager, "personas", {}) or {}).get(persona_id)
        building_id = getattr(persona, "current_building_id", None)
        meta: Dict[str, Any] = {"slot_kind": str(slot.get("kind") or "")}
        title = str(slot.get("title") or "").strip()
        if title:
            meta["title"] = title
        ep = episodes.open_episode(
            manager, persona_id,
            _episode_kind_for_slot(slot.get("kind")),
            building_id=building_id,
            participants=[persona_id],
            origin_ref=_slot_origin_ref(persona_id, plan_date_str, index),
            meta=meta,
        )
        return ep.get("episode_ref")
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to open slot episode (persona=%s date=%s index=%d)",
            persona_id, plan_date_str, index, exc_info=True,
        )
        return None


def _close_slot_episode(
    manager: Any,
    persona_id: str,
    episode_ref: Optional[str],
    slot_after: Optional[Dict[str, Any]],
) -> None:
    """コマの出来事を閉じる。record_level (presence_only 等) を meta に透過する。"""
    if not episode_ref:
        return
    try:
        from saiverse import episodes

        meta: Optional[Dict[str, Any]] = None
        record_level = str((slot_after or {}).get("record_level") or "")
        if record_level:
            meta = {"record_level": record_level}
        episodes.close_episode(manager, persona_id, episode_ref, meta=meta)
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to close slot episode %s (persona=%s)",
            episode_ref, persona_id, exc_info=True,
        )


def _apply_budget_gate(
    manager: Any, persona_id: str, plan_date_str: str, index: int, slot: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """予算ゲート (v2 §4.5 / life.md Phase2 §7)。切り詰め後の slot を返す。
    発火中止なら None。

    - ライフが宣言されている日 (:func:`get_lives` 非空): そのコマが属する
      ライフの残 (budget_pulses − 消費) を見る二値ゲート
      (:func:`_apply_life_budget_gate`)。ラウンド数のクランプはしない —
      パルスとラウンドは単位が異なるため
    - ライフの無い日: 旧ゲート (台帳が無ければ無効 = slot をそのまま返す。
      残高 < 実効予算 → 残高まで切り詰め + WARN。残高 0 → skipped + WARN)
    """
    lives = get_lives(manager, persona_id, plan_date_str)
    if lives:
        return _apply_life_budget_gate(manager, persona_id, plan_date_str, index, slot, lives)

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

    # ライフのパルス消費 (life.md Phase2 §7): このコマの発火 = 標準パルス 1 回。
    # lives の無い日は no-op (consume_life_pulse 内で判定)。
    try:
        consume_life_pulse(manager, persona_id, plan_date_str)
    except Exception:
        LOGGER.warning(
            "[day_plan] consume_life_pulse failed (persona=%s date=%s index=%d); continuing",
            persona_id, plan_date_str, index, exc_info=True,
        )

    # コマの実行区間を出来事として開く (skip 済み経路はここに到達しない =
    # skip されたコマは出来事を作らない。life_concept_map.md §8.1)
    episode_ref = _open_slot_episode(manager, persona_id, plan_date_str, slot, index)

    # desire 参照コマの発火 = 欲求への再訪。帳簿 (touch_count / 鮮度) に記録する
    # (v2 §5.3「何度も選ばれ再訪される欲求は関心に深まる」)。ハンドラの成否に
    # 依らず「取り組みに向かった」事実を記録するため、実行前に付ける。
    ref = slot.get("ref") or REF_NONE
    # 欲求コマ発火 = 再訪記録。参照アドレッシング統一 (Q2) で全 ref が task:N に
    # なり prefix では desire を判別できないが、touch_desire は stage='candidate'
    # でなければ安全に no-op するので、task ref すべてに対して呼ぶ。
    if ref != REF_NONE and ref.startswith("task:"):
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
        # 実行区間は終わった (異常終了) — 出来事は閉じる (事実の記録)。
        _close_slot_episode(manager, persona_id, episode_ref, None)
        return

    # (e) 実測の消費ラウンドを日次予算台帳へ積算する (v2 §4.5 / life.md Phase2 §7)
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
        try:
            consume_life_rounds(
                manager, persona_id, plan_date_str, used_rounds, at_time=slot.get("start"),
            )
        except Exception:
            LOGGER.exception(
                "[day_plan] consume_life_rounds failed (persona=%s date=%s index=%d); "
                "continuing",
                persona_id, plan_date_str, index,
            )
    done_slot = _update_slot(manager, persona_id, plan_date_str, index, status=STATUS_DONE)
    # 出来事を閉じる。record_level (presence_only 等、ハンドラが永続化した
    # 完了記録の詳しさ) は DONE 更新後の slot から透過する。
    _close_slot_episode(manager, persona_id, episode_ref, done_slot)


# ---------------------------------------------------------------------------
# ref 解決 (task:N / desire:N → タイトル等)
# ---------------------------------------------------------------------------


def _resolve_ref(manager: Any, persona_id: str, ref: str) -> Optional[str]:
    """ref を人間可読なタイトル/目標へ解決する。none / 解決不能は None。

    desire は親なし + stage='candidate' の目的ノード (P3c-0 desire 正規化) で
    あり、task:N と同じ short_id 参照空間を共有する (persona_task_manager.py)。
    したがって "desire:N" も同じ短縮参照 N で解決する。
    """
    if not ref or ref == REF_NONE:
        return None
    m = _REF_RE.match(ref)
    if m is None:
        LOGGER.warning("[day_plan] unresolvable ref format: %r", ref)
        return None

    from saiverse.persona_task_manager import (
        STAGE_CANDIDATE,
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

    if m.group(1) == "desire" and task.get("stage") != STAGE_CANDIDATE:
        LOGGER.warning(
            "[day_plan] ref %r resolved to a non-desire task (stage=%r); using it anyway",
            ref, task.get("stage"),
        )

    title = task.get("title") or "(無題)"
    goal = (task.get("goal") or "").strip()
    return f"{title}（目標: {goal}）" if goal else title


# ---------------------------------------------------------------------------
# 大枝 (track:N) コマの指示書 (P5: コマ参照の任意階層化、life_concept_map.md §3.1)
# ---------------------------------------------------------------------------


def _read_track_desk_memo(track: Any) -> Optional[Dict[str, Any]]:
    """Track metadata から作業メモ (desk_memo) を読む。無ければ None。"""
    raw = getattr(track, "track_metadata", None)
    if not raw:
        return None
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError):
        return None
    memo = metadata.get("desk_memo") if isinstance(metadata, dict) else None
    return memo if isinstance(memo, dict) else None


def _build_track_instruction(
    manager: Any, persona_id: str, slot: Dict[str, Any], ref: str
) -> tuple[Optional[str], Optional[str]]:
    """track:N コマの指示書を Track の文脈 (title・机メモ・配下の生存タスク) から組む。

    大枝コマの実行意味 (§3.1「中身はその場の判断」): 発火時に Track の状況を
    見てセッション 1 本を回す。**中身が空の Track** (配下に生存タスクが無く
    机メモも無い) は presence 相当に縮退する — 充填生成でセッションを回すと
    v1 の空回りに逆戻りするため (§6 無意味の予算の注意)。

    Returns:
        ``(instruction, track_id)``。縮退時は ``(None, track_id)``。
        Track が解決できない場合は ``(None, None)`` (呼び出し側で WARN 済み想定)。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        LOGGER.warning("[day_plan] track ref %r but manager has no track_manager", ref)
        return None, None
    try:
        track_id = track_manager.resolve_track_ref(persona_id, ref)
        track = track_manager.get(track_id)
    except Exception:
        LOGGER.warning(
            "[day_plan] track ref %r could not be resolved (persona=%s)",
            ref, persona_id, exc_info=True,
        )
        return None, None

    title = getattr(track, "title", None) or "(無題)"
    intent = (getattr(track, "intent", None) or "").strip()
    memo = _read_track_desk_memo(track)

    from saiverse.persona_task_manager import PersonaTaskManager

    try:
        ptm = PersonaTaskManager(manager.SessionLocal)
        live_tasks = ptm.list_tasks(
            persona_id, track_id=track_id,
            statuses=("pending", "active", "paused"), include_steps=False,
        )
    except Exception:
        LOGGER.warning(
            "[day_plan] failed to list tasks under track %s (persona=%s)",
            track_id, persona_id, exc_info=True,
        )
        live_tasks = []

    if not live_tasks and not memo:
        # 中身が空 → presence 縮退 (呼び出し側が record_level を刻む)
        return None, track_id

    note = (slot.get("note") or "").strip()
    parts = [f"目的: 関心「{title}」を前に進める。"]
    if intent:
        parts.append(f"この関心の意図: {intent}")
    if note:
        parts.append(f"このコマの覚え書き: {note}")
    if memo:
        memo_label = "詰まり" if memo.get("status") == "blocked" else "続き"
        parts.append(
            f"前回の作業メモ [{memo_label}]: {str(memo.get('text') or '').strip() or '(記載なし)'}"
        )
    if live_tasks:
        lines = ["この関心の下にあるタスク:"]
        for t in live_tasks:
            t_ref = t.get("task_ref") or "task:?"
            goal = (t.get("goal") or "").strip()
            lines.append(
                f"- {t_ref} [{t.get('status')}] {t.get('title') or '(無題)'}"
                + (f"（目標: {goal}）" if goal else "")
            )
        parts.append("\n".join(lines))
    parts.append(
        "この中から今のコマで実際に進められることを選んで取り組むこと。"
        "スペルで実際に実行・確認できたことだけを行い、成果は document_create 等の"
        "スペルで実際に残すこと。"
        "完成条件: 実際にやったことが読み返せる形で残っていること。"
        "実際にやったこと以外を「やった」と書かないこと。"
    )
    return "\n".join(parts), track_id


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

    ref の階層でセッションの形が変わる (P5, life_concept_map.md §3.1):

    - task:N / desire:N / none — 型別テンプレートの指示書 (従来どおり。
      desire コマ = お試し採用: 発火時にそのまま欲求を生きる。採用への昇格は
      しない — それは判断点の仕事)
    - track:N (大枝) — Track の title・机メモ・配下の生存タスクから指示書を
      組む (:func:`_build_track_instruction`)。**中身が空の Track は presence
      相当に縮退** し、セッションを回さず ``None`` を返す (呼び出し側は
      判断点を撃たず予算も消費しない)

    Returns:
        ``sea.work_session.WorkSessionResult`` (raise しない契約)。
        presence 縮退時のみ ``None``。
    """
    kind = slot["kind"]
    ref = slot.get("ref") or REF_NONE
    track_id: Optional[str] = None
    if _TRACK_REF_RE.match(ref):
        instruction, track_id = _build_track_instruction(manager, persona_id, slot, ref)
        if instruction is None:
            # 中身が空 (または解決不能) → presence 縮退。詳細な実行記録が
            # 無いことを slot に永続化する (暮らし/休む スタブと同じ正直さ)。
            LOGGER.info(
                "[day_plan] track slot degraded to presence (empty track): "
                "persona=%s date=%s index=%d ref=%s",
                persona_id, plan_date_str, index, ref,
            )
            _update_slot(
                manager, persona_id, plan_date_str, index,
                record_level=RECORD_LEVEL_PRESENCE_ONLY,
            )
            return None
    else:
        template = _WORKER_INSTRUCTION_TEMPLATES[kind]
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

    # track:N コマではセッションの対象タスクは無い (Track 単位の取り組み)。
    # task_ref を track 参照で埋めると post_session の task_verdict が偽対象を
    # 裁定してしまうため、track_id 側に流す。
    is_track_ref = track_id is not None
    result = run_work_session(
        persona_id,
        instruction,
        budget,
        task_ref=ref if (ref != REF_NONE and not is_track_ref) else None,
        metadata={"day_plan": {"plan_date": plan_date_str, "slot_index": index, "kind": kind}},
        manager=manager,
        track_id=track_id,
        title=str(slot.get("title") or "").strip() or None,
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
    """六型の作業コマの組み込みハンドラ (本番の恒久配線)。

    セッション運転の後に **セッション終了判断 (post_session)** を撃つ
    (v2 §4.2 の背骨。かつては ScenarioPlayer のラップハンドラだけが担っていた
    接続の恒久化)。判断は ``saiverse.autonomy_wiring.fire_judgment_point`` の
    本番ゲート (Active のみ / Playbook 欠如は WARNING スキップ) を通る。
    シム (ScenarioPlayer) は実行中このハンドラ自体を自前のラップに差し替える
    ため二重発火しない (day_scenario.py の register_slot_handler 上書き)。

    NOTE: post_session 判断はコマの status が done になる前 (fired のまま) に
    走る — 判断が見る「残りの時間割」に当該コマは含まれない。予算台帳への
    積算は判断の後、戻り値経由で行われる (ScenarioPlayer のラップと同順序)。

    Returns:
        実際に消費したラウンド数 (``_fire_slot`` が予算台帳へ積算する)。
    """
    result = run_worker_slot_session(manager, persona_id, plan_date_str, slot, index)
    if result is None:
        # 中身が空の track コマの presence 縮退 (P5)。セッションが走っていない
        # ので判断点も撃たない (偽前提の状況テキストは作話を誘発する)。
        return 0
    try:
        from saiverse.autonomy_wiring import fire_judgment_point
        from saiverse.judgment_points import KIND_POST_SESSION

        ref = str(slot.get("ref") or REF_NONE)
        context: Dict[str, Any] = {
            "session_result": result,
            "budget_rounds": int(slot.get("budget_rounds") or 0) or None,
        }
        # track:N コマの対象は Track (WorkSessionResult.track_id 経由で判断点へ
        # 届く)。task_ref に track 参照を入れると task_verdict が壊れる。
        if ref != REF_NONE and not _TRACK_REF_RE.match(ref):
            context["task_ref"] = ref
        fire_judgment_point(manager, persona_id, KIND_POST_SESSION, context)
    except Exception:
        LOGGER.exception(
            "[day_plan] post_session judgment failed (persona=%s date=%s index=%d); "
            "slot bookkeeping continues",
            persona_id, plan_date_str, index,
        )
    return worker_session_rounds_used(result)


def _handle_living_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「暮らし」コマ: ログのみのスタブ。暮らし Pulse は後続フェーズで刺さる。

    施設への実移動 (presence) は ``_fire_slot`` (c) で済んでおり、それだけは
    本物 — カフェ等に実際に居ることが遭遇と会話のきっかけになる (まはー決定
    2026-07-05)。ここでは「詳細な実行記録が無い」ことを slot に永続化し、
    表示側が「実行済み」と偽らないようにする。
    """
    LOGGER.info(
        "[day_plan] living slot (stub — 暮らし Pulse は後続フェーズ): "
        "persona=%s date=%s index=%d note=%r",
        persona_id, plan_date_str, index, slot.get("note"),
    )
    _update_slot(
        manager, persona_id, plan_date_str, index,
        record_level=RECORD_LEVEL_PRESENCE_ONLY,
    )


def _handle_rest_slot(
    manager: Any, persona_id: str, plan_date_str: str, slot: Dict[str, Any], index: int
) -> None:
    """「休む」コマ: 何もしない。不作為の可視化として INFO で記録する (v2 §4.2)。

    暮らしと同じく、詳細な実行記録が無いことを slot に永続化する
    (:data:`RECORD_LEVEL_PRESENCE_ONLY`)。
    """
    LOGGER.info(
        "[day_plan] rest slot: persona=%s date=%s index=%d — 何もしない "
        "(コマとして明示的に選ばれた休息) note=%r",
        persona_id, plan_date_str, index, slot.get("note"),
    )
    _update_slot(
        manager, persona_id, plan_date_str, index,
        record_level=RECORD_LEVEL_PRESENCE_ONLY,
    )


for _kind in WORKER_SESSION_KINDS:
    register_slot_handler(_kind, _handle_worker_slot, consumes_budget=True)
register_slot_handler(KIND_LIVING, _handle_living_slot)
register_slot_handler(KIND_REST, _handle_rest_slot)
