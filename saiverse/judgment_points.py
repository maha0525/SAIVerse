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

判断点は 5 種 (judgment_points.md §2 の一覧):

- ``day_open``   — 起床判断: 時間割の編成 + 予算配分 (+ 欲求→関心の昇格)
- ``post_conversation`` — 会話終了判断: 会話からの収穫 (タスク・欲求) +
  中断中セッションの扱い + 残り時間割の整え
- ``post_session`` — セッション終了判断: タスクの裁定 (接地検証つき) + 次への接続
- ``on_event``   — イベント到着判断: 反応の選択 (engage_now / insert_slot /
  note_only / ignore。alert は engage_now のみに縮退)
- ``day_close``  — 就寝判断: 予定 vs 実績のふりかえり + 明日の自分へのメモ +
  欲求のたな卸し + ユーザーへの報告種

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
    to_desire_ref,
)
from saiverse.note_manager import NOTE_TYPE_DESIRE, NoteManager
from saiverse.persona_task_manager import (
    PARENT_NOTE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

KIND_DAY_OPEN = "day_open"
KIND_POST_CONVERSATION = "post_conversation"
KIND_POST_SESSION = "post_session"
KIND_ON_EVENT = "on_event"
KIND_DAY_CLOSE = "day_close"

#: 判断点 kind → Playbook 名 (builtin_data/playbooks/public/)。
JUDGMENT_PLAYBOOK_MAP: Dict[str, str] = {
    KIND_DAY_OPEN: "judgment_day_open",
    KIND_POST_CONVERSATION: "judgment_post_conversation",
    KIND_POST_SESSION: "judgment_post_session",
    KIND_ON_EVENT: "judgment_on_event",
    KIND_DAY_CLOSE: "judgment_day_close",
}

# 会話終了判断 resume_session の選択肢 (judgment_points.md §5)
RESUME_NOW = "resume_now"
RESUME_DEFER = "defer_to_slot"
RESUME_DROP = "drop"
RESUME_CHOICES = (RESUME_NOW, RESUME_DEFER, RESUME_DROP)

# イベント到着判断 reaction の種別 (judgment_points.md §7)
REACTION_ENGAGE_NOW = "engage_now"
REACTION_INSERT_SLOT = "insert_slot"
REACTION_NOTE_ONLY = "note_only"
REACTION_IGNORE = "ignore"

#: 「中断中セッション」と見なす desk_memo.status (post_session 判断が刻む)
DESK_MEMO_INTERRUPTED_STATUSES = ("continue", "blocked")

#: picked_tasks.track_ref の enum に載せる Track status
#: (judgment_points.md §5「active/pending Track の動的注入」。alert は
#: 「要即応の active」なので含める。unstarted / 終了状態は含めない。
#: 値は saiverse.track_manager の STATUS_RUNNING / STATUS_ALERT / STATUS_PENDING)
PICKABLE_TRACK_STATUSES = ("running", "alert", "pending")

#: 日次予算 (ラウンド) の既定値。予算ゲート (v2 §4.5) が乗るまでの素朴な形
#: (セッション数 × ラウンド上限 ≒ 5 × 8)。context["daily_budget_rounds"] で上書き可。
DEFAULT_DAILY_BUDGET_ROUNDS = 40

#: バックログとして提示するタスクの status (生きているもの)
BACKLOG_TASK_STATUSES = ("pending", "active", "paused")

#: 終了済み (裁定・時間割の参照対象にならない) タスクの status。
#: enum 構築 (collect_slot_ref_enum) は生存 status の positive フィルタで
#: 元から completed を除外しているが、「enum 構築後に完了した task を指す
#: 古い ref」がスキーマ・時間割へ滑り込む経路を塞ぐための negative フィルタ
#: (2026-07-05 実 LLM シム 3回目 異常③: completed 済み task:1 への再セッション
#: → 再 done 裁定 → artifact_refs 多重追記)。
TERMINAL_TASK_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED)

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REF_RE = re.compile(r"^(task|desire):(\d+)$")


def normalize_task_ref(ref: str) -> str:
    """``desire:N`` を ``task:N`` へ正規化する (同じ short_id 参照空間。day_plan 参照)。"""
    ref = (ref or "").strip()
    if ref.startswith("desire:"):
        return "task:" + ref[len("desire:"):]
    return ref


