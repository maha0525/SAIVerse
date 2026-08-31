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

判断点は 4 種 (judgment_points.md §2 の一覧):

- ``day_open``   — 起床判断: 時間割の編成 + 予算配分
- ``post_session`` — セッション終了判断: タスクの裁定 (接地検証つき) + 次への接続
- ``on_event``   — イベント到着判断: 反応の選択 (engage_now / insert_slot /
  note_only / ignore。alert は engage_now のみに縮退)
- ``day_close``  — 就寝判断: 予定 vs 実績のふりかえり + 明日の自分へのメモ +
  ユーザーへの報告種

会話終了判断 (``post_conversation``) は 2026-08-16 の裁定で退役した
(autonomous_behavior_v3.md §8 / §13.3): 会話に切れ目は定義できず、本人の声の
捕獲はスルースの一手へ一本化された。会話の待ち閉じは機械の帳簿処理だけが残る
(``autonomy_wiring.handle_conversation_end``)。

モデルは standard (META 相当): 起動は ``PulseController.submit_meta_judgment``
(= ``pulse_type="meta_judgment"``) を使い、``sea.pulse_context.aspect_from_pulse_type``
が META アスペクト (line_role='meta_judgment' / scope='discardable' / standard tier)
を導出する — meta_judgment Playbook の起動経路と同一。

**自動起動の配線は本モジュールではしない** (中間起動の空打ち防止、
feedback_phased_implementation_intermediate_run)。本番の恒久配線は
``saiverse.autonomy_wiring`` (Active ゲート / Playbook 欠如スキップ / watchdog
込み) が担い、シム (``saiverse.day_scenario``) とテストは ``run_judgment_point``
を直接呼ぶ。

時刻はすべて ``saiverse.clock.now()`` を読む (v2 §12 の不変条件)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from saiverse import clock
from saiverse.day_plan import (
    FACILITY_OWN_ROOM,
    REF_NONE,
    STATUS_DEFERRED,
    STATUS_PENDING,
    all_kinds,
    day_order_minutes,
    get_lives,
    is_in_user_conversation,
    life_consumed,
    load_day_plan,
    load_plan_meta,
    worker_session_kinds,
)
from saiverse.persona_task_manager import (
    STAGE_CANDIDATE,
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
KIND_POST_SESSION = "post_session"
KIND_ON_EVENT = "on_event"
KIND_DAY_CLOSE = "day_close"

#: 判断点 kind → Playbook 名 (builtin_data/playbooks/public/)。
JUDGMENT_PLAYBOOK_MAP: Dict[str, str] = {
    KIND_DAY_OPEN: "judgment_day_open",
    KIND_POST_SESSION: "judgment_post_session",
    KIND_ON_EVENT: "judgment_on_event",
    KIND_DAY_CLOSE: "judgment_day_close",
}

# イベント到着判断 reaction の種別 (judgment_points.md §7)
REACTION_ENGAGE_NOW = "engage_now"
REACTION_INSERT_SLOT = "insert_slot"
REACTION_NOTE_ONLY = "note_only"
REACTION_IGNORE = "ignore"

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


def collect_facility_ids(manager: Any) -> List[str]:
    """コマの facility enum: 公共施設タグ付き Building + "own_room" (v2 §6.1)。

    タグ付き Building がゼロの DB では全 Building を提示する (後方互換)。

    候補集合の決定は :func:`saiverse.facility_map.candidate_buildings` に一本化
    してある — head の「行ける場所」(FacilitiesSection) が読む情報を出し、こちらが
    選べる選択肢を出すので、二つが同じ集合を見ないと「head に無い場所が選べる /
    head にあるのに選べない」が起きる。
    """
    from saiverse.facility_map import candidate_buildings

    out: List[str] = []
    for b in candidate_buildings(manager):
        bid = getattr(b, "building_id", None)
        if bid:
            out.append(bid)
    out.append(FACILITY_OWN_ROOM)
    return out


def list_backlog_tasks(manager: Any, persona_id: str) -> List[Dict[str, Any]]:
    """バックログタスク (欲求候補を除く生きているタスク) の dict リスト。

    P3c-0 以降、欲求候補は parent_kind でなく stage='candidate' で識別する
    (候補は常に親なしで生まれるため、parent_kind だけではもう区別できない)。

    公開関数なのは slot_close の帰属先一覧・経験の台帳の索引が同じ供給を使う
    ため。判断側の enum とだけ食い違う一覧をペルソナに出さない。
    """
    ptm = PersonaTaskManager(manager.SessionLocal)
    tasks = ptm.list_tasks(
        persona_id, statuses=BACKLOG_TASK_STATUSES, include_steps=False,
    )
    return [t for t in tasks if t.get("stage") != STAGE_CANDIDATE]


def collect_slot_ref_enum(manager: Any, persona_id: str) -> List[str]:
    """コマの ref enum: 実在の採用済みタスク (task:N) + "none"。

    Track 撤廃 (track_retirement.md §7.2 ④群) と欲求プールの退役
    (autonomous_behavior_v3.md §8) で、track:N と欲求候補の供給は消えた。
    作業セッション系でないコマ (出かける/自室で過ごす/自由時間) は 'none'。
    """
    refs: List[str] = []
    for t in list_backlog_tasks(manager, persona_id):
        ref = t.get("task_ref")
        if ref:
            refs.append(ref)
    refs.append(REF_NONE)
    return refs


def collect_purpose_refs(manager: Any, persona_id: str) -> List[str]:
    """層2 棚入れの purpose enum: 実在の採用済みバックログタスク (task:N)。

    Track 撤廃 (track_retirement.md §7.2 ④群) で track:N の供給は消え、欲求候補
    (desire) は木の外なので元から棚に含めていない。残るのは task:N だけ。
    """
    refs: List[str] = []
    for t in list_backlog_tasks(manager, persona_id):
        ref = t.get("task_ref")
        if ref:
            refs.append(ref)
    return refs


def collect_today_closed_episodes(
    manager: Any, persona_id: str, plan_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """今日閉じた出来事の dict リスト (episode_ref を持つもののみ)。

    就寝判断 (day_close) の層2 棚入れ enum の供給元。「今日」は
    clock.now() の暦日 (0:00〜24:00、naive local — episodes の刻印と同系)。

    Args:
        plan_date: 収集対象日 "YYYY-MM-DD"。深夜跨ぎリズムでは就寝判断の
            営業日 (前日の暦日) を渡す。None のとき clock.now().date() を使う。
    """
    from datetime import date as _date

    from saiverse import episodes as episodes_mod

    now = clock.now()
    if plan_date is not None:
        d = _date.fromisoformat(plan_date)
    else:
        d = now.date()
    from datetime import datetime as _datetime
    day_start = int(_datetime(d.year, d.month, d.day, 0, 0, 0).timestamp())
    day_end = day_start + 86400
    try:
        eps = episodes_mod.list_today(manager, persona_id, day_start, day_end)
    except Exception:
        LOGGER.warning(
            "[judgment] failed to list today's episodes for %s", persona_id,
            exc_info=True,
        )
        return []
    return [
        e for e in eps
        if e.get("status") == episodes_mod.STATUS_CLOSED and e.get("episode_ref")
    ]


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
            "kind": {"type": "string", "enum": list(all_kinds())},
            "title": {
                "type": "string",
                "description": "このコマの表題。「○○をする」という形の短い一文 (一日の予定表にそのまま載る)",
            },
            "ref": {
                "type": "string",
                "enum": list(ref_enum),
                "description": (
                    "取り組む対象のタスク (task:N)。作業セッション系でないコマ"
                    " (出かける/自室で過ごす/自由時間) は 'none'"
                ),
            },
            "facility": {
                "type": "string",
                "enum": list(facility_enum),
                "description": "コマを過ごす場所 (Building ID または own_room)",
            },
            "budget_rounds": {
                "type": "integer",
                "description": "このコマの作業ラウンド予算 (作業セッション系のコマのみ。それ以外は 0)",
            },
            "note": {"type": "string", "description": "このコマで何をするかの短い覚え書き"},
        },
        "required": ["start", "kind", "title", "ref", "facility", "note"],
    }


def _episode_purposes_field(purpose_refs: List[str]) -> Dict[str, Any]:
    """層2 棚入れの共通フィールド (§9.1: 既存の判断コールに enum 1 フィールド追加)。

    対象の出来事は判断点の文脈から一意 (post_session=セッション) なので
    フィールドは purpose 参照の複数選択のみ。
    """
    return {
        "type": "array",
        "items": {"type": "string", "enum": list(purpose_refs)},
        "description": (
            "この出来事がどのタスクに係るか (複数可)。"
            "どれにも係らなければ空配列"
        ),
    }


