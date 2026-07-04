"""判断点コーディネータ (自律行動 v2 の意思決定層、judgment_points.md)。

判断点 = ペルソナが「何を見て、どういうスキーマで意思決定を出力するか」が
定義された LLM 呼び出し。meta_judgment v2 で確立した様式
(docs/intent/persona_cognition/meta_judgment_structured.md) をそのまま継承する:

1. 状況テキストは tail 注入 (Playbook judge ノードの action テンプレートに展開。
   head は不変、キャッシュ保護)
2. LLM は動的 ``response_schema`` に従う JSON を返す (function calling は使わない)
3. finalize ツール (``builtin_data/tools/judgment_finalize.py``) が JSON を
   検証・適用し、メインキャッシュには整形済み独白＋要約行のみを残す
   (JSON 非混入、不変条件 v2-A 継承)
4. 選択肢は動的 enum 注入で物理的に絞る (実在しないものは構造的に選べない)
5. ``additionalProperties`` はスキーマにハードコードしない (プロバイダ正規化層に
   任せる。meta_judgment_structured.md §Phase4 の Gemini 事故の教訓)

本フェーズの判断点は 2 種:

- ``day_open``   — 起床判断: 時間割の編成 + 予算配分 (+ 欲求→関心の昇格)
- ``post_session`` — セッション終了判断: タスクの裁定 (接地検証つき) + 次への接続

モデルは standard (META 相当): 起動は ``PulseController.submit_meta_judgment``
(= ``pulse_type="meta_judgment"``) を使い、``sea.pulse_context.aspect_from_pulse_type``
が META アスペクト (line_role='meta_judgment' / scope='discardable' / standard tier)
を導出する — meta_judgment Playbook の起動経路と同一。

**自動起動の配線は本モジュールではしない** (中間起動の空打ち防止、
feedback_phased_implementation_intermediate_run)。呼び出しはシム / テスト /
後続フェーズの配線 (PersonaSchedule 起床時刻 / セッションランナー終了) から行う。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from saiverse import clock
from saiverse.day_plan import (
    ALL_KINDS,
    FACILITY_OWN_ROOM,
    KIND_LIVING,
    KIND_REST,
    REF_NONE,
    STATUS_DEFERRED,
    STATUS_PENDING,
    load_day_plan,
    load_plan_meta,
)
from saiverse.desire_engine import (
    CANDIDATE_STATUSES,
    DESIRE_TYPES,
    decay_desires,
    desire_summary_for_prompt,
    promotion_candidates,
)
from saiverse.note_manager import NOTE_TYPE_DESIRE, NoteManager
from saiverse.persona_task_manager import (
    PARENT_NOTE,
    PersonaTaskManager,
    TaskNotFoundError,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

KIND_DAY_OPEN = "day_open"
KIND_POST_SESSION = "post_session"

#: 判断点 kind → Playbook 名 (builtin_data/playbooks/public/)。
#: 後続フェーズで post_conversation / on_event / day_close が加わる。
JUDGMENT_PLAYBOOK_MAP: Dict[str, str] = {
    KIND_DAY_OPEN: "judgment_day_open",
    KIND_POST_SESSION: "judgment_post_session",
}

#: 日次予算 (ラウンド) の既定値。予算ゲート (v2 §4.5) が乗るまでの素朴な形
#: (セッション数 × ラウンド上限 ≒ 5 × 8)。context["daily_budget_rounds"] で上書き可。
DEFAULT_DAILY_BUDGET_ROUNDS = 40

#: バックログとして提示するタスクの status (生きているもの)
BACKLOG_TASK_STATUSES = ("pending", "active", "paused")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REF_RE = re.compile(r"^(task|desire):(\d+)$")


def normalize_task_ref(ref: str) -> str:
    """``desire:N`` を ``task:N`` へ正規化する (同じ short_id 参照空間。day_plan 参照)。"""
    ref = (ref or "").strip()
    if ref.startswith("desire:"):
        return "task:" + ref[len("desire:"):]
    return ref


# ---------------------------------------------------------------------------
# 動的 enum の収集
# ---------------------------------------------------------------------------


def collect_facility_ids(manager: Any) -> List[str]:
    """コマの facility enum: 実在の Building ID 一覧 + "own_room"。

    TODO(#7): 公共施設タグは未実装のため当面は全 Building を提示する。
    タグが乗ったら公共施設のみに絞る (judgment_points.md §3.2 / v2 §6.1)。
    """
    out: List[str] = []
    for b in getattr(manager, "buildings", None) or []:
        bid = getattr(b, "building_id", None)
        if bid:
            out.append(bid)
    out.append(FACILITY_OWN_ROOM)
    return out


def _list_backlog_tasks(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """バックログタスク (desire 候補を除く生きているタスク) の dict リスト。"""
    ptm = PersonaTaskManager(manager.SessionLocal)
    tasks = ptm.list_tasks(
        persona_id, statuses=BACKLOG_TASK_STATUSES, include_steps=False,
    )
    return [t for t in tasks if t.get("parent_kind") != PARENT_NOTE]


def _list_desire_tasks(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """生きている欲求候補 (desire ノート内 Task) の dict リスト。"""
    nm = NoteManager(manager.SessionLocal)
    notes = nm.list_for_persona(persona_id, note_type=NOTE_TYPE_DESIRE)
    if not notes:
        return []
    ptm = PersonaTaskManager(manager.SessionLocal)
    return ptm.list_tasks(
        persona_id,
        note_id=notes[0].note_id,
        parent_kind=PARENT_NOTE,
        statuses=CANDIDATE_STATUSES,
        include_steps=False,
    )


def collect_slot_ref_enum(manager: Any, persona_id: str) -> List[str]:
    """コマの ref enum: 実在の task:N / desire:N + "none" (judgment_points.md §3.2)。"""
    refs: List[str] = []
    for t in _list_backlog_tasks(manager, persona_id):
        ref = t.get("task_ref")
        if ref:
            refs.append(ref)
    for t in _list_desire_tasks(manager, persona_id):
        ref = t.get("task_ref")
        if ref:
            refs.append("desire:" + ref[len("task:"):])
    refs.append(REF_NONE)
    return refs


def collect_promotion_refs(manager: Any, persona_id: str) -> List[str]:
    """promotions.desire_ref enum: 再訪回数が閾値を超えた欲求のみ (desire:N 形式)。"""
    out: List[str] = []
    for c in promotion_candidates(manager, persona_id):
        ref = c.get("task_ref") or ""
        if ref.startswith("task:"):
            out.append("desire:" + ref[len("task:"):])
    return out


# ---------------------------------------------------------------------------
# response_schema (動的 enum 注入)
# ---------------------------------------------------------------------------


def _build_slot_schema(ref_enum: List[str], facility_enum: List[str]) -> Dict[str, Any]:
    """時間割コマの共通スキーマ部品 (judgment_points.md §3.2)。

    ``additionalProperties`` は出さない (プロバイダ正規化層に任せる)。
    """
    return {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "開始時刻 HH:MM (24時間制)。コマは開始時刻の厳密昇順に並べる",
            },
            "kind": {"type": "string", "enum": list(ALL_KINDS)},
            "ref": {
                "type": "string",
                "enum": list(ref_enum),
                "description": "取り組む対象 (実在のタスク/欲求)。「暮らし」「休む」は 'none'",
            },
            "facility": {
                "type": "string",
                "enum": list(facility_enum),
                "description": "コマを過ごす場所 (Building ID または own_room)",
            },
            "budget_rounds": {
                "type": "integer",
                "description": "このコマの作業ラウンド予算 (暮らし/休む は 0)",
            },
            "note": {"type": "string", "description": "このコマで何をするかの短い覚え書き"},
        },
        "required": ["start", "kind", "ref", "facility", "note"],
    }


def _new_desires_schema() -> Dict[str, Any]:
    """new_desires 共通フィールド (judgment_points.md §3.1)。"""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": list(DESIRE_TYPES)},
                "title": {"type": "string"},
                "source_quote": {
                    "type": "string",
                    "description": "この欲求を生んだ直前の実経験からの引用（発言・出来事・読んだ文）",
                },
            },
            "required": ["type", "title", "source_quote"],
        },
    }


def build_day_open_schema(manager: Any, persona_id: str) -> Dict[str, Any]:
    """起床判断の response_schema (judgment_points.md §4)。

    - timetable のコマ ref / facility は実在リストの動的 enum
    - promotions は昇格候補が空なら **フィールド自体を出さない**
      (空 enum 事故防止、meta_judgment_structured.md §9)
    """
    slot = _build_slot_schema(
        collect_slot_ref_enum(manager, persona_id),
        collect_facility_ids(manager),
    )
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "monologue": {"type": "string"},
            "timetable": {"type": "array", "minItems": 1, "items": slot},
        },
        "required": ["monologue", "timetable"],
    }
    promo_refs = collect_promotion_refs(manager, persona_id)
    if promo_refs:
        schema["properties"]["promotions"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "desire_ref": {"type": "string", "enum": promo_refs},
                    "title": {"type": "string"},
                    "intent": {"type": "string"},
                },
                "required": ["desire_ref", "title", "intent"],
            },
        }
    return schema


def build_post_session_schema(
    manager: Any,
    persona_id: str,
    artifacts: List[str],
    task_ref: Optional[str],
    track_id: Optional[str],
) -> Dict[str, Any]:
    """セッション終了判断の response_schema (judgment_points.md §6)。

    **接地の要**: done 分岐の artifact_ref enum は「このセッションが実際に作った
    成果物」のみ。成果物ゼロのセッションでは done 分岐 (anyOf の第 1 分岐) を
    スキーマから除去する — やったフリはスキーマのレベルで構造的に不可能になる。
    """
    slot = _build_slot_schema(
        collect_slot_ref_enum(manager, persona_id),
        collect_facility_ids(manager),
    )
    props: Dict[str, Any] = {"monologue": {"type": "string"}}
    required = ["monologue"]

    if task_ref:
        variants: List[Dict[str, Any]] = []
        if artifacts:
            variants.append({
                "type": "object",
                "properties": {
                    "status": {"type": "string", "const": "done"},
                    "artifact_ref": {"type": "string", "enum": list(artifacts)},
                    "desk_memo": {"type": "string"},
                },
                "required": ["status", "artifact_ref", "desk_memo"],
            })
        variants.append({
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["continue", "blocked"]},
                "desk_memo": {
                    "type": "string",
                    "description": "どこまでやった・次はどこから・何に詰まったか",
                },
            },
            "required": ["status", "desk_memo"],
        })
        props["task_verdict"] = {"anyOf": variants}
        required.append("task_verdict")

    if track_id:
        props["track_op"] = {"type": "string", "enum": ["none", "complete"]}

    props["new_desires"] = _new_desires_schema()
    props["remaining_timetable"] = {
        "anyOf": [
            {"type": "array", "items": slot},
            {"type": "null"},
        ],
    }
    required.append("remaining_timetable")

    return {"type": "object", "properties": props, "required": required}


# ---------------------------------------------------------------------------
# 状況テキスト (tail 注入)
# ---------------------------------------------------------------------------


def _format_track_backlog(manager: Any, persona_id: str) -> str:
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return "(Track 情報は取得できませんでした)"
    from saiverse.track_manager import LIVE_STATUSES

    try:
        tracks = track_manager.list_for_persona(persona_id, statuses=LIVE_STATUSES)
    except Exception:
        LOGGER.warning(
            "[judgment] failed to list tracks for %s", persona_id, exc_info=True,
        )
        return "(Track 情報は取得できませんでした)"
    if not tracks:
        return "進行中の Track はありません。"
    lines = ["Track:"]
    for t in tracks:
        short = f"t:{t.short_id}" if t.short_id is not None else t.track_id[:8]
        lines.append(
            f"- {short} [{t.track_type}/{t.status}] {t.title or '(無題)'}"
        )
    return "\n".join(lines)


def _format_task_backlog(manager: Any, persona_id: str) -> str:
    tasks = _list_backlog_tasks(manager, persona_id)
    if not tasks:
        return "バックログのタスクはありません。"
    lines = ["タスクバックログ:"]
    for t in tasks:
        ref = t.get("task_ref") or "task:?"
        has_artifact = "あり" if t.get("artifact_refs") else "なし"
        lines.append(
            f"- {ref} [{t.get('status')}] {t.get('title') or '(無題)'}"
            f" (成果物参照: {has_artifact})"
        )
    return "\n".join(lines)


def _format_facilities(manager: Any) -> str:
    lines = ["行ける場所:"]
    for b in getattr(manager, "buildings", None) or []:
        bid = getattr(b, "building_id", None)
        if not bid:
            continue
        name = getattr(b, "name", "") or bid
        lines.append(f"- {bid}: {name}")
    lines.append(f"- {FACILITY_OWN_ROOM}: 自分の部屋")
    return "\n".join(lines)


def _format_remaining_timetable(manager: Any, persona_id: str, plan_date: str) -> str:
    slots = load_day_plan(manager, persona_id, plan_date)
    if not slots:
        return "今日の時間割はありません。"
    remaining = [
        s for s in slots if s.get("status") in (STATUS_PENDING, STATUS_DEFERRED)
    ]
    if not remaining:
        return "今日の残りのコマはありません。"
    lines = ["残りの時間割:"]
    for s in remaining:
        lines.append(
            f"- {s.get('start')} {s.get('kind')} ref={s.get('ref')}"
            f" @{s.get('facility')} 予算{s.get('budget_rounds', 0)} {s.get('note') or ''}".rstrip()
        )
    return "\n".join(lines)


def _yesterday_review_text(manager: Any, persona_id: str, yesterday: str) -> str:
    """昨日のふりかえり素材。day_close (後続 4b) が meta_json に書く day_digest を
    優先し、無ければ昨日の時間割の予定 vs 実績を要約する。"""
    meta = load_plan_meta(manager, persona_id, yesterday)
    digest = (meta.get("day_digest") or "").strip() if isinstance(meta, dict) else ""
    if digest:
        return digest
    slots = load_day_plan(manager, persona_id, yesterday)
    if not slots:
        return "(昨日の記録はありません)"
    lines = ["昨日の時間割の実績:"]
    for s in slots:
        lines.append(
            f"- {s.get('start')} {s.get('kind')} ref={s.get('ref')}"
            f" → {s.get('status')}"
        )
    return "\n".join(lines)


def build_day_open_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """起床判断の tail 注入テキスト (judgment_points.md §4「見るもの」)。"""
    now = clock.now()
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()

    memo = (load_plan_meta(manager, persona_id, yesterday).get("tomorrow_memo") or "").strip()
    budget = context.get("daily_budget_rounds") or DEFAULT_DAILY_BUDGET_ROUNDS

    parts = [
        "[起床判断]",
        f"おはようございます。今日 ({today}) の一日が始まります。",
        "机メモ・昨日のふりかえり・バックログ・やりたいこと候補を見て、"
        "今日の時間割を編成してください。",
        "",
        "[昨日の自分からの机メモ]",
        memo or "(机メモはありません)",
        "",
        "[昨日のふりかえり]",
        _yesterday_review_text(manager, persona_id, yesterday),
        "",
        "[進行中のことと、やりたいこと]",
        _format_track_backlog(manager, persona_id),
        "",
        _format_task_backlog(manager, persona_id),
        "",
        desire_summary_for_prompt(manager, persona_id),
        "",
        "[今日の予算]",
        f"作業ラウンドの日次予算: {budget} (全コマの budget_rounds 合計の目安)",
        "",
        "[施設一覧]",
        _format_facilities(manager),
    ]
    events = (context.get("scheduled_events") or "").strip() if isinstance(
        context.get("scheduled_events"), str
    ) else ""
    if events:
        parts += ["", "[予定されたイベント]", events]
    return "\n".join(parts)


def _ws_get(session_result: Any, key: str, default: Any = None) -> Any:
    """WorkSessionResult (dataclass) / dict の両方から値を読む。"""
    if isinstance(session_result, dict):
        return session_result.get(key, default)
    return getattr(session_result, key, default)


def build_post_session_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """セッション終了判断の tail 注入テキスト (judgment_points.md §6「見るもの」)。"""
    now = clock.now()
    today = now.date().isoformat()
    sr = context.get("session_result")

    digest = str(_ws_get(sr, "digest", "") or "").strip()
    artifacts = list(_ws_get(sr, "artifacts", None) or [])
    rounds_used = _ws_get(sr, "rounds_used", 0) or 0
    ended_reason = _ws_get(sr, "ended_reason", "") or "?"
    budget_rounds = context.get("budget_rounds") or _ws_get(sr, "budget_rounds", None)

    task_ref = context.get("task_ref") or _ws_get(sr, "task_ref", None)
    task_line = "(対象タスクなし)"
    if task_ref:
        try:
            ptm = PersonaTaskManager(manager.SessionLocal)
            task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(str(task_ref)))
            task = ptm.get_task(task_id, persona_id=persona_id)
            goal = (task.get("goal") or "").strip()
            task_line = f"{task_ref} [{task.get('status')}] {task.get('title') or '(無題)'}"
            if goal:
                task_line += f"（目標: {goal}）"
        except TaskNotFoundError:
            task_line = f"{task_ref} (解決できませんでした)"

    budget_text = (
        f"{rounds_used}/{budget_rounds}" if budget_rounds else f"{rounds_used}"
    )
    parts = [
        "[セッション終了判断]",
        "作業セッションが終わりました。実際に起きたことに基づいて、"
        "タスクの裁定と次への接続を決めてください。",
        "",
        "[セッションの実績]",
        f"対象タスク: {task_line}",
        f"使用ラウンド: {budget_text} (終了理由: {ended_reason})",
        "ダイジェスト:",
        digest or "(ダイジェストはありません)",
        "",
        "[このセッションで実際に作った成果物]",
    ]
    if artifacts:
        parts += [f"- {a}" for a in artifacts]
    else:
        parts.append(
            "(成果物はありません — このセッションでは「完了 (done)」は選べません)"
        )
    parts += [
        "",
        f"現在時刻: {now.strftime('%H:%M')}",
        _format_remaining_timetable(manager, persona_id, today),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 判断点の起動
# ---------------------------------------------------------------------------


def build_judgment_args(
    manager: Any, persona_id: str, kind: str, context: Dict[str, Any]
) -> Dict[str, Any]:
    """判断点 Playbook に渡す args (situation_text + response_schema + judgment_context)。

    day_open では前処理として ``decay_desires`` を deterministic に実行する
    (judgment_points.md §4「減衰の帳簿処理はこの判断の前に済ませる」)。
    """
    today = clock.now().date().isoformat()

    if kind == KIND_DAY_OPEN:
        decay_desires(manager, persona_id)
        situation_text = build_day_open_situation_text(manager, persona_id, context)
        response_schema = build_day_open_schema(manager, persona_id)
        judgment_context: Dict[str, Any] = {
            "plan_date": today,
            "daily_budget_rounds": (
                context.get("daily_budget_rounds") or DEFAULT_DAILY_BUDGET_ROUNDS
            ),
        }
    elif kind == KIND_POST_SESSION:
        sr = context.get("session_result")
        artifacts = [str(a) for a in (_ws_get(sr, "artifacts", None) or [])]
        task_ref = context.get("task_ref") or _ws_get(sr, "task_ref", None)
        track_id = context.get("track_id") or _ws_get(sr, "track_id", None)
        situation_text = build_post_session_situation_text(manager, persona_id, context)
        response_schema = build_post_session_schema(
            manager, persona_id, artifacts,
            str(task_ref) if task_ref else None,
            str(track_id) if track_id else None,
        )
        judgment_context = {
            "plan_date": today,
            "artifacts": artifacts,
            "task_ref": str(task_ref) if task_ref else None,
            "track_id": str(track_id) if track_id else None,
        }
    else:
        raise ValueError(f"unknown judgment kind: {kind!r}")

    return {
        "situation_text": situation_text,
        "response_schema": response_schema,
        "judgment_context": json.dumps(judgment_context, ensure_ascii=False),
    }


def run_judgment_point(
    manager: Any,
    persona_id: str,
    kind: str,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """判断点を 1 回起動する (状況テキスト組み立て → 動的スキーマ → Playbook 起動)。

    起動経路は meta_judgment v2 と同一: ``PulseController.submit_meta_judgment``
    (pulse_type="meta_judgment" → META アスペクト → standard モデル)。
    Playbook 内の judge ノードが構造化出力を生成し、``judgment_finalize`` ツールが
    検証・適用・SAIMemory 書き込みを行う。

    Args:
        context: 判断点ごとの入力。
            - day_open: ``daily_budget_rounds`` (省略可) / ``scheduled_events`` (省略可)
            - post_session: ``session_result`` (WorkSessionResult または dict、必須) /
              ``task_ref`` / ``track_id`` / ``budget_rounds`` (いずれも省略時は
              session_result から読む)

    Returns:
        ``{"kind", "playbook", "args", "submitted": bool, "errors": [...]}``。
        起動できなかった場合は ``submitted=False`` + ``reason``。
    """
    context = context or {}
    playbook_name = JUDGMENT_PLAYBOOK_MAP.get(kind)
    if playbook_name is None:
        raise ValueError(
            f"unknown judgment kind: {kind!r} (expected one of {sorted(JUDGMENT_PLAYBOOK_MAP)})"
        )

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        LOGGER.warning(
            "[judgment] persona %s not loaded; cannot run %s", persona_id, kind,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "persona not loaded"}

    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        LOGGER.warning(
            "[judgment] persona %s has no current_building_id; cannot run %s",
            persona_id, kind,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "no current building"}

    pulse_controller = getattr(manager, "pulse_controller", None)
    if pulse_controller is None:
        LOGGER.warning(
            "[judgment] manager has no pulse_controller; cannot run %s for %s",
            kind, persona_id,
        )
        return {"kind": kind, "playbook": playbook_name, "submitted": False,
                "reason": "no pulse_controller"}

    args = build_judgment_args(manager, persona_id, kind, context)

    errors: List[Dict[str, Any]] = []

    def _capture_event(ev: Dict[str, Any]) -> None:
        if isinstance(ev, dict) and ev.get("type") == "error":
            errors.append(ev)

    LOGGER.info(
        "[judgment] dispatching %s: persona=%s playbook=%s", kind, persona_id,
        playbook_name,
    )
    try:
        pulse_controller.submit_meta_judgment(
            persona_id=persona_id,
            building_id=building_id,
            meta_playbook=playbook_name,
            args=args,
            event_callback=_capture_event,
        )
    except Exception as exc:
        LOGGER.warning(
            "[judgment] %s Playbook raised: persona=%s error=%r",
            kind, persona_id, exc,
        )
        return {"kind": kind, "playbook": playbook_name, "args": args,
                "submitted": False, "reason": f"runtime exception: {exc!r}",
                "errors": errors}

    if errors:
        for err in errors:
            LOGGER.warning(
                "[judgment] %s Playbook emitted error: persona=%s error=%s",
                kind, persona_id, err,
            )
    return {"kind": kind, "playbook": playbook_name, "args": args,
            "submitted": True, "errors": errors}


# ---------------------------------------------------------------------------
# finalize 用の検証ヘルパ (builtin_data/tools/judgment_finalize.py が使う)
# ---------------------------------------------------------------------------


def sanitize_timetable(
    manager: Any, persona_id: str, raw_slots: Any
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """LLM が返した timetable を検証し、save_day_plan 形式へ正規化する。

    不正な項目は **該当コマだけ棄却** して警告に積む (判断全体を落とさない。
    握り潰さない — 呼び出し側が WARN ログに流す):

    - dict でない / start が HH:MM でない / kind が未知 → コマ棄却
    - ref が実在しない (task:N / desire:N が解決不能) → コマ棄却
    - 暮らし/休む に ref が付いている → ref='none' に矯正 (コマは残す)
    - facility が実在しない → 'own_room' に矯正 (コマは残す)
    - 時刻の重複 → 後のコマを棄却 (ソート後に判定)

    Returns:
        (正規化済みコマ配列 [start 昇順], 警告メッセージのリスト)
    """
    warnings: List[str] = []
    if not isinstance(raw_slots, list):
        if raw_slots is not None:
            warnings.append(f"timetable is not a list (got {type(raw_slots).__name__})")
        return [], warnings

    ptm = PersonaTaskManager(manager.SessionLocal)
    facilities = set(collect_facility_ids(manager))
    cleaned: List[Dict[str, Any]] = []
    for i, slot in enumerate(raw_slots):
        if not isinstance(slot, dict):
            warnings.append(f"slot[{i}] rejected: not a dict")
            continue
        start = slot.get("start")
        if not isinstance(start, str) or not _TIME_RE.match(start):
            warnings.append(f"slot[{i}] rejected: start={start!r} is not 'HH:MM'")
            continue
        kind = slot.get("kind")
        if kind not in ALL_KINDS:
            warnings.append(f"slot[{i}] rejected: unknown kind={kind!r}")
            continue

        ref = str(slot.get("ref") or REF_NONE).strip() or REF_NONE
        if kind in (KIND_LIVING, KIND_REST):
            if ref != REF_NONE:
                warnings.append(
                    f"slot[{i}]: kind={kind!r} には ref を付けられません; ref='none' に矯正"
                )
                ref = REF_NONE
        elif ref != REF_NONE:
            if not _REF_RE.match(ref):
                warnings.append(f"slot[{i}] rejected: invalid ref format {ref!r}")
                continue
            try:
                ptm.resolve_task_ref(persona_id, normalize_task_ref(ref))
            except TaskNotFoundError:
                warnings.append(f"slot[{i}] rejected: ref {ref!r} does not exist")
                continue

        facility = str(slot.get("facility") or "").strip()
        if facility not in facilities:
            warnings.append(
                f"slot[{i}]: facility={facility!r} は実在しません; 'own_room' に矯正"
            )
            facility = FACILITY_OWN_ROOM

        budget = slot.get("budget_rounds", 0)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0:
            warnings.append(
                f"slot[{i}]: budget_rounds={budget!r} は非負整数でないため 0 に矯正"
            )
            budget = 0
        budget = int(budget)

        note = slot.get("note")
        note = note if isinstance(note, str) else ""

        cleaned.append({
            "start": start,
            "kind": kind,
            "ref": ref,
            "facility": facility,
            "budget_rounds": budget,
            "note": note,
        })

    # start 昇順に整列し、重複時刻は後のコマを棄却する (save_day_plan の厳密昇順要件)。
    cleaned.sort(key=lambda s: s["start"])
    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for slot in cleaned:
        if slot["start"] in seen:
            warnings.append(
                f"slot start={slot['start']} が重複しています; 後のコマを棄却"
            )
            continue
        seen.add(slot["start"])
        deduped.append(slot)
    return deduped, warnings


def save_desk_memo(
    manager: Any, track_id: str, memo: Dict[str, Any]
) -> bool:
    """Track metadata に机メモを保存する (judgment_points.md §6 の continue/blocked)。

    ``track_metadata.desk_memo = {text, status, task_ref, updated_at}`` を上書きする。
    次の起床判断・セッション再開が「どこまでやった・次はどこから」を読む置き場。

    Returns:
        保存できたら True。Track が見つからない等は False (呼び出し側で WARN)。
    """
    from database.models import ActionTrack

    db = manager.SessionLocal()
    try:
        track = (
            db.query(ActionTrack).filter(ActionTrack.track_id == track_id).first()
        )
        if track is None:
            return False
        try:
            metadata = json.loads(track.track_metadata) if track.track_metadata else {}
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["desk_memo"] = dict(memo)
        track.track_metadata = json.dumps(metadata, ensure_ascii=False)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