def _task_ref_status(manager: Any, persona_id: str, ref: str) -> Optional[str]:
    """ref (task:N / desire:N) が指すタスクの status を返す。解決不能は None。"""
    try:
        ptm = PersonaTaskManager(manager.SessionLocal)
        task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(str(ref)))
        task = ptm.get_task(task_id, persona_id=persona_id)
    except TaskNotFoundError:
        return None
    except Exception:
        LOGGER.warning(
            "[judgment] failed to read status for ref %r (persona=%s)",
            ref, persona_id, exc_info=True,
        )
        return None
    return task.get("status") if isinstance(task, dict) else None


# ---------------------------------------------------------------------------
# 動的 enum の収集
# ---------------------------------------------------------------------------


def _facility_candidate_buildings(manager: Any) -> List[Any]:
    """facility enum / 状況テキストの施設一覧に載せる Building 群。

    公共施設タグ (FACILITY_ROLES) 付き Building が 1 つでもあればそれのみ、
    ゼロなら後方互換で全 Building (まだ誰もタグ付けしていない DB で従来挙動を
    壊さない — v2 §6.1 / facility_map.py)。
    """
    from saiverse.facility_map import list_tagged_buildings

    tagged = list_tagged_buildings(manager)
    if tagged:
        return tagged
    return list(getattr(manager, "buildings", None) or [])


def collect_facility_ids(manager: Any) -> List[str]:
    """コマの facility enum: 公共施設タグ付き Building + "own_room" (v2 §6.1)。

    タグ付き Building がゼロの DB では全 Building を提示する (後方互換)。
    """
    out: List[str] = []
    for b in _facility_candidate_buildings(manager):
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


def collect_pickable_track_refs(manager: Any, persona_id: str) -> List[str]:
    """picked_tasks.track_ref enum: 実在の active/pending Track (t:N 形式)。

    judgment_points.md §5。short_id 未採番の行は参照子が無いため載せない。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return []
    try:
        tracks = track_manager.list_for_persona(
            persona_id, statuses=PICKABLE_TRACK_STATUSES,
        )
    except Exception:
        LOGGER.warning(
            "[judgment] failed to list pickable tracks for %s", persona_id,
            exc_info=True,
        )
        return []
    return [f"t:{t.short_id}" for t in tracks if t.short_id is not None]


def find_interrupted_session(manager: Any, persona_id: str) -> Optional[Dict[str, Any]]:
    """「中断中セッション」= desk_memo (status: continue/blocked) を持つ生きた Track。

    post_session 判断が :func:`save_desk_memo` で凍結した作業メモが実体
    (judgment_points.md §5「中断中セッションの作業メモ (あれば)」)。複数あれば
    updated_at が最新の 1 件を返す (resume_session は単一選択のため)。

    Returns:
        ``{"track_id", "track_ref", "track_title", "task_ref", "text",
        "status", "updated_at"}`` または None。
    """
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return None
    from saiverse.track_manager import LIVE_STATUSES

    try:
        tracks = track_manager.list_for_persona(persona_id, statuses=LIVE_STATUSES)
    except Exception:
        LOGGER.warning(
            "[judgment] failed to scan desk memos for %s", persona_id, exc_info=True,
        )
        return None

    best: Optional[Dict[str, Any]] = None
    for t in tracks:
        raw = getattr(t, "track_metadata", None)
        if not raw:
            continue
        try:
            metadata = json.loads(raw)
        except (TypeError, ValueError):
            continue
        memo = metadata.get("desk_memo") if isinstance(metadata, dict) else None
        if not isinstance(memo, dict):
            continue
        if memo.get("status") not in DESK_MEMO_INTERRUPTED_STATUSES:
            continue
        candidate = {
            "track_id": t.track_id,
            "track_ref": f"t:{t.short_id}" if t.short_id is not None else t.track_id[:8],
            "track_title": t.title or "(無題)",
            "task_ref": str(memo.get("task_ref") or ""),
            "text": str(memo.get("text") or ""),
            "status": str(memo.get("status") or ""),
            "updated_at": str(memo.get("updated_at") or ""),
        }
        if best is None or candidate["updated_at"] > best["updated_at"]:
            best = candidate
    return best


def collect_today_touched_desires(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """今日生まれた・触れた欲求 (生きている候補のみ) の dict リスト。

    就寝判断 ``desire_reviews`` の動的 enum 供給元 (judgment_points.md §8
    「今日触れた欲求のみ動的注入」)。「今日」の判定は ``last_touched_at``
    または ``created_at`` の暦日が ``clock.now()`` の日付に一致すること。
    """
    today = clock.now().date().isoformat()
    out: List[Dict[str, Any]] = []
    for task in _list_desire_tasks(manager, persona_id):
        touched = str(task.get("last_touched_at") or "")
        created = str(task.get("created_at") or "")
        if touched.startswith(today) or created.startswith(today):
            out.append(task)
    return out


def collect_today_touched_desire_refs(manager: Any, persona_id: str) -> List[str]:
    """collect_today_touched_desires の ref のみ (desire:N 形式)。"""
    refs: List[str] = []
    for task in collect_today_touched_desires(manager, persona_id):
        ref = task.get("task_ref") or ""
        if ref.startswith("task:"):
            refs.append("desire:" + ref[len("task:"):])
    return refs


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
            "title": {
                "type": "string",
                "description": "このコマの表題。「○○をする」という形の短い一文 (一日の予定表にそのまま載る)",
            },
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
        "required": ["start", "kind", "title", "ref", "facility", "note"],
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

    対象タスクが既に終了済み (completed / cancelled) の場合は **task_verdict
    欄自体を出さない** — 再 done 裁定 (artifact_refs 多重追記) も、終了済み
    タスクへの desk_memo (偽の「中断中の作業」) も構造的に不可能にする
    (状況テキストには [completed] と正直に出る。2026-07-05 実 LLM シム 異常③)。
    """
    slot = _build_slot_schema(
        collect_slot_ref_enum(manager, persona_id),
        collect_facility_ids(manager),
    )
    props: Dict[str, Any] = {"monologue": {"type": "string"}}
    required = ["monologue"]

    if task_ref and _task_ref_status(manager, persona_id, task_ref) in TERMINAL_TASK_STATUSES:
        LOGGER.info(
            "[judgment] post_session target %s is already terminal; "
            "omitting task_verdict from schema (persona=%s)",
            task_ref, persona_id,
        )
        task_ref = None

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