def build_day_open_schema(manager: Any, persona_id: str) -> Dict[str, Any]:
    """起床判断の response_schema (judgment_points.md §4)。

    - timetable のコマ ref / facility は実在リストの動的 enum
    - ``promotions`` (欲求 → 関心の昇格) は欄ごと退役した — 欲求プールと Track の
      双方が供給源ごと消えたため (autonomous_behavior_v3.md §8)
    """
    slot = _build_slot_schema(
        collect_slot_ref_enum(manager, persona_id),
        collect_facility_ids(manager),
    )
    return {
        "type": "object",
        "properties": {
            "monologue": {"type": "string"},
            "timetable": {"type": "array", "minItems": 1, "items": slot},
        },
        "required": ["monologue", "timetable"],
    }


def build_post_session_schema(
    manager: Any,
    persona_id: str,
    artifacts: List[str],
    task_ref: Optional[str],
    episode_purpose_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """セッション終了判断の response_schema (judgment_points.md §6)。

    ``episode_purpose_refs`` が非空なら層2 棚入れの ``episode_purposes``
    フィールドを追加する (対象=このセッションの出来事。§9.1)。None / 空
    (出来事が特定できない・目的が無い) ならフィールド自体を出さない
    (空 enum 事故防止)。

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

    # digest 統合 (§6 改定 2026-07-18 / W1 Chunk C D9-4): post_session 自身が
    # セッションの記録 (原本) から実績要約を書く。required — digest 専用コールは
    # 廃止されており、この欄が唯一の生成点。
    props["digest"] = {
        "type": "string",
        "description": (
            "このセッションで実際に起きたことだけの短い要約"
            "（セッションの記録に基づく実績。確認できていない成果は書かない）"
        ),
    }
    required.append("digest")

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

    # ``track_op`` (Track の完了宣言) と ``new_desires`` (欲求候補の採取) は欄ごと
    # 退役した — Track 操作スペルと欲求プールが機構ごと消えたため
    # (track_retirement.md §7.2 ④群 / autonomous_behavior_v3.md §8)。
    if episode_purpose_refs:
        props["episode_purposes"] = _episode_purposes_field(episode_purpose_refs)
    props["remaining_timetable"] = {
        "anyOf": [
            {"type": "array", "items": slot},
            {"type": "null"},
        ],
    }
    required.append("remaining_timetable")

    return {"type": "object", "properties": props, "required": required}


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
        },
        "required": ["monologue", "reaction"],
    }


def build_day_close_schema(
    manager: Any,
    persona_id: str,
    episode_refs: Optional[List[str]] = None,
    purpose_refs: Optional[List[str]] = None,
    curation_candidates: Optional[List[Dict[str, Any]]] = None,
    naming_candidates: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """就寝判断の response_schema (judgment_points.md §8)。

    ``desire_reviews`` (欲求のたな卸し) は欄ごと退役した — 欲求プールが機構ごと
    消えたため (autonomous_behavior_v3.md §8)。

    層2 棚入れ (§9.1): day_close は対象の出来事が単一でない (今日閉じた
    出来事すべて) ため、``episode_purposes`` は {episode, purpose} ペアの
    配列になる。``episode_refs`` / ``purpose_refs`` のどちらかが空なら
    フィールド自体を出さない。

    P4-a 編纂候補 (``curation_candidates``): 候補が空 / None なら
    ``curation_reviews`` フィールド自体を出さない (空 enum 事故防止)。
    各 review: ``op_id`` (候補の op_id の enum) + ``verdict`` ("approve"|"skip")。

    P4-b 命名候補 (``naming_candidates``): 候補が空 / None なら
    ``naming_reviews`` フィールド自体を出さない (空 enum 事故防止)。
    各 review: ``cluster_id`` (候補の cluster_id の enum) +
    ``verdict`` ("name"|"skip") + ``name`` (verdict=name の場合必須)。
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
    if episode_refs and purpose_refs:
        schema["properties"]["episode_purposes"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "episode": {"type": "string", "enum": list(episode_refs)},
                    "purpose": {"type": "string", "enum": list(purpose_refs)},
                },
                "required": ["episode", "purpose"],
            },
            "description": (
                "今日の出来事のうち、どれがどの関心・タスクに係るか (任意・複数可)。"
                "係るものだけ挙げればよい"
            ),
        }
    # P4-a 編纂候補 — 候補が空なら空 enum 事故防止のためフィールド自体を出さない
    if curation_candidates:
        op_id_enum = [c["op_id"] for c in curation_candidates if c.get("op_id")]
        if op_id_enum:
            schema["properties"]["curation_reviews"] = {
                "type": "array",
                "description": (
                    "棚の乱れの裁定。approve すると眠っている間にバックグラウンドで整理されます。"
                    "skip は翌日以降に再提示されます"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "op_id": {
                            "type": "string",
                            "enum": op_id_enum,
                            "description": "裁定する編纂候補の ID",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["approve", "skip"],
                            "description": "approve=承認（実行する） / skip=見送り（翌日再提示）",
                        },
                    },
                    "required": ["op_id", "verdict"],
                },
            }
    # P4-b 命名候補 — 候補が空なら空 enum 事故防止のためフィールド自体を出さない
    if naming_candidates:
        cluster_id_enum = [
            c["cluster_id"] for c in naming_candidates if c.get("cluster_id")
        ]
        if cluster_id_enum:
            schema["properties"]["naming_reviews"] = {
                "type": "array",
                "description": (
                    "テーマの芽の裁定。name を与えるとテーマが棚に立ちます。"
                    "skip は翌日以降に再提示されます"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "cluster_id": {
                            "type": "string",
                            "enum": cluster_id_enum,
                            "description": "裁定する命名候補の ID",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["name", "skip"],
                            "description": (
                                "name=命名してテーマページを作成 / "
                                "skip=見送り（翌日再提示）"
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": "テーマに付ける名前（verdict=name のとき必須）",
                        },
                    },
                    "required": ["cluster_id", "verdict"],
                },
            }
    return schema


# ---------------------------------------------------------------------------
# 状況テキスト (tail 注入)
#
# 静的な一覧 (行ける場所) はここには無い。判断のたびに再送するものではないので
# head に移設した (まはー裁定 2026-07-29、
# docs/issues/judgment_static_lists_to_head.md): 一覧の本体は
# sea/head_pipeline/sections/facilities.py が head に常駐させ、
# 凍結中の増減は同 Section の差分通知が届ける。ここに残るのは「その判断の
# 瞬間にしか意味がない情報」だけ (現在時刻・残りの時間割・今日の予算・
# セッションの実績など)。
# ---------------------------------------------------------------------------


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