def build_post_conversation_schema(
    manager: Any,
    persona_id: str,
    track_refs: List[str],
    has_interrupted_session: bool,
) -> Dict[str, Any]:
    """会話終了判断の response_schema (judgment_points.md §5)。

    - picked_tasks.track_ref は実在の active/pending Track (t:N) + "new" の動的 enum
    - resume_session は **中断中セッションがあるときだけ** フィールドを挿入する
      (無いのに要求しない — v1 の空 enum 事故の教訓)
    """
    slot = _build_slot_schema(
        collect_slot_ref_enum(manager, persona_id),
        collect_facility_ids(manager),
    )
    props: Dict[str, Any] = {
        "monologue": {"type": "string"},
        "picked_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "track_ref": {
                        "type": "string",
                        "enum": list(track_refs) + ["new"],
                        "description": (
                            "このタスクが属する関心 (実在の Track)。"
                            "新しい関心として立てるなら 'new'"
                        ),
                    },
                    "origin_quote": {
                        "type": "string",
                        "description": "根拠となる会話中の発言の引用",
                    },
                },
                "required": ["title", "track_ref", "origin_quote"],
            },
        },
        "new_desires": _new_desires_schema(),
        "remaining_timetable": {
            "anyOf": [
                {"type": "array", "items": slot},
                {"type": "null"},
            ],
        },
    }
    if has_interrupted_session:
        props["resume_session"] = {
            "type": "string",
            "enum": list(RESUME_CHOICES),
            "description": (
                "中断中の作業をどうするか: resume_now (今すぐ再開) / "
                "defer_to_slot (残りの時間割の中で再開) / drop (取りやめる)"
            ),
        }
    return {
        "type": "object",
        "properties": props,
        "required": ["monologue", "picked_tasks", "new_desires", "remaining_timetable"],
    }