def build_day_open_situation_text(
    manager: Any, persona_id: str, context: Dict[str, Any]
) -> str:
    """起床判断の tail 注入テキスト (judgment_points.md §4「見るもの」)。

    v0.5 (life.md §9.3/§11.2): 今日の活動時間 (ライフ) はもうペルソナが宣言する
    ものではなく、この判断点が起動される前にシステムが確定済み
    (:func:`saiverse.day_plan.confirm_life_for_today`、呼び出し元は
    :func:`saiverse.autonomy_wiring.fire_judgment_point`)。ここでは確定済みの
    活動時間と現在時刻を**確定情報として明記**する — 実機初日は現在時刻を渡さず
    21 時に朝からの時間割を編成させてしまった (遅発 day_open の破綻)。

    昨日の生の実績表 ([昨日のふりかえり]) は**渡さない** (2026-07-29 撤去)。
    昨日の消化は就寝判断が済ませており、朝が受け取るのはその成果物 —
    tomorrow_memo と、窓に残る就寝の独白 — だけ。圧縮段の下流へ生材料を
    再供給しない (Chronicle のレベル制と同じ規律)。やり残しはタスク台帳が運ぶ。

    [進行中のことと、やりたいこと] と [施設一覧] も**渡さない** (2026-07-30 移設)。
    毎朝同じものを貼り直す情報なので head に常駐させた (PurposeBacklogSection /
    FacilitiesSection)。時間割が選べる ref / facility の enum は従来どおり
    live state から供給されるので、head が凍結して古くても実在しないものは
    選べない。
    """
    now = clock.now()
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    now_hhmm = now.strftime("%H:%M")

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

    lives_today = get_lives(manager, persona_id, today)
    if lives_today:
        life = lives_today[0]  # 初期実装は 1 日 1 窓 (life.md §4.3)
        remaining_pulses = max(
            0.0, int(life.get("budget_pulses") or 0) - life_consumed(life)
        )
        life_line = (
            f"今日のあなたの活動時間は {life['start']}〜{life['end']} です。"
            f"現在 {now_hhmm}。時間割は今この時刻から就寝までの範囲で"
            "編成してください (これより前の時刻のコマは選べません)。"
            f" 活動予算: 残り {remaining_pulses:.0f}/{int(life.get('budget_pulses') or 0)}。"
        )
    else:
        life_line = f"現在 {now_hhmm}。"

    # 習慣テンプレート (時間割改修 T2、timetable_redesign.md §5.1/§5.2):
    # テンプレートのあるペルソナの起床判断は「組む」でなく「埋める」— 雛形を
    # 穴の位置つきで提示し、指示を穴埋め + 例外調整の提案に変える。response_schema
    # は従来どおり timetable 全体 (枠の保証はプロンプトでなく finalize の
    # テンプレート整合強制が担う — saiverse/timetable_template.py)。
    template = None
    try:
        from saiverse.timetable_template import (
            get_active_template,
            render_template_situation_lines,
        )
        template = get_active_template(manager, persona_id, today)
    except Exception:
        LOGGER.warning(
            "[judgment] failed to load timetable template (persona=%s); "
            "falling back to free composition", persona_id, exc_info=True,
        )
        template = None

    if template:
        instruction_lines = [
            "今日の時間割には、あなたとユーザーとで決めた習慣テンプレート"
            " (下の雛形) があります。",
            "雛形のコマをそのまま timetable に写して出力し、＜空欄＞の項目"
            "だけを、昨日の自分からのメモと常に手元にある一覧を見て埋めて"
            "ください。",
            "確定済みの項目 (時刻・種別・場所など) は変更できません — 出力で"
            "変えても雛形の値に戻されます。枠を変えたい・例外の調整が必要だと"
            "感じたら、monologue に提案として書いてください (テンプレートの"
            "変更はユーザーとの相談で決まります)。",
            "各コマには「○○をする」という短い表題 (title) を付けてください — "
            "あなたの一日の予定表にそのまま載ります (表題が確定済みのコマは"
            "それを写します)。",
            "既に開始時刻を過ぎたコマは「流れた」として記録され、今の時刻"
            "以降のコマから今日が始まります。",
            "",
            "[今日の習慣テンプレート]",
            *render_template_situation_lines(template["slots"]),
        ]
    else:
        # テンプレ未設定 (または無効) のペルソナは従来どおりの全生成 —
        # 移行の安全弁 (intent §5.2 の fallback)。
        instruction_lines = [
            "昨日の自分からのメモと、常に手元にある一覧 (行ける場所) を見て、"
            "今日の時間割を編成してください。",
            "各コマには「○○をする」という短い表題 (title) を付けてください — "
            "あなたの一日の予定表にそのまま載ります。",
            "コマの ref には取り組むタスク (task:N) を指してください。",
        ]

    parts = [
        "[起床判断]",
        f"おはようございます。今日 ({today}) の一日が始まります。",
        life_line,
        *instruction_lines,
        "",
        "[昨日の自分からのメモ]",
        memo or "(メモはありません)",
        "",
        "[今日の予算]",
        *budget_lines,
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


def _resolve_session_episode_ref(context: Dict[str, Any]) -> Optional[str]:
    """post_session 文脈からセッションの episode_ref を読む (context 明示が優先)。"""
    episode_ref = context.get("episode_ref")
    if not episode_ref:
        episode_ref = _ws_get(context.get("session_result"), "episode_ref", None)
    return str(episode_ref) if episode_ref else None


def _render_session_transcript(
    manager: Any, persona_id: str, episode_ref: Optional[str]
) -> Optional[str]:
    """セッション原本 (origin_episode で引ける生ログ) の時系列レンダリング (D9-2)。

    発話者・時刻・内容 (スペル行と結果を含む) を素直な形式で並べる。
    **文字数上限は設けない** (まはー裁定 2026-07-18: セッションが走れた時点で
    サイズは実証済み — 原本 = セッションのモデルが実際にコンテキストに載せた
    内容)。adapter が読み口を持たない環境 (テストスタブ等) や原本 0 件は
    None を返し、呼び出し側が「取得できませんでした」を明示する。
    """
    if not episode_ref:
        return None
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    adapter = getattr(persona, "sai_memory", None) if persona is not None else None
    fetch = getattr(adapter, "get_messages_by_origin_episode", None)
    if not callable(fetch):
        return None
    try:
        rows = fetch(str(episode_ref))
    except Exception:
        LOGGER.warning(
            "[judgment] failed to fetch session transcript for %s (%s)",
            persona_id, episode_ref, exc_info=True,
        )
        return None
    if not rows:
        return None
    persona_name = str(
        getattr(persona, "persona_name", None) or persona_id
    )
    lines: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        role = str(row.get("role") or "")
        if role in ("assistant", "model"):
            speaker = persona_name
        elif role == "system":
            speaker = "システム"
        elif role == "user":
            speaker = "ユーザー"
        else:
            speaker = role or "?"
        created = row.get("created_at")
        stamp = ""
        if isinstance(created, int):
            try:
                stamp = datetime.fromtimestamp(created).strftime("%H:%M")
            except (OverflowError, OSError, ValueError):
                stamp = ""
        prefix = f"[{stamp}] " if stamp else ""
        lines.append(f"{prefix}{speaker}:\n{content}")
    if not lines:
        return None
    return "\n\n".join(lines)


def build_post_session_situation_text(
    manager: Any,
    persona_id: str,
    context: Dict[str, Any],
    shelving: bool = False,
    *,
    embed_transcript: bool = True,
) -> str:
    """セッション終了判断の tail 注入テキスト (judgment_points.md §6「見るもの」)。

    ``shelving=True`` のとき、層2 棚入れ (episode_purposes) を促す一文を添える。

    digest 統合 (§6 改定 2026-07-18 / W1 Chunk C D9): 旧「ダイジェスト:」欄は
    **セッションの記録 (原本)** に置き換わった。``embed_transcript=True``
    (LLM に渡る situation_text) では原本全文を埋め込み、``False`` (保存用の
    paired_situation_text — adapter の paired_action 展開で以後の Pulse 文脈に
    載り続けるため原本を含めない**コールローカル注入**) では episode 参照 +
    読み口 (/spell episode_read) の一行に留める。
    """
    now = clock.now()
    today = now.date().isoformat()
    sr = context.get("session_result")

    artifacts = list(_ws_get(sr, "artifacts", None) or [])
    rounds_used = _ws_get(sr, "rounds_used", 0) or 0
    ended_reason = _ws_get(sr, "ended_reason", "") or "?"
    budget_rounds = context.get("budget_rounds") or _ws_get(sr, "budget_rounds", None)
    episode_ref = _resolve_session_episode_ref(context)

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
    if embed_transcript:
        transcript = _render_session_transcript(manager, persona_id, episode_ref)
        record_lines = [
            "セッションの記録 (原本):",
            transcript or "(セッション原本を取得できませんでした)",
        ]
    else:
        if episode_ref:
            record_lines = [
                f"セッションの記録: {episode_ref} "
                "(原本は /spell episode_read で読めます)",
            ]
        else:
            record_lines = ["セッションの記録: (出来事参照なし)"]
    parts = [
        "[セッション終了判断]",
        "作業セッションが終わりました。実際に起きたことに基づいて、"
        "タスクの裁定と次への接続を決めてください。",
        "独白・裁定・メモで出典を挙げるときは、このセッションで実際に参照・"
        "取得した情報源に限ってください。このセッションで取得していない文献・"
        "サイト・ガイドライン名を根拠として書いてはいけません。",
        "digest 欄には、セッションの記録に基づき、このセッションで実際に"
        "起きたこと（スペルの実行結果で確認できたこと）だけを短く要約して"
        "ください。実行していない作業や、確認できていない成果を書いては"
        "いけません。成果物を作った場合は、その名前に触れてください。",
        "",
        "[セッションの実績]",
        f"対象タスク: {task_line}",
        f"使用ラウンド: {budget_text} (終了理由: {ended_reason})",
        *record_lines,
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
    if shelving:
        parts += [
            "",
            "このセッション (出来事) がどのタスクに係るかを "
            "episode_purposes で選んでください（複数可・どれにも係らなければ空）。",
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

    # 現在の活動状態。
    #
    # 「ユーザーと会話中か」の正典は**開いている会話の出来事 (Episode)** であって
    # running Track の種別ではない (life.md §7 案 Y, 2026-07-13)。案 Y 以降、対ユーザー
    # 会話 Track は会話が終わっても running のまま残る永続 Track なので、種別で判定すると
    # 何日も前に終わった会話について「ユーザーと会話中です」とペルソナへ渡してしまう
    # (2026-07-29 修正。同じ漏れの実害は起動時タイマー再確立の側で観測済み)。
    # 判定は day_plan.is_in_user_conversation に一本化する — 二つ目の実装を作らない。
    #
    # 会話以外の活動は、束 6c (2026-08-22) で供給源ごと消えた —— 出来事の書き手が
    # 全滅したので (v3 §7)、「いま何に取り組んでいるか」を答えられる器が v0.3 には
    # 無い。判定 (仲裁) と提示 (ここ) は同じ集合から引くという不変条件はそのまま
    # で、集合が空になった: 仲裁側の
    # ``user_conversation._get_open_non_conversation_episode`` も常に None を返す。
    # 器の作り直しは v0.4 のティック設計 (v3 §9-3)。
    activity = "手すきです。"
    if is_in_user_conversation(manager, persona_id):
        activity = "ユーザーと会話中です。"

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
        # strict_tags: 取得段階でタグ厳格一致にする。後段検査だけだと、
        # タグ無しの paired_action 展開行が limit 枠を占拠し、本物の
        # ダイジェストが取得から押し出される (2026-07-29 Codex 指摘 high1)。
        payloads = fetch(limit, required_tags=[DIGEST_TAG], strict_tags=True)
    except Exception:
        LOGGER.warning(
            "[judgment] failed to fetch session digests for %s", persona_id,
            exc_info=True,
        )
        return []
    out: List[str] = []
    for payload in payloads:
        # required_tags の絞り込みは「タグ無し行は素通し」の legacy 救済を持つ
        # (_payload_passes_context_filter)。paired_action 展開で生まれる判断
        # プロンプト行はタグ無しなのでそこを素通りし、「ダイジェスト」として
        # 就寝判断へ混入 → 保存 → 翌日また混入、と日を跨いで雪だるまになる
        # (2026-07-29 実害: 就寝判断 21,369 字)。ここで実タグを検査して閉じる。
        meta = payload.get("metadata")
        raw_tags = meta.get("tags") if isinstance(meta, dict) else None
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        if DIGEST_TAG not in tags:
            continue
        # 実 adapter の payload は created_at が epoch int (ISO 文字列は mock /
        # 旧形式)。epoch を日付文字列へ直してから plan_date と突き合わせる —
        # 従来は str(epoch) が plan_date と前方一致せず全件落ちていた
        # (W1 Chunk C で判明した既存欠陥の修正)。
        created_raw = payload.get("created_at")
        if isinstance(created_raw, (int, float)) and not isinstance(created_raw, bool):
            try:
                created = datetime.fromtimestamp(int(created_raw)).date().isoformat()
            except (OverflowError, OSError, ValueError):
                created = ""
        else:
            created = str(created_raw or "")
        if created and not created.startswith(plan_date):
            continue
        content = str(payload.get("content") or "").strip()
        if content:
            out.append(content)
    return out


def build_day_results_text(manager: Any, persona_id: str, plan_date: str) -> str:
    """今日の予定 vs 実績の対照テキスト (judgment_points.md §8「見るもの」)。

    slots_json の status / note と予算 (計画値)、取得できれば work_session
    ダイジェスト群を含む。就寝判断の状況テキストが使う — 決定論構築なので
    接地が保たれる。(旧 ``day_digest`` = 翌朝への保存コピーは 2026-07-29 撤去。
    朝は就寝判断の成果物だけを受け取る。)

    実績ラベルは :func:`saiverse.day_plan.slot_result_label` — skipped は
    システム都合 (実行手段未実装 / 予算切れ / 会話優先) を明示し、本人の
    「見送り」判断として提示しない (してもいない判断の理由をペルソナに
    捏造させないため。接地原則 v2 §3-1)。同様に、詳細な実行記録の無い done
    (presence スタブ、record_level='presence_only') は「実行済み」でなく
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
        from saiverse.day_plan import get_lives

        if get_lives(manager, persona_id, plan_date):
            # ライフ宣言がある日: 単位はラウンドでなく標準パルス換算
            # (life.md Phase2 §7、get_budget_state がライフ由来に切り替わる)。
            lines.append(
                f"日次予算（実測）: {budget_state['used']:.1f} / {budget_state['total']} "
                f"パルス消費 (残り {budget_state['remaining']:.1f})"
            )
        else:
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


def _format_today_episodes(episodes_today: List[Dict[str, Any]]) -> str:
    """今日閉じた出来事の一覧 (層2 棚入れの選択材料)。決定論構築・SELECT のみ。"""
    lines = ["[今日の出来事]"]
    for ep in episodes_today:
        ref = ep.get("episode_ref") or "episode:?"
        kind = ep.get("kind") or "?"
        meta = ep.get("meta") if isinstance(ep.get("meta"), dict) else {}
        title = str(meta.get("title") or "").strip()
        lines.append(f"- {ref} [{kind}]" + (f" {title}" if title else ""))
    lines.append(
        "これらの出来事がどのタスクに係るかを episode_purposes で"
        "選んでください（任意・複数可。係るものだけでよい）。"
    )
    return "\n".join(lines)


def _format_curation_candidates(candidates: List[Dict[str, Any]]) -> str:
    """編纂候補の状況テキスト節（就寝判断用）。

    ``detect_curation_candidates`` が返した候補リストを
    「## 今日の棚の乱れ（承認したものだけ、眠っている間に整理されます）」
    節として整形する。候補ゼロなら空文字を返す（節ごと出さない）。
    """
    if not candidates:
        return ""
    lines = [
        "## 今日の棚の乱れ（承認したものだけ、眠っている間に整理されます）",
    ]
    for c in candidates:
        lines.append(f"- {c['line']}")
    lines.append(
        "承認した項目に verdict='approve' を、見送りは 'skip' を設定してください。"
        "skip は条件が続く限り翌日以降に再提示されます。"
    )
    return "\n".join(lines)


def _format_naming_candidates(candidates: List[Dict[str, Any]]) -> str:
    """命名（テーマ立て）候補の状況テキスト節（就寝判断用、P4-b）。

    ``detect_naming_candidates`` が返した候補リストを
    「## テーマの芽」節として整形する。候補ゼロなら空文字を返す（節ごと出さない）。
    """
    if not candidates:
        return ""
    lines = [
        "## テーマの芽",
        "以下の候補に名前を与えると、テーマとして記憶の棚に立ちます。",
        "verdict='name' の場合、name フィールドに自分が付けたいテーマ名を記入してください。",
    ]
    for c in candidates:
        lines.append(f"- {c['line']}")
    return "\n".join(lines)


def build_day_close_situation_text(
    manager: Any,
    persona_id: str,
    context: Dict[str, Any],
    episodes_today: Optional[List[Dict[str, Any]]] = None,
    curation_candidates: Optional[List[Dict[str, Any]]] = None,
    naming_candidates: Optional[List[Dict[str, Any]]] = None,
    plan_date: Optional[str] = None,
) -> str:
    """就寝判断の tail 注入テキスト (judgment_points.md §8「見るもの」)。

    ``episodes_today`` (今日閉じた出来事) が与えられたら、層2 棚入れの
    選択材料として episode:N の一覧を添える。

    ``curation_candidates`` (編纂候補) が与えられたら「今日の棚の乱れ」節を
    追加する（P4-a 裁定の就寝判断相乗り）。候補ゼロ・None なら節ごと出さない。

    ``naming_candidates`` (命名候補) が与えられたら「テーマの芽」節を追加する
    （P4-b 裁定の就寝判断相乗り）。候補ゼロ・None なら節ごと出さない。

    Args:
        plan_date: 営業日 "YYYY-MM-DD"。深夜跨ぎリズムで就寝判断が翌暦日 01:00
            に発火するケースでは前日の暦日を渡す。None のとき clock.now().date()
            を使う (後方互換)。
    """
    now = clock.now()
    today = plan_date if plan_date is not None else now.date().isoformat()
    parts = [
        "[就寝判断]",
        f"今日 ({today}) を終えます。予定と実際に起きたことを見比べて、"
        "ふりかえりと明日の自分へのメモを書いてください。",
        "ふりかえりやメモで出典を挙げるときは、今日実際に参照・取得した"
        "情報源に限ってください。取得していない文献・サイト・ガイドライン名を"
        "根拠として書いてはいけません。",
        "",
        build_day_results_text(manager, persona_id, today),
    ]
    if episodes_today:
        parts += ["", _format_today_episodes(episodes_today)]
    # P4-a 編纂候補（棚の乱れ）
    if curation_candidates:
        curation_text = _format_curation_candidates(curation_candidates)
        if curation_text:
            parts += ["", curation_text]
    # P4-b 命名候補（テーマの芽）
    if naming_candidates:
        naming_text = _format_naming_candidates(naming_candidates)
        if naming_text:
            parts += ["", naming_text]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 判断点の起動
# ---------------------------------------------------------------------------


def validate_judgment_context(kind: str, context: Optional[Dict[str, Any]]) -> None:
    """呼び出し側が渡すべき context が揃っているかの検査 (**配線の誤り**)。

    :func:`run_judgment_point` は引数の組み立てで起きた**環境の障害** (DB が
    読めない等) を「起動できなかった」という結果へ畳むが、契約違反はそこに
    混ぜない — 畳むと発火経路の配線ミスが submitted=False として静かに流れ、
    誰も気づかないまま判断が起きなくなる。だから検査はここに分けて、
    **環境の状態を見るより前**に必ず raise させる (ペルソナ未ロード等で先に
    return してしまうと、配線ミスが環境の問題に化けて隠れる)。

    - ``on_event``: ``event_text`` が要る。無ければ「何のイベントか」の無い
      判断になる
    - ``post_session``: ``session_result`` が要る。無いまま組み立てると成果物
      ゼロ・0 ラウンド・終了理由不明の**起きていないセッション**を前提に裁定が
      走り、時間割の変更まで永続化される (発火側の
      :func:`saiverse.day_plan` も result が None のときは撃たない —
      「偽前提の状況テキストは作話を誘発する」)

    Raises:
        ValueError: 必須の context が欠けている。
    """
    ctx = context or {}
    if kind == KIND_ON_EVENT:
        if not str(ctx.get("event_text") or "").strip():
            raise ValueError(
                "on_event judgment requires context['event_text'] (non-empty)"
            )
    elif kind == KIND_POST_SESSION:
        if ctx.get("session_result") is None:
            raise ValueError(
                "post_session judgment requires context['session_result'] "
                "(judging a session that did not run fabricates its outcome)"
            )


def build_judgment_args(
    manager: Any, persona_id: str, kind: str, context: Dict[str, Any]
) -> Dict[str, Any]:
    """判断点 Playbook に渡す args (situation_text + response_schema + judgment_context)。

    旧 day_open 前処理の ``decay_desires`` (欲求の減衰) は欲求プールの退役で
    消えた (autonomous_behavior_v3.md §8)。
    """
    today = clock.now().date().isoformat()

    if kind == KIND_DAY_OPEN:
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
        # 層2 棚入れ (§9.1) と digest 配送 (D9-5) の対象 = このセッションの出来事。
        # WorkSessionResult.episode_ref が主経路 (呼び出し側 context の明示指定が優先)。
        # コマ締め (slot_close) で帰属が確定済みのセッションでは棚入れ欄を
        # 出さない — 同一セッションの二重帰属宣言は「再訪」ではないのに
        # revisit_count を偽増加させ recall の順位を汚染する (Codex 一巡目 #5。
        # 締めが失敗/不発だったセッションでは従来どおり欄を出し、帰属の
        # 代替経路として機能する)。
        episode_ref = _resolve_session_episode_ref(context)
        purpose_refs = collect_purpose_refs(manager, persona_id) if episode_ref else []
        shelving = bool(
            episode_ref and purpose_refs
            and not context.get("episode_attribution_done")
        )
        # コールローカル注入 (D9-3): LLM に渡る situation_text だけが原本を含む。
        # 保存用 (paired_situation_text) は episode 参照 + 読み口の一行に留める
        # (adapter の paired_action 展開で以後の Pulse 文脈に載り続けるため)。
        situation_text = build_post_session_situation_text(
            manager, persona_id, context, shelving=shelving,
        )
        paired_situation_text = build_post_session_situation_text(
            manager, persona_id, context, shelving=shelving,
            embed_transcript=False,
        )
        response_schema = build_post_session_schema(
            manager, persona_id, artifacts,
            str(task_ref) if task_ref else None,
            episode_purpose_refs=purpose_refs if shelving else None,
        )
        judgment_context = {
            "plan_date": today,
            "artifacts": artifacts,
            "task_ref": str(task_ref) if task_ref else None,
            "track_id": str(track_id) if track_id else None,
            "paired_situation_text": paired_situation_text,
            # digest メッセージの work_session metadata (finalize が復元する
            # ws_meta。旧 work_session 直書きの同形を保つ — day_close の収集と
            # 一日新聞が読む)。
            "ws_meta": {
                "task_ref": str(task_ref) if task_ref else None,
                "artifacts": artifacts,
                "rounds_used": _ws_get(sr, "rounds_used", 0) or 0,
                "budget_rounds": (
                    context.get("budget_rounds")
                    or _ws_get(sr, "budget_rounds", None)
                ),
                "ended_reason": _ws_get(sr, "ended_reason", None),
                "started_at": _ws_get(sr, "started_at", None),
                "ended_at": _ws_get(sr, "ended_at", None),
            },
        }
        ws_extra = _ws_get(sr, "extra", None)
        if isinstance(ws_extra, dict) and ws_extra:
            judgment_context["ws_meta"]["extra"] = dict(ws_extra)
        # episode_ref は shelving に関係なく載せる (RESULT_JSON が読む)。
        # purpose_refs は shelving 時のみ。
        # ⚠ 束 6c (2026-08-22) で作業セッションが出来事を開かなくなったので
        # (v3 §7)、episode_ref は実質いつも None。digest 配送の set_digest_ref も
        # 同便で退役した。旧データを持つ環境からの値だけがここを通る。
        if episode_ref:
            judgment_context["episode_ref"] = str(episode_ref)
        if shelving:
            judgment_context["purpose_refs"] = purpose_refs
    elif kind == KIND_ON_EVENT:
        validate_judgment_context(kind, context)
        event_text = str(context.get("event_text") or "").strip()
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
        # 営業日 (覚醒日) の算出 — 深夜跨ぎリズムで 01:00 に発火した就寝判断は
        # 「前日」が営業日になる。_find_day_schedules は autonomy_wiring に在るが、
        # autonomy_wiring が judgment_points を import するため遅延 import で循環回避。
        _day_close_plan_date: str
        try:
            from saiverse.autonomy_wiring import (
                _find_day_schedules,
                effective_plan_date,
            )
            sched = _find_day_schedules(manager, persona_id)
            _now_for_date = clock.now()
            _day_close_plan_date = effective_plan_date(
                _now_for_date, sched.get("wake"), sched.get("close")
            ).isoformat()
        except Exception:
            LOGGER.warning(
                "[judgment] failed to derive effective plan_date for day_close "
                "(persona=%s); falling back to calendar date", persona_id, exc_info=True,
            )
            _day_close_plan_date = today

        # 層2 棚入れ (§9.1): 対象 = 今日閉じた出来事すべて ({episode, purpose} ペア)。
        episodes_today = collect_today_closed_episodes(
            manager, persona_id, plan_date=_day_close_plan_date
        )
        episode_refs = [e["episode_ref"] for e in episodes_today]
        purpose_refs = collect_purpose_refs(manager, persona_id) if episode_refs else []
        shelving = bool(episode_refs and purpose_refs)

        # P4-a 編纂候補: ペルソナの memory.db を adapter 経由で読んで検知
        curation_candidates: List[Dict[str, Any]] = []
        try:
            persona_obj = (getattr(manager, "personas", None) or {}).get(persona_id)
            adapter = getattr(persona_obj, "sai_memory", None) if persona_obj else None
            mem_conn = getattr(adapter, "conn", None) if adapter else None
            if mem_conn is not None:
                from saiverse.curation import detect_curation_candidates
                curation_candidates = detect_curation_candidates(mem_conn, persona_id)
        except Exception:
            LOGGER.warning(
                "[judgment] failed to detect curation candidates for %s",
                persona_id, exc_info=True,
            )

        # P4-b 命名候補: main DB の persona_task から detect_naming_candidates で検知
        naming_candidates: List[Dict[str, Any]] = []
        try:
            from saiverse.curation import detect_naming_candidates
            naming_candidates = detect_naming_candidates(manager, persona_id)
        except Exception:
            LOGGER.warning(
                "[judgment] failed to detect naming candidates for %s",
                persona_id, exc_info=True,
            )

        situation_text = build_day_close_situation_text(
            manager, persona_id, context,
            episodes_today=episodes_today if shelving else None,
            curation_candidates=curation_candidates if curation_candidates else None,
            naming_candidates=naming_candidates if naming_candidates else None,
            plan_date=_day_close_plan_date,
        )
        response_schema = build_day_close_schema(
            manager, persona_id,
            episode_refs=episode_refs if shelving else None,
            purpose_refs=purpose_refs if shelving else None,
            curation_candidates=curation_candidates if curation_candidates else None,
            naming_candidates=naming_candidates if naming_candidates else None,
        )
        judgment_context = {
            "plan_date": _day_close_plan_date,
        }
        if curation_candidates:
            judgment_context["curation_candidates"] = curation_candidates
        if naming_candidates:
            judgment_context["naming_candidates"] = naming_candidates
        if shelving:
            judgment_context["episode_refs"] = episode_refs
            judgment_context["purpose_refs"] = purpose_refs
    else:
        raise ValueError(f"unknown judgment kind: {kind!r}")

    return {
        "situation_text": situation_text,
        "response_schema": response_schema,
        "judgment_context": json.dumps(judgment_context, ensure_ascii=False),
    }


#: 判断が結末に至らなかったときの結末 (``submitted=False`` の結果 dict の
#: ``outcome``)。呼び出し側が答えたい問いは 1 つ — **判断の代わりに自分で応対
#: してよいか**。それは「判断が世界へ作用しえた地点まで進んだか」と「席が
#: 残っていて後からもう一度走りうるか」で決まる。
#:
#: - ``aborted``: LLM へ渡る前に止まり、席は残っていない (そもそも取っていない /
#:   放棄済み)。→ 代替経路 **OK**
#: - ``no_effect``: メタレーンへ渡ったが、副作用ゼロが確定した失敗 (Beat 関所の
#:   閉鎖 / LLM エラー) で戻り、台帳は failed 終端。→ 代替経路 **OK**
#: - ``ran``: メタレーンが走った後、成功の証跡なしに戻った。finalize が判断を
#:   適用済みかもしれない。→ 代替経路 **NG** (判断の決定を上書きしてしまう)
#: - ``indeterminate``: 席が残っている / 別の claimant が走らせている / 台帳が
#:   読めない。回復 tick に再発火されうる。→ 代替経路 **NG** (二重処理になる)
OUTCOME_ABORTED = "aborted"
OUTCOME_NO_EFFECT = "no_effect"
OUTCOME_RAN = "ran"
OUTCOME_INDETERMINATE = "indeterminate"

#: 代替経路 (呼び出し側が判断を経ずに自分で応対する) を許す結末。
_DIRECT_FALLBACK_OUTCOMES = frozenset({OUTCOME_ABORTED, OUTCOME_NO_EFFECT})


def direct_fallback_allowed(result: Dict[str, Any]) -> bool:
    """``submitted=False`` の判断結果に対し、代替経路を走らせてよいか。

    **既定は「走らせない」**。``outcome`` の無い結果は「LLM が動いたかもしれない」
    として扱う (2026-08-14 Codex 指摘 F3)。判断が走った後の失敗を「起動できな
    かった」と読んで応対すると、finalize が note_only 等を適用した**後**に応答を
    重ねてしまう —— 判断の決定を機構が上書きする形になる。

    結末を書き忘れた経路は WARNING で表に出す。既定が拒否なので、書き忘れは
    「ユーザーの呼びかけへの沈黙」として現れる —— 黙って通すより、ログに残して
    気づける形にしておく。
    """
    outcome = result.get("outcome")
    if outcome is None:
        LOGGER.warning(
            "[judgment] result has no outcome; refusing the direct fallback "
            "(kind=%s reason=%s execution=%s)",
            result.get("kind"), result.get("reason"), result.get("execution_id"),
        )
        return False
    return outcome in _DIRECT_FALLBACK_OUTCOMES


def _abandon_seat(ledger: Any, execution_id: Optional[str], reason: str) -> bool:
    """LLM 開始前の離脱で、claim 済みの席を放棄する (prepared 限定 CAS)。

    ``mark_failed`` は running からの遷移も許すため、同じ execution_id を共有
    した別の claimant が既に走らせている台帳まで failed に壊しうる。放棄には
    :meth:`ExecutionLedger.abandon_prepared` (status=prepared のときだけ failed)
    を使う — これがこの用途のために用意されている条件付き遷移。

    Returns:
        True = 席は残っていない (放棄した / そもそも席が無い)。
        False = 放棄できなかった (他の claimant の所有、または台帳が応答しない)。
    """
    if ledger is None or execution_id is None:
        return True
    abandon = getattr(ledger, "abandon_prepared", None)
    try:
        if callable(abandon):
            return bool(abandon(execution_id, reason))
        # prepared 限定 CAS を持たない台帳 (旧テストスタブ) は mark_failed へ
        # degrade する。本番台帳は abandon_prepared を持つ。
        ledger.mark_failed(execution_id, reason)
        return True
    except Exception:
        LOGGER.warning(
            "[judgment] failed to abandon prepared seat (execution=%s reason=%s)",
            execution_id, reason, exc_info=True,
        )
        return False


def _try_mark_running(ledger: Any, execution_id: str) -> bool:
    """prepared → running の席取り (早い者勝ち CAS)。

    claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重 claim は
    同じ execution_id を両方へ runnable として返しうる — 実行を一人に絞るのは
    :meth:`ExecutionLedger.try_mark_running` (status=prepared のときだけ running)。
    持たない台帳 (旧テストスタブ) は無条件 ``mark_running`` へ degrade する
    (勝者一意化なしの従来挙動。本番台帳は try_mark_running を持つ)。

    台帳の例外はそのまま上げる (呼び出し側が「台帳が応答しない」として処理)。
    """
    try_mark = getattr(ledger, "try_mark_running", None)
    if callable(try_mark):
        return bool(try_mark(execution_id))
    ledger.mark_running(execution_id)
    return True


def run_judgment_point(
    manager: Any,
    persona_id: str,
    kind: str,
    context: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """判断点を 1 回起動する (状況テキスト組み立て → 動的スキーマ → Playbook 起動)。

    起動経路は meta_judgment v2 と同一: ``PulseController.submit_meta_judgment``
    (pulse_type="meta_judgment" → META アスペクト → standard モデル)。
    Playbook 内の judge ノードが構造化出力を生成し、``judgment_finalize`` ツールが
    検証・適用・SAIMemory 書き込みを行う。

    W1 Chunk A (A7): ``execution_id`` と ``manager.execution_ledger`` が両方
    あるときは台帳フロー — submit 直前に ``try_mark_running`` (prepared 限定
    CAS。二重 claim の敗者は台帳に書かず indeterminate で離脱)、例外は
    BeatGateClosedError / LLMError → failed (適用前・副作用ゼロ)、
    Cancelled / その他 → unknown (LLM が動いたか不明) に分類し、正常 return
    後は台帳 status の証跡で成功を判定する。どちらかが無ければ従来挙動に
    degrade する (WARN は発火側 ``autonomy_wiring.fire_judgment_point`` が出す)。

    Args:
        context: 判断点ごとの入力。
            - day_open: ``daily_budget_rounds`` (省略可) / ``scheduled_events`` (省略可)
            - post_session: ``session_result`` (WorkSessionResult または dict、必須) /
              ``task_ref`` / ``track_id`` / ``budget_rounds`` (いずれも省略時は
              session_result から読む)
            - on_event: ``event_text`` (必須) / ``is_alert`` (省略時 False。
              True なら reaction スキーマが engage_now のみに縮退)。
              **ユーザー会話中は原則発火させないこと** — 会話の至上性
              (judgment_points.md §7)。その抑止は呼び出し側の責務であり、
              本モジュールは判定しない (会話中の収穫はスルースが担う)
            - day_close: なし (予定 vs 実績は本モジュールが DB から収集する)

    Returns:
        ``{"kind", "playbook", "args", "submitted": bool, "errors": [...],
        "execution_id": str | None}``。
        起動できなかった場合は ``submitted=False`` + ``reason``。
    """
    context = context or {}
    playbook_name = JUDGMENT_PLAYBOOK_MAP.get(kind)
    if playbook_name is None:
        raise ValueError(
            f"unknown judgment kind: {kind!r} (expected one of {sorted(JUDGMENT_PLAYBOOK_MAP)})"
        )

    # 呼び出し側の契約違反 (必須 context の欠落) は畳まずに上げる。**環境の状態を
    # 見るより前**に検査する — ペルソナ未ロード等で先に return すると、配線ミスが
    # 環境の問題に化けて隠れる (2026-07-30 Codex 四巡目)。
    validate_judgment_context(kind, context)

    ledger = getattr(manager, "execution_ledger", None)
    tracked = ledger is not None and execution_id is not None

    def _abort(reason: str) -> Dict[str, Any]:
        """LLM 開始前の離脱: 席を放棄してから「起動できなかった」を返す。

        席を放棄しないまま submitted=False を返すと、呼び出し側の代替経路
        (on_event の direct dispatch) と、回復 tick による prepared の再発火が
        **両方**走る。放棄できなかった場合は結末を indeterminate にして、
        呼び出し側に代替経路を走らせない (二重処理の方が害が大きい)。
        """
        LOGGER.warning(
            "[judgment] %s aborted before dispatch: %s (persona=%s execution=%s)",
            kind, reason, persona_id, execution_id,
        )
        released = _abandon_seat(ledger if tracked else None, execution_id, reason)
        return {
            "kind": kind, "playbook": playbook_name, "submitted": False,
            "reason": reason, "execution_id": execution_id,
            "outcome": OUTCOME_ABORTED if released else OUTCOME_INDETERMINATE,
        }

    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        return _abort("persona not loaded")

    building_id = getattr(persona, "current_building_id", None)
    if not building_id:
        return _abort("no current building")

    pulse_controller = getattr(manager, "pulse_controller", None)
    if pulse_controller is None:
        return _abort("no pulse_controller")

    # 引数の組み立て (= 状況テキストと動的スキーマの DB 収集) は LLM 開始前の
    # 工程。ここで落ちたら副作用はゼロなので、例外でなく「起動できなかった」
    # として返す — 呼び出し側 (on_event の direct fallback / schedule の
    # backoff) はその戻り値で分岐する (2026-07-30 Codex 三巡目)。
    try:
        args = build_judgment_args(manager, persona_id, kind, context)
    except Exception as exc:
        LOGGER.warning(
            "[judgment] failed to build args for %s (persona=%s execution=%s)",
            kind, persona_id, execution_id, exc_info=True,
        )
        return _abort(f"args build failed: {exc!r}")

    if execution_id is not None:
        # execution_id を judgment_context に同乗させ finalize へ届ける
        # (playbook JSON は不変 — judgment_context は既に args で渡っている)。
        try:
            jctx = json.loads(args.get("judgment_context") or "{}")
        except (TypeError, ValueError):
            jctx = {}
        jctx["execution_id"] = execution_id
        args["judgment_context"] = json.dumps(jctx, ensure_ascii=False)

    errors: List[Dict[str, Any]] = []
    applied_events: List[Dict[str, Any]] = []

    def _capture_event(ev: Dict[str, Any]) -> None:
        if not isinstance(ev, dict):
            return
        if ev.get("type") == "error":
            errors.append(ev)
        elif ev.get("type") == "judgment_applied":
            # judgment_finalize が emit する適用結果 (kind / applied / extras)。
            # on_event の reaction 等、呼び出し側 (saiverse.autonomy_wiring) が
            # 判断結果に応じて後続処理 (engage_now の応対起動) を選ぶために使う。
            applied_events.append(ev)

    LOGGER.info(
        "[judgment] dispatching %s: persona=%s playbook=%s execution=%s",
        kind, persona_id, playbook_name, execution_id,
    )
    if tracked:
        # 不変条件 1: 不可逆処理 (LLM) の開始「前」に running を宣言する。
        # claim_execution は既存 prepared 行を再利用するため、ほぼ同時の二重
        # claim は同じ execution_id を両方へ runnable として返しうる — 勝者を
        # 一人に絞るのは、この prepared 限定 CAS (try_mark_running)。
        try:
            seat_won = _try_mark_running(ledger, execution_id)
        except Exception:
            LOGGER.warning(
                "[judgment] ledger mark_running failed; not dispatching %s "
                "(persona=%s execution=%s)", kind, persona_id, execution_id,
                exc_info=True,
            )
            # ここも LLM 開始前。席が残ったままなら結末は indeterminate になり、
            # 呼び出し側の代替経路は走らない (別の claimant が走らせている、
            # あるいは台帳が応答しない状態なので)。
            aborted = _abort("ledger transition failed")
            aborted.update({"args": args, "errors": errors,
                            "applied_events": applied_events})
            return aborted
        if not seat_won:
            # 敗者: 同じ execution_id の席を別の claimant が先に running へ
            # 進めた。判断は勝者側で走る (あるいはもう終端している) ので、
            # 台帳には一切書かずに離脱する — mark_failed 等を呼ぶと勝者の
            # 走行中台帳を壊す (try_mark_running の契約)。呼び出し側は代替
            # 経路を走らせてはいけない (勝者が同じイベントを処理するため
            # indeterminate)。
            LOGGER.info(
                "[judgment] %s seat already taken by another claimant; "
                "leaving without ledger writes (persona=%s execution=%s)",
                kind, persona_id, execution_id,
            )
            return {"kind": kind, "playbook": playbook_name, "args": args,
                    "submitted": False,
                    "reason": "seat taken by another claimant",
                    "outcome": OUTCOME_INDETERMINATE,
                    "errors": errors, "applied_events": applied_events,
                    "execution_id": execution_id}
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
        # 結末は「台帳へ何を書けたか」から導く (書けなかった = 席の状態が不明)。
        # 台帳の無い manager (旧テストスタブ) では例外の性質だけで決める。
        if tracked:
            terminal = _classify_runtime_failure(ledger, execution_id, exc)
            if terminal is None:
                outcome = OUTCOME_INDETERMINATE
            else:
                outcome = OUTCOME_NO_EFFECT if terminal == "failed" else OUTCOME_RAN
        else:
            outcome = (
                OUTCOME_NO_EFFECT if _runtime_failure_is_side_effect_free(exc)
                else OUTCOME_RAN
            )
        return {"kind": kind, "playbook": playbook_name, "args": args,
                "submitted": False, "reason": f"runtime exception: {exc!r}",
                "outcome": outcome,
                "errors": errors, "applied_events": applied_events,
                "execution_id": execution_id}

    if errors:
        for err in errors:
            LOGGER.warning(
                "[judgment] %s Playbook emitted error: persona=%s error=%s",
                kind, persona_id, err,
            )

    submitted = True
    #: 台帳 status を読めなかったときの結末 (読めた場合は None のまま)。
    unread_outcome: Optional[str] = None
    if tracked:
        # A7: 成功 = finalize 完了の永続証跡 (台帳 status) から導出する。
        status: Optional[str] = None
        try:
            status = ledger.get_execution(execution_id)["status"]
        except Exception:
            # 台帳が読めない = 証跡が確認できない。**成功へ倒さない**
            # (2026-08-14 Codex 指摘: 旧実装は初期値 submitted=True のまま通し、
            # 結末も付かないので代替経路の可否すら判定できなかった)。
            # ただし callback が finalize の judgment_applied を捕まえていれば、
            # それは台帳とは独立した一次証跡なので成功として扱ってよい —
            # 台帳の読み取り失敗だけを理由に、実際に下された判断を捨てない。
            # ただし**イベントの中身を確かめる**: 同じ判断点 (kind) のもので、
            # かつ applied が真のものだけ (finalize は適用できなかった場合も
            # applied=False で emit する。type だけ見ると「適用に失敗した」を
            # 「適用した」と読む。2026-08-14 Codex 三巡目)。
            if any(
                isinstance(ev, dict)
                and ev.get("kind") == kind
                and bool(ev.get("applied"))
                for ev in applied_events
            ):
                LOGGER.warning(
                    "[judgment] failed to read ledger status after %s; "
                    "accepting the finalize event captured by the callback "
                    "(persona=%s execution=%s)",
                    kind, persona_id, execution_id, exc_info=True,
                )
            else:
                LOGGER.warning(
                    "[judgment] failed to read ledger status after %s and no "
                    "finalize event was captured; treating the outcome as "
                    "indeterminate (persona=%s execution=%s)",
                    kind, persona_id, execution_id, exc_info=True,
                )
                submitted = False
                unread_outcome = OUTCOME_INDETERMINATE
                errors.append({
                    "type": "error",
                    "message": "ledger status unreadable after meta lane return",
                })
        from saiverse.execution_ledger import (
            STATUS_APPLIED,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_RUNNING,
        )
        if status in (STATUS_APPLIED, STATUS_COMPLETED):
            submitted = True
        elif status == STATUS_RUNNING:
            # finalize が mark_applied を呼ばずにメタレーンが戻った = 証跡なし。
            # 「成功 = finalize 完了の永続証跡」(A7 修正方針) に従い unknown 化
            # (自動再実行はされず、照合対象として観測面に残る)。
            submitted = False
            LOGGER.warning(
                "[judgment] %s returned without finalize evidence "
                "(ledger still running); marking unknown "
                "(persona=%s execution=%s)", kind, persona_id, execution_id,
            )
            try:
                ledger.mark_unknown(
                    execution_id, "meta lane returned without finalize evidence",
                )
            except Exception:
                LOGGER.warning(
                    "[judgment] mark_unknown failed after missing finalize "
                    "evidence (persona=%s execution=%s)",
                    persona_id, execution_id, exc_info=True,
                )
            errors.append({
                "type": "error",
                "message": "no finalize evidence: ledger still running "
                           "after meta lane return",
            })
        elif status == STATUS_FAILED:
            submitted = False
        elif status is not None:
            # unknown 等 (回復 tick との競合)。finalize 証跡なし = 成功と言えない。
            submitted = False
            errors.append({
                "type": "error",
                "message": f"ledger status {status} after meta lane return",
            })

    result = {"kind": kind, "playbook": playbook_name, "args": args,
              "submitted": submitted, "errors": errors,
              "applied_events": applied_events, "execution_id": execution_id}
    if not submitted:
        # メタレーンは例外なく戻ったが成功の証跡が無い (finalize 証跡なし /
        # failed / unknown)。LLM も finalize も走った後かもしれないので、
        # 呼び出し側の代替経路は許さない。台帳自体が読めなかった場合は
        # 「走ったかどうかも分からない」= indeterminate。
        result["outcome"] = unread_outcome or OUTCOME_RAN
    return result


def _runtime_failure_is_side_effect_free(exc: Exception) -> bool:
    """submit_meta_judgment の例外が「副作用ゼロ確定」か (A7、D4)。

    - BeatGateClosedError: 実行は始まっていない → 副作用ゼロ
    - LLMError: 出力なし = 世界適用前 → 副作用ゼロ
    - ExecutionCancelledException / その他: LLM が動いたか不明 → 副作用不明

    台帳の終端 (failed / unknown) と呼び出し側の結末 (no_effect / ran) は
    **この 1 つの判定から導く** — 二箇所で例外型を読み分けると、片方だけ直した
    ときに「台帳は unknown なのに呼び出し側は再実行してよいと読む」形の食い違いが
    生まれる。

    ⚠ **型だけでは「判断が適用済みか」は分からない** (2026-08-14 Codex 指摘)。
    ``sea/runtime_graph.py`` と ``sea/runtime_llm.py`` は Playbook 実行中の
    **任意の例外**を ``LLMError`` に包み直すため、ここへ届く LLMError は
    「プロバイダが出力前に落ちた」とは限らず、finalize が判断を適用した**後**の
    クラッシュでもありうる。それでも代替経路が判断を上書きしないのは、**台帳の
    合法遷移が歯止めになっている**から:

    - finalize が適用済み → 行は ``applied`` → ``applied → failed`` は不正遷移で
      :func:`_classify_runtime_failure` が書けず None を返す → 結末は
      ``indeterminate`` (代替経路は走らない)
    - finalize 前 → 行は ``running`` → ``running → failed`` は合法 → ``no_effect``
      (適用された判断が無いので、代替経路が上書きする決定も無い)

    つまり**この関数の型判定は台帳の裏付けとセットでしか正しくない**。台帳の無い
    経路 (execution_ledger を持たない manager = 旧テストスタブ) では裏付けが無く、
    型の推定がそのまま結末になる。本番の manager は必ず台帳を持つ。
    回帰は ``test_runtime_exception_after_finalize_is_indeterminate``。
    """
    from llm_clients.exceptions import LLMError
    from sea.beat_gate import BeatGateClosedError

    return isinstance(exc, (BeatGateClosedError, LLMError))


def _classify_runtime_failure(
    ledger: Any, execution_id: str, exc: Exception
) -> Optional[str]:
    """submit_meta_judgment の例外を台帳の終端状態へ分類する (A7、D4)。

    副作用ゼロ確定 (:func:`_runtime_failure_is_side_effect_free`) なら failed、
    そうでなければ unknown (自動再実行禁止の対象、intent §2.5)。

    台帳遷移自体の例外は握らず WARN に留める (二重障害でクラッシュさせない)。

    Returns:
        台帳へ実際に書いた終端 (``"failed"`` / ``"unknown"``)。遷移が失敗して
        **何も書けなかった場合は None** — 呼び出し側はそれを「席の状態が不明」
        として扱う (書けなかったことを成功と読まない)。
    """
    from llm_clients.exceptions import LLMError
    from sea.beat_gate import BeatGateClosedError
    from sea.cancellation import ExecutionCancelledException

    if isinstance(exc, BeatGateClosedError):
        detail = f"beat gate closed: {exc}"
    elif isinstance(exc, LLMError):
        detail = f"llm error: {exc}"
    elif isinstance(exc, ExecutionCancelledException):
        detail = f"cancelled: {exc}"
    else:
        detail = str(exc) or type(exc).__name__
    terminal = "failed" if _runtime_failure_is_side_effect_free(exc) else "unknown"

    try:
        if terminal == "failed":
            ledger.mark_failed(execution_id, detail)
        else:
            ledger.mark_unknown(execution_id, detail)
    except Exception:
        LOGGER.warning(
            "[judgment] ledger transition failed after runtime error "
            "(execution=%s original=%r)", execution_id, exc, exc_info=True,
        )
        return None
    return terminal


# ---------------------------------------------------------------------------
# finalize 用の検証ヘルパ (builtin_data/tools/judgment_finalize.py が使う)
# ---------------------------------------------------------------------------


def sanitize_timetable(
    manager: Any, persona_id: str, raw_slots: Any, plan_date: Any = None
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """LLM が返した timetable を検証し、save_day_plan 形式へ正規化する。

    不正な項目は **該当コマだけ棄却** して警告に積む (判断全体を落とさない。
    握り潰さない — 呼び出し側が WARN ログに流す):

    - dict でない / start が HH:MM でない / kind が未知 (封印済みの旧 kind を
      含む) → コマ棄却
    - ref が実在しない (task:N / desire:N が解決不能) → コマ棄却
    - 作業セッション系でないコマに ref が付いている → ref='none' に矯正 (コマは残す)
    - facility が実在しない → 'own_room' に矯正 (コマは残す)
    - 時刻の重複 → 後のコマを棄却 (ソート後に判定)

    Args:
        plan_date: 並び順の基準にするライフを引くための日付。省略すると暦の
            時刻順に退化する (ライフ未宣言の日と同じ後方互換の挙動)。深夜跨ぎ
            のライフがある日は **必ず渡すこと** — 渡さないと就寝コマが先頭に
            並び、保存時の丸めが一日を潰す (:func:`day_order_minutes`)。

    Returns:
        (正規化済みコマ配列 [一日の流れ順], 警告メッセージのリスト)
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
        if kind not in all_kinds():
            warnings.append(f"slot[{i}] rejected: unknown kind={kind!r}")
            continue

        ref = str(slot.get("ref") or REF_NONE).strip() or REF_NONE
        if kind not in worker_session_kinds():
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

    # 「一日の流れ」順に整列し、重複時刻は後のコマを棄却する
    # (save_day_plan の厳密昇順要件)。深夜跨ぎのライフ (例: 07:00〜01:00) では
    # 暦の時刻順が一日の前後関係と一致しない — 就寝 "00:30" を暦順で先頭に
    # 置くと、以降の丸めが後続コマを 1 分刻みに潰す (day_order_minutes 参照)。
    lives = get_lives(manager, persona_id, plan_date) if plan_date is not None else []
    cleaned.sort(key=lambda s: day_order_minutes(lives, s["start"]))
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
        pushed, range_notes = replace_remaining_slots(manager, persona_id, plan_date, merged)
    except ValueError as exc:
        warnings.append(f"コマの挿入に失敗 (時間割は不変): {exc}")
        return None, warnings
    warnings.extend(range_notes)
    return pushed, warnings


# NOTE: 旧 ``save_desk_memo`` (作業メモを Track metadata へ保存) は 2026-08-21 に
# 撤去した。読み手 (``day_plan._build_track_instruction``) は Track 撤廃で到達不能
# になっており、唯一の書き手だったセッション終了判断の ``task_verdict``
# (continue / blocked) も同日に呼び出しごと退役した。作業メモは独白記録に残る
# (引っ越し先の中断中エピソードのしおりは track_retirement.md §2 住人 4)。