def build_on_event_schema(
    manager: Any, persona_id: str, is_alert: bool
) -> Dict[str, Any]:
    """イベント到着判断の response_schema (judgment_points.md §7)。

    reaction は anyOf 4 分岐 (engage_now / insert_slot / note_only / ignore)。
    **alert イベントでは anyOf を engage_now のみに動的縮退**させる
    (v1 状況 B の「強制」の継承)。
    """
    engage_now = {
        "type": "object",
        "properties": {"type": {"type": "string", "const": REACTION_ENGAGE_NOW}},
        "required": ["type"],
    }
    variants: List[Dict[str, Any]] = [engage_now]
    if not is_alert:
        slot = _build_slot_schema(
            collect_slot_ref_enum(manager, persona_id),
            collect_facility_ids(manager),
        )
        variants.append({
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": REACTION_INSERT_SLOT},
                "slot": slot,
            },
            "required": ["type", "slot"],
        })
        variants.append({
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": REACTION_NOTE_ONLY},
                "memo": {"type": "string"},
            },
            "required": ["type", "memo"],
        })
        variants.append({
            "type": "object",
            "properties": {"type": {"type": "string", "const": REACTION_IGNORE}},
            "required": ["type"],
        })
    return {
        "type": "object",
        "properties": {
            "monologue": {"type": "string"},
            "reaction": {"anyOf": variants},
            "new_desires": _new_desires_schema(),
        },
        "required": ["monologue", "reaction"],
    }


def build_day_close_schema(
    manager: Any, persona_id: str, touched_desire_refs: List[str]
) -> Dict[str, Any]:
    """就寝判断の response_schema (judgment_points.md §8)。

    desire_reviews の enum は **今日触れた欲求のみ**。空なら
    フィールド自体を出さない (空 enum 事故防止)。
    """
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "monologue": {
                "type": "string",
                "description": "一日のふりかえり。予定と実際のズレに触れる",
            },
            "tomorrow_memo": {
                "type": "string",
                "description": "明日の自分へのメモ",
            },
            "day_theme": {
                "type": "string",
                "description": "今日という一日を一言で表すなら (任意)",
            },
            "user_report_seeds": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "string",
                    "description": (
                        "帰還したユーザーに自分から話したいこと。"
                        "今日実際に起きたことに限る"
                    ),
                },
            },
        },
        "required": ["monologue", "tomorrow_memo"],
    }
    if touched_desire_refs:
        schema["properties"]["desire_reviews"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "desire_ref": {
                        "type": "string",
                        "enum": list(touched_desire_refs),
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["keep", "fading", "fulfilled"],
                    },
                },
                "required": ["desire_ref", "verdict"],
            },
        }
    return schema


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
    """施設一覧 (facility enum と同じ候補集合。ロールタグがあれば併記)。"""
    from saiverse.facility_map import ROLE_LABELS, building_roles

    lines = ["行ける場所:"]
    for b in _facility_candidate_buildings(manager):
        bid = getattr(b, "building_id", None)
        if not bid:
            continue
        name = getattr(b, "name", "") or bid
        roles = building_roles(b)
        label = "・".join(ROLE_LABELS.get(r, r) for r in roles)
        lines.append(f"- {bid}: {name}" + (f"（{label}）" if label else ""))
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
        title = (s.get("title") or "").strip()
        lines.append(
            f"- {s.get('start')} {s.get('kind')}"
            + (f"「{title}」" if title else "")
            + f" ref={s.get('ref')}"
            + f" @{s.get('facility')} 予算{s.get('budget_rounds', 0)} {s.get('note') or ''}".rstrip()
        )
    return "\n".join(lines)


def _yesterday_review_text(manager: Any, persona_id: str, yesterday: str) -> str:
    """昨日のふりかえり素材。就寝判断 (day_close) の finalize が meta_json に書く
    day_digest (実績の決定論要約) を優先し、無ければ昨日の時間割の予定 vs 実績を
    要約する。"""
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

    # 残高 (v2 §4.5): 起床判断のやり直し等で今日すでに消費済みなら明示する
    from saiverse.day_plan import get_budget_state

    budget_lines = [
        f"作業ラウンドの日次予算: {budget} (全コマの budget_rounds 合計の目安)",
    ]
    budget_state = get_budget_state(manager, persona_id, today)
    if budget_state and budget_state["used"] > 0:
        budget_lines.append(
            f"今日すでに消費済み: {budget_state['used']} ラウンド"
            f" (残り {max(0, int(budget) - budget_state['used'])})"
        )

    parts = [
        "[起床判断]",
        f"おはようございます。今日 ({today}) の一日が始まります。",
        "昨日の自分からのメモ・昨日のふりかえり・バックログ・やりたいこと候補を"
        "見て、今日の時間割を編成してください。",
        "各コマには「○○をする」という短い表題 (title) を付けてください — "
        "あなたの一日の予定表にそのまま載ります。",
        "",
        "[昨日の自分からのメモ]",
        memo or "(メモはありません)",
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
        *budget_lines,
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


def _format_pickable_tracks(manager: Any, persona_id: str) -> str:
    """picked_tasks.track_ref の選択材料 (t:N がどの関心かを示す一覧)。"""
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is None:
        return "進行中の関心 (Track) はありません。"
    try:
        tracks = track_manager.list_for_persona(
            persona_id, statuses=PICKABLE_TRACK_STATUSES,
        )
    except Exception:
        LOGGER.warning(
            "[judgment] failed to list tracks for %s", persona_id, exc_info=True,
        )
        return "(Track 情報は取得できませんでした)"
    live = [t for t in tracks if t.short_id is not None]
    if not live:
        return "進行中の関心 (Track) はありません。"
    lines = ["進行中の関心 (Track):"]
    for t in live:
        lines.append(f"- t:{t.short_id} [{t.track_type}/{t.status}] {t.title or '(無題)'}")
    return "\n".join(lines)


def build_post_conversation_situation_text(
    manager: Any,
    persona_id: str,
    context: Dict[str, Any],
    interrupted: Optional[Dict[str, Any]] = None,
) -> str:
    """会話終了判断の tail 注入テキスト (judgment_points.md §5「見るもの」)。

    会話本文は載せない — この判断は会話と同じ main line 文脈で走るため、
    会話はコンテキストに既に在る (メタ判断と同じ起動経路)。
    """
    now = clock.now()
    today = now.date().isoformat()
    parts = [
        "[会話終了判断]",
        "会話がひと区切りつきました。この会話から拾うべきこと"
        "（約束・頼まれごと・やりたくなったこと）と、残りの時間の使い方を"
        "決めてください。会話の内容はこの文脈にあります。",
        "",
        f"現在時刻: {now.strftime('%H:%M')}",
        _format_remaining_timetable(manager, persona_id, today),
    ]
    if interrupted:
        memo_label = "詰まり" if interrupted.get("status") == "blocked" else "続き"
        parts += [
            "",
            "[中断中の作業]",
            f"{interrupted['track_ref']}「{interrupted['track_title']}」"
            f"の作業メモ [{memo_label}]: {interrupted['text'] or '(記載なし)'}",
            "この作業をどうするかを resume_session で選んでください。",
        ]
    parts += [
        "",
        "[すでにあるもの（重複して作らないでください）]",
        _format_pickable_tracks(manager, persona_id),
        "",
        _format_task_backlog(manager, persona_id),
        "",
        desire_summary_for_prompt(manager, persona_id),
    ]
    return "\n".join(parts)


def build_on_event_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """イベント到着判断の tail 注入テキスト (judgment_points.md §7「見るもの」)。"""
    now = clock.now()
    today = now.date().isoformat()
    event_text = str(context.get("event_text") or "").strip()
    is_alert = bool(context.get("is_alert"))

    # 現在の活動状態 (running Track から導出)
    activity = "手すきです（暮らし）。"
    track_manager = getattr(manager, "track_manager", None)
    if track_manager is not None:
        try:
            running = track_manager.get_running(persona_id)
        except Exception:
            LOGGER.warning(
                "[judgment] get_running failed for %s", persona_id, exc_info=True,
            )
            running = None
        if running is not None:
            if getattr(running, "track_type", None) == "user_conversation":
                activity = "ユーザーと会話中です。"
            else:
                activity = f"「{running.title or '(無題)'}」に取り組んでいます。"

    parts = [
        "[イベント到着判断]",
        "イベントが届きました。どう反応するかを決めてください。",
    ]
    if is_alert:
        parts.append("このイベントは即応が必要です（今すぐ応対してください）。")
    parts += [
        "",
        "[イベント内容]",
        event_text or "(内容なし)",
        "",
        "[現在の状態]",
        f"現在時刻: {now.strftime('%H:%M')}",
        f"いまの活動: {activity}",
        _format_remaining_timetable(manager, persona_id, today),
    ]
    return "\n".join(parts)


def _collect_today_session_digests(
    manager: Any, persona_id: str, plan_date: str, limit: int = 12
) -> List[str]:
    """今日の作業セッションのダイジェスト本文 (best-effort)。

    SAIMemory の committed ダイジェスト (``sea.work_session.DIGEST_TAG``) を
    adapter 経由で読む。adapter が read API を持たない / 読めない場合は
    空リスト (状況テキストは slots_json の実績だけで成立する)。
    """
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    fetch = getattr(adapter, "recent_persona_messages_by_count", None)
    if not callable(fetch):
        return []
    from sea.work_session import DIGEST_TAG

    try:
        payloads = fetch(limit, required_tags=[DIGEST_TAG])
    except Exception:
        LOGGER.warning(
            "[judgment] failed to fetch session digests for %s", persona_id,
            exc_info=True,
        )
        return []
    out: List[str] = []
    for payload in payloads:
        created = str(payload.get("created_at") or "")
        if created and not created.startswith(plan_date):
            continue
        content = str(payload.get("content") or "").strip()
        if content:
            out.append(content)
    return out


def build_day_results_text(manager: Any, persona_id: str, plan_date: str) -> str:
    """今日の予定 vs 実績の対照テキスト (judgment_points.md §8「見るもの」)。

    slots_json の status / note と予算 (計画値)、取得できれば work_session
    ダイジェスト群を含む。就寝判断の状況テキストと、finalize が meta_json に
    保存する ``day_digest`` (翌朝 day_open の「昨日のふりかえり」が読む) の
    両方がこれを使う — 決定論構築なので接地が保たれる。

    実績ラベルは :func:`saiverse.day_plan.slot_result_label` — skipped は
    システム都合 (実行手段未実装 / 予算切れ / 会話優先) を明示し、本人の
    「見送り」判断として提示しない (してもいない判断の理由をペルソナに
    捏造させないため。接地原則 v2 §3-1)。同様に、詳細な実行記録の無い done
    (暮らし/休む スタブ、record_level='presence_only') は「実行済み」でなく
    「時間を過ごした（詳細な記録なし）」— していない活動の内容をふりかえりで
    捏造させない (soft-confabulation 防止、2026-07-05)。
    """
    from saiverse.day_plan import slot_result_label

    slots = load_day_plan(manager, persona_id, plan_date)
    if not slots:
        return "今日の時間割はありませんでした。"
    lines = ["今日の時間割（予定 → 実績）:"]
    consumed = 0
    planned = 0
    for s in slots:
        status = str(s.get("status") or STATUS_PENDING)
        label = slot_result_label(s)
        budget = int(s.get("budget_rounds") or 0)
        planned += budget
        if status in ("fired", "done"):
            consumed += budget
        title = (s.get("title") or "").strip()
        line = (
            f"- {s.get('start')} {s.get('kind')}"
            + (f"「{title}」" if title else "")
            + (f" ref={s.get('ref')}" if s.get("ref") not in (None, REF_NONE) else "")
            + f" @{s.get('facility')} → {label}"
        )
        note = (s.get("note") or "").strip()
        if note:
            line += f"（{note}）"
        lines.append(line)
    lines.append(f"作業予算（計画値）: 消化 {consumed} / 計画 {planned} ラウンド")
    from saiverse.day_plan import get_budget_state

    budget_state = get_budget_state(manager, persona_id, plan_date)
    if budget_state is not None:
        lines.append(
            f"日次予算（実測）: {budget_state['used']} / {budget_state['total']} "
            f"ラウンド消費 (残り {budget_state['remaining']})"
        )
    digests = _collect_today_session_digests(manager, persona_id, plan_date)
    if digests:
        lines.append("")
        lines.append("今日の作業セッションのダイジェスト:")
        for d in digests:
            lines.append(f"- {d}")
    return "\n".join(lines)


def build_day_close_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """就寝判断の tail 注入テキスト (judgment_points.md §8「見るもの」)。"""
    now = clock.now()
    today = now.date().isoformat()
    parts = [
        "[就寝判断]",
        f"今日 ({today}) を終えます。予定と実際に起きたことを見比べて、"
        "ふりかえりと明日の自分へのメモを書いてください。",
        "",
        build_day_results_text(manager, persona_id, today),
        "",
        "[今日生まれた・触れた「やりたいこと」]",
    ]
    touched = collect_today_touched_desires(manager, persona_id)
    if touched:
        for task in touched:
            ref = to_desire_ref(task.get("task_ref"))
            dtype = task.get("desire_type") or "未分類"
            title = task.get("title") or "(無題)"
            count = task.get("touch_count") or 0
            parts.append(
                f"- {ref} [{dtype}] {title} (再訪: {count}回)"
            )
    else:
        parts.append("今日触れた「やりたいこと」はありません。")
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
    elif kind == KIND_POST_CONVERSATION:
        track_refs = collect_pickable_track_refs(manager, persona_id)
        interrupted = find_interrupted_session(manager, persona_id)
        situation_text = build_post_conversation_situation_text(
            manager, persona_id, context, interrupted=interrupted,
        )
        response_schema = build_post_conversation_schema(
            manager, persona_id, track_refs, interrupted is not None,
        )
        judgment_context = {
            "plan_date": today,
            "track_refs": track_refs,
        }
        if interrupted is not None:
            judgment_context["resume"] = {
                "track_id": interrupted["track_id"],
                "task_ref": interrupted["task_ref"],
                "text": interrupted["text"],
            }
    elif kind == KIND_ON_EVENT:
        event_text = str(context.get("event_text") or "").strip()
        if not event_text:
            raise ValueError(
                "on_event judgment requires context['event_text'] (non-empty)"
            )
        is_alert = bool(context.get("is_alert"))
        situation_text = build_on_event_situation_text(manager, persona_id, context)
        response_schema = build_on_event_schema(manager, persona_id, is_alert)
        judgment_context = {
            "plan_date": today,
            "is_alert": is_alert,
            # note_only の覚え書きに「何のイベントだったか」を添えるための抜粋
            "event_text": event_text[:200],
        }
    elif kind == KIND_DAY_CLOSE:
        touched_refs = collect_today_touched_desire_refs(manager, persona_id)
        situation_text = build_day_close_situation_text(manager, persona_id, context)
        response_schema = build_day_close_schema(manager, persona_id, touched_refs)
        judgment_context = {
            "plan_date": today,
            "touched_desire_refs": touched_refs,
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
            - post_conversation: なし (中断中セッション・Track・タスク・欲求は
              本モジュールが DB から収集する)。会話本文は main line 文脈に
              既に在る前提 (メタ判断と同じ起動経路)
            - post_session: ``session_result`` (WorkSessionResult または dict、必須) /
              ``task_ref`` / ``track_id`` / ``budget_rounds`` (いずれも省略時は
              session_result から読む)
            - on_event: ``event_text`` (必須) / ``is_alert`` (省略時 False。
              True なら reaction スキーマが engage_now のみに縮退)。
              **ユーザー会話中は原則発火させないこと** — 会話の至上性
              (judgment_points.md §7)。その抑止は呼び出し側の責務であり、
              本モジュールは判定しない (会話中の収穫は会話終了判断が担う)
            - day_close: なし (予定 vs 実績・今日触れた欲求は本モジュールが
              DB から収集する)

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
                task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(ref))
                task = ptm.get_task(task_id, persona_id=persona_id)
            except TaskNotFoundError:
                warnings.append(f"slot[{i}] rejected: ref {ref!r} does not exist")
                continue
            # 終了済みタスクを指すコマは棄却する。ref enum は生存タスクから
            # 構築されるが、判断の適用順 (task_verdict で完了 → 同じ判断の
            # remaining_timetable が旧 enum の ref を再提出) や旧 plan からの
            # 引き写しで、完了済み ref がここへ届きうる (シム 3回目 異常③)。
            status = (task or {}).get("status")
            if status in TERMINAL_TASK_STATUSES:
                warnings.append(
                    f"slot[{i}] rejected: ref {ref!r} は既に {status} のタスクです"
                )
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

        title = slot.get("title")
        title = title.strip() if isinstance(title, str) else ""

        note = slot.get("note")
        note = note if isinstance(note, str) else ""

        cleaned.append({
            "start": start,
            "kind": kind,
            "ref": ref,
            "facility": facility,
            "budget_rounds": budget,
            "title": title,
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


def insert_timetable_slot(
    manager: Any,
    persona_id: str,
    plan_date: str,
    slot: Dict[str, Any],
    not_before: Optional[str] = None,
) -> Tuple[Optional[int], List[str]]:
    """コマ 1 件を今日の残り時間割へ挿入する (on_event insert_slot / resume_now)。

    検証は :func:`sanitize_timetable` (単一コマ) + 時刻整合:

    - ``not_before`` (HH:MM) より前の start は棄却 (過去のコマは挿入できない。
      「今すぐ」は engage_now / resume_now が担う)
    - start が既存コマ (消化済み含む) と重複する場合は空きが見つかるまで
      1 分ずつ繰り下げる (上限 30 分。同時刻コマは day_plan の key 空間で
      衝突するため)
    - 適用は :func:`day_plan.replace_remaining_slots` (残りコマ + 挿入コマの
      全置換)。時刻昇順の検証に失敗した場合は時間割を一切変更しない

    Returns:
        (置換後に push したコマ数 | 失敗時 None, 警告メッセージのリスト)
    """
    cleaned, warnings = sanitize_timetable(manager, persona_id, [slot])
    if not cleaned:
        return None, warnings
    new_slot = cleaned[0]

    if not_before and new_slot["start"] < not_before:
        warnings.append(
            f"挿入コマ rejected: start={new_slot['start']} は現在時刻 "
            f"{not_before} より前です"
        )
        return None, warnings

    current = load_day_plan(manager, persona_id, plan_date) or []
    remaining = [
        s for s in current if s.get("status") in (STATUS_PENDING, STATUS_DEFERRED)
    ]
    taken = {s.get("start") for s in current}
    start = new_slot["start"]
    for _ in range(30):
        if start not in taken:
            break
        minutes = int(start[:2]) * 60 + int(start[3:]) + 1
        if minutes >= 24 * 60:
            warnings.append(
                f"挿入コマ rejected: start={new_slot['start']} 以降に空き時刻が"
                "ありません (日を跨ぐ挿入は不可)"
            )
            return None, warnings
        start = f"{minutes // 60:02d}:{minutes % 60:02d}"
    else:
        warnings.append(
            f"挿入コマ rejected: start={new_slot['start']} 周辺 30 分に空き時刻が"
            "ありません"
        )
        return None, warnings
    if start != new_slot["start"]:
        warnings.append(
            f"挿入コマ: start={new_slot['start']} は使用済みのため {start} へ繰り下げ"
        )
        new_slot["start"] = start

    merged = sorted(remaining + [new_slot], key=lambda s: s["start"])
    from saiverse.day_plan import replace_remaining_slots

    try:
        pushed = replace_remaining_slots(manager, persona_id, plan_date, merged)
    except ValueError as exc:
        warnings.append(f"コマの挿入に失敗 (時間割は不変): {exc}")
        return None, warnings
    return pushed, warnings


def save_desk_memo(
    manager: Any, track_id: str, memo: Dict[str, Any]
) -> bool:
    """Track metadata に作業メモを保存する (judgment_points.md §6 の continue/blocked)。

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


def clear_desk_memo(manager: Any, track_id: str) -> bool:
    """Track metadata の作業メモを片づける (resume_session='drop' の適用)。

    ``track_metadata.desk_memo`` を除去する。以後この Track は
    :func:`find_interrupted_session` の対象から外れる (タスク自体は残る)。

    Returns:
        除去できたら True。Track が無い / 作業メモが無い場合は False。
    """
    from database.models import ActionTrack

    db = manager.SessionLocal()
    try:
        track = (
            db.query(ActionTrack).filter(ActionTrack.track_id == track_id).first()
        )
        if track is None or not track.track_metadata:
            return False
        try:
            metadata = json.loads(track.track_metadata)
        except (TypeError, ValueError):
            return False
        if not isinstance(metadata, dict) or "desk_memo" not in metadata:
            return False
        metadata.pop("desk_memo", None)
        track.track_metadata = json.dumps(metadata, ensure_ascii=False)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
