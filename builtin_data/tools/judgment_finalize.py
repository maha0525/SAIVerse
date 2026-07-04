"""judgment_finalize: 判断点 (judgment_points.md) の後処理ツール。

判断点 Playbook (judgment_day_open / judgment_post_session) の最終ノードで
呼ばれる。meta_judgment_finalize と同じ様式の kind ディスパッチ:

1. judge LLM ノードが返した dict (構造化出力結果) を受け取る
2. kind に応じて検証・適用する。不正な項目は **該当項目だけ棄却 + WARN**
   (判断全体を落とさない。握り潰さない)
   - day_open: timetable 検証 → save_day_plan + schedule_day_plan。
     promotions → track_create (from_candidate) スペル
   - post_session: task_verdict 適用 (done は artifact_ref の接地検証つき) /
     desk_memo → Track metadata / track_op / new_desires → desire_add /
     remaining_timetable → 残りコマの全置換
3. 整形済みテキスト ``monologue + 適用結果の要約行 + /spell 行`` を SAIMemory に
   ``role='assistant', line_role='meta_judgment'`` で保存する。
   メインキャッシュに LLM の生 JSON は残らない (不変条件 v2-A 継承)。

詳細: ``docs/intent/persona_cognition/judgment_points.md``
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from saiverse import clock
from saiverse import day_plan as day_plan_mod
from saiverse.desire_engine import DESIRE_TYPES
from saiverse.judgment_points import (
    KIND_DAY_OPEN,
    KIND_POST_SESSION,
    collect_promotion_refs,
    normalize_task_ref,
    sanitize_timetable,
    save_desk_memo,
)
from saiverse.persona_task_manager import (
    STATUS_COMPLETED,
    PersonaTaskManager,
    TaskNotFoundError,
)
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.judgment_finalize")


def _spell_to_text(name: str, args: Dict[str, Any]) -> str:
    """記録用の /spell 行 (正準形式。実行は args dict で直接渡る)。"""
    return f"/spell name='{name}' args={json.dumps(args, ensure_ascii=False)}"


def _fire_spell(
    name: str, args: Dict[str, Any], spells_record: List[Dict[str, Any]]
) -> None:
    """TOOL_REGISTRY 経由でスペルを実行する (meta_judgment_finalize と同じ経路)。

    Track 系スペルは PulseContext の deferred-track-ops に enqueue され、Pulse
    完了時に適用される。失敗は記録 + WARN (判断全体は落とさない)。
    """
    from tools import TOOL_REGISTRY

    tool_func = TOOL_REGISTRY.get(name)
    if tool_func is None:
        LOGGER.warning("[judgment_finalize] spell %r not found in registry", name)
        spells_record.append({
            "name": name, "args": dict(args),
            "result": "tool not found in registry",
        })
        return
    try:
        result = tool_func(**args)
        if isinstance(result, tuple):
            result_str = str(result[0]) if result else ""
        else:
            result_str = str(result)
        spells_record.append({"name": name, "args": dict(args), "result": result_str})
    except Exception as exc:
        LOGGER.exception("[judgment_finalize] spell %r raised", name)
        spells_record.append({
            "name": name, "args": dict(args), "result": f"error: {exc}",
        })


# ---------------------------------------------------------------------------
# day_open
# ---------------------------------------------------------------------------


def _finalize_day_open(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """起床判断の適用。timetable 保存 or promotions 発火があれば True (committed)。"""
    applied = False
    plan_date = ctx.get("plan_date") or clock.now().date().isoformat()

    # --- timetable: 検証 → save_day_plan + schedule_day_plan -------------
    slots, tt_warnings = sanitize_timetable(manager, persona_id, output.get("timetable"))
    warnings.extend(tt_warnings)

    # 予算合計の検証はソフト制約 (judgment_points.md §3.2)。超過は WARN のみで
    # 保存は通す — 予算ゲート (v2 §4.5) が乗ったら実行側で打ち切られる。
    daily_budget = ctx.get("daily_budget_rounds")
    if isinstance(daily_budget, int) and daily_budget > 0:
        total = sum(s["budget_rounds"] for s in slots)
        if total > daily_budget:
            warnings.append(
                f"budget_rounds 合計 {total} が日次予算 {daily_budget} を超過 (保存は続行)"
            )

    if not slots:
        # 空配列は不可 — 最低 1 コマを要求する (judgment_points.md §4)。
        # 検証で全滅した場合も plan は保存しない (前日の plan / 既存 plan を壊さない)。
        warnings.append(
            "timetable が検証後に空になったため、時間割は保存しませんでした"
        )
    else:
        # 全置換: 旧 plan の予約 (index ベースの key) を先に落としてから保存する。
        day_plan_mod.cancel_scheduled_slots(manager, persona_id, plan_date)
        try:
            day_plan_mod.save_day_plan(manager, persona_id, plan_date, slots)
        except ValueError as exc:
            warnings.append(f"時間割の保存に失敗: {exc}")
        else:
            pushed = day_plan_mod.schedule_day_plan(manager, persona_id, plan_date)
            applied = True
            lines.append(
                f"（今日の時間割を編成: {len(slots)} コマ、{pushed} コマを予約）"
            )
            for s in slots:
                lines.append(
                    f"  {s['start']} {s['kind']}"
                    + (f" {s['ref']}" if s["ref"] != "none" else "")
                    + f" @{s['facility']}"
                    + (f"（{s['note']}）" if s["note"] else "")
                )

    # --- promotions: 欲求 → 関心 (Track 化) ------------------------------
    promo_valid = set(collect_promotion_refs(manager, persona_id))
    for i, promo in enumerate(output.get("promotions") or []):
        if not isinstance(promo, dict):
            warnings.append(f"promotions[{i}] rejected: not a dict")
            continue
        desire_ref = str(promo.get("desire_ref") or "").strip()
        title = str(promo.get("title") or "").strip()
        if desire_ref not in promo_valid:
            warnings.append(
                f"promotions[{i}] rejected: {desire_ref!r} は昇格候補にありません"
            )
            continue
        if not title:
            warnings.append(f"promotions[{i}] rejected: title が空です")
            continue
        _fire_spell("track_create", {
            "track_type": "autonomous",
            "title": title,
            "intent": str(promo.get("intent") or ""),
            "from_candidate": normalize_task_ref(desire_ref),
        }, spells_record)
        applied = True

    return applied


# ---------------------------------------------------------------------------
# post_session
# ---------------------------------------------------------------------------


def _apply_task_verdict(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
) -> bool:
    """task_verdict の適用。タスク完了 or 机メモ保存があれば True。"""
    verdict = output.get("task_verdict")
    if not isinstance(verdict, dict):
        return False
    task_ref = ctx.get("task_ref")
    if not task_ref:
        warnings.append("task_verdict がありますが対象タスクが不明のため棄却")
        return False

    status = verdict.get("status")
    desk_memo = str(verdict.get("desk_memo") or "").strip()
    session_artifacts = [str(a) for a in (ctx.get("artifacts") or [])]

    ptm = PersonaTaskManager(manager.SessionLocal)
    try:
        task_id = ptm.resolve_task_ref(persona_id, normalize_task_ref(str(task_ref)))
    except TaskNotFoundError:
        warnings.append(f"task_verdict rejected: タスク {task_ref!r} が見つかりません")
        return False

    applied = False
    if status == "done":
        artifact_ref = str(verdict.get("artifact_ref") or "")
        if artifact_ref and artifact_ref in session_artifacts:
            # 接地検証 OK: 完了 + 成果物参照をタスクに刻む
            ptm.update_task_status(
                task_id, status=STATUS_COMPLETED, actor="judgment_post_session",
                persona_id=persona_id,
                reason=f"session verdict: done (artifact={artifact_ref})",
            )
            ptm.append_artifact_ref(
                task_id, artifact_ref,
                persona_id=persona_id, actor="judgment_post_session",
            )
            lines.append(
                f"（タスク {task_ref} を完了にした。成果物: {artifact_ref}）"
            )
            applied = True
        else:
            # やったフリの棄却: 成果物リストに無い ref は完了させない。
            # continue 相当に降格 (タスクは動かさず、机メモだけ残す)。
            warnings.append(
                f"task_verdict 'done' rejected: artifact_ref={artifact_ref!r} は"
                "このセッションの成果物リストにありません (タスクは完了させません)"
            )
            status = "continue"

    if status in ("continue", "blocked"):
        memo_label = "詰まり" if status == "blocked" else "続き"
        if desk_memo:
            lines.append(f"（机メモ [{memo_label}]: {desk_memo}）")
        track_id = ctx.get("track_id")
        if track_id:
            memo = {
                "text": desk_memo,
                "status": status,
                "task_ref": str(task_ref),
                "updated_at": clock.now().isoformat(timespec="seconds"),
            }
            try:
                if save_desk_memo(manager, str(track_id), memo):
                    applied = True
                else:
                    warnings.append(
                        f"desk_memo の保存先 Track {track_id!r} が見つかりません"
                    )
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] save_desk_memo raised")
                warnings.append(f"desk_memo の保存に失敗: {exc}")
        else:
            warnings.append(
                "desk_memo の保存先 Track が不明のため、独白記録にのみ残します"
            )
    elif status not in ("done",):
        warnings.append(f"task_verdict rejected: 未知の status={status!r}")

    return applied


def _finalize_post_session(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    spells_record: List[Dict[str, Any]],
) -> bool:
    """セッション終了判断の適用。何か 1 つでも適用したら True (committed)。"""
    applied = _apply_task_verdict(manager, persona_id, output, ctx, lines, warnings)

    # --- track_op: complete は Track の全タスク消化済みのときのみ --------
    track_op = output.get("track_op")
    track_id = ctx.get("track_id")
    if track_op == "complete":
        if not track_id:
            warnings.append("track_op 'complete' rejected: 対象 Track が不明です")
        else:
            ptm = PersonaTaskManager(manager.SessionLocal)
            open_tasks = [
                t for t in ptm.get_track_tasks(str(track_id)) if not t.get("done")
            ]
            if open_tasks:
                refs = ", ".join(t.get("task_ref") or "task:?" for t in open_tasks)
                warnings.append(
                    f"track_op 'complete' rejected: 未消化のタスクが残っています ({refs})"
                )
            else:
                _fire_spell("track_complete", {"track_id": str(track_id)}, spells_record)
                applied = True
    elif track_op not in (None, "none"):
        warnings.append(f"track_op rejected: 未知の値 {track_op!r}")

    # --- new_desires → desire_add (type/source 付き; v2 §5.2) ------------
    for i, desire in enumerate(output.get("new_desires") or []):
        if not isinstance(desire, dict):
            warnings.append(f"new_desires[{i}] rejected: not a dict")
            continue
        dtype = desire.get("type")
        title = str(desire.get("title") or "").strip()
        source_quote = str(desire.get("source_quote") or "").strip()
        if dtype not in DESIRE_TYPES:
            warnings.append(f"new_desires[{i}] rejected: 未知の型 {dtype!r}")
            continue
        if not title:
            warnings.append(f"new_desires[{i}] rejected: title が空です")
            continue
        _fire_spell("desire_add", {
            "title": title,
            "type": dtype,
            "source": source_quote,
        }, spells_record)
        applied = True

    # --- remaining_timetable: null=変更なし / 配列=残りコマの全置換 ------
    rt = output.get("remaining_timetable")
    if isinstance(rt, list):
        plan_date = ctx.get("plan_date") or clock.now().date().isoformat()
        if not rt:
            # 空配列は「残りを全部無くす」とも読めるが、空の時間割は保存できない
            # (最低 1 コマ要件) ため変更なし扱いにする。
            warnings.append(
                "remaining_timetable が空配列のため、時間割は変更しません"
            )
        else:
            slots, rt_warnings = sanitize_timetable(manager, persona_id, rt)
            warnings.extend(rt_warnings)
            if not slots:
                warnings.append(
                    "remaining_timetable が検証後に空になったため、時間割は変更しません"
                )
            else:
                try:
                    pushed = day_plan_mod.replace_remaining_slots(
                        manager, persona_id, plan_date, slots
                    )
                except ValueError as exc:
                    warnings.append(
                        f"remaining_timetable の置換に失敗 (時間割は不変): {exc}"
                    )
                else:
                    applied = True
                    lines.append(
                        f"（残りの時間割を組み替えた: {len(slots)} コマ、{pushed} コマを予約）"
                    )
    elif rt is not None:
        warnings.append(
            f"remaining_timetable rejected: 配列または null が必要 (got {type(rt).__name__})"
        )

    return applied


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def judgment_finalize(
    judgment_output: Optional[Dict[str, Any]] = None,
    kind: str = "",
    judgment_context: str = "",
    situation_text: str = "",
) -> Tuple[str, ToolResult, None]:
    """Finalize a judgment-point turn (see module docstring)."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("judgment_finalize requires an active persona context")
    manager = get_active_manager()
    if manager is None:
        raise RuntimeError("judgment_finalize requires an active manager context")

    output = judgment_output if isinstance(judgment_output, dict) else {}
    monologue = (output.get("monologue") or "").strip()
    try:
        ctx = json.loads(judgment_context) if judgment_context else {}
    except (TypeError, ValueError):
        LOGGER.warning(
            "[judgment_finalize] judgment_context is not valid JSON: %r",
            judgment_context,
        )
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    lines: List[str] = []
    warnings: List[str] = []
    spells_record: List[Dict[str, Any]] = []

    if kind == KIND_DAY_OPEN:
        committed = _finalize_day_open(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
        )
    elif kind == KIND_POST_SESSION:
        committed = _finalize_post_session(
            manager, persona_id, output, ctx, lines, warnings, spells_record,
        )
    else:
        LOGGER.warning("[judgment_finalize] unknown kind=%r; nothing applied", kind)
        warnings.append(f"unknown judgment kind: {kind!r}")
        committed = False

    for w in warnings:
        LOGGER.warning("[judgment_finalize] (%s/%s) %s", persona_id, kind, w)

    # --- 整形済みテキスト (JSON 非混入; 不変条件 v2-A) --------------------
    spell_lines = [_spell_to_text(s["name"], s["args"]) for s in spells_record]
    body_parts = [p for p in (monologue, "\n".join(lines), "\n".join(spell_lines)) if p]
    final_text = "\n\n".join(body_parts) or "(empty judgment)"
    scope = "committed" if committed or spells_record else "discardable"

    # --- SAIMemory への保存 (meta_judgment_finalize と同形式) --------------
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is not None:
        adapter = getattr(persona, "sai_memory", None)
        if adapter is not None:
            pulse_ctx = get_active_pulse_context()
            pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
            try:
                adapter.append_persona_message({
                    "role": "assistant",
                    "content": final_text,
                    # tz-aware UTC ISO で渡す (naive ISO は adapter 側で ±9h ずれる。
                    # docs/issues/history_manager_timestamp_tz_drift.md と同根)。
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"tags": ["meta_judgment", f"judgment:{kind}"]},
                    "line_role": "meta_judgment",
                    "scope": scope,
                    "pulse_id": pulse_id,
                    # 判断時に渡された状況テキストを Pulse タイムラインで見えるようにする。
                    "paired_action_text": situation_text.strip() or None,
                })
            except Exception:
                LOGGER.exception(
                    "[judgment_finalize] Failed to append persona message"
                )

    summary = (
        f"Judgment finalized (kind={kind}, applied={committed}, "
        f"spells={len(spells_record)}, warnings={len(warnings)}, scope={scope})"
    )
    return summary, ToolResult(history_snippet=summary), None


def schema() -> ToolSchema:
    return ToolSchema(
        name="judgment_finalize",
        description=(
            "Internal tool for judgment-point Playbooks only (judgment_day_open / "
            "judgment_post_session). Receives the judge node's structured output "
            "(dict), validates and applies it per judgment kind (day plan save, "
            "task verdict with artifact grounding, desk memo, promotions, new "
            "desires), and persists the resulting monologue + summary text to "
            "SAIMemory. Invalid items are rejected individually with warnings."
        ),
        parameters={
            "type": "object",
            "properties": {
                "judgment_output": {"type": "object"},
                "kind": {"type": "string"},
                "judgment_context": {"type": "string"},
                "situation_text": {"type": "string"},
            },
            "required": ["judgment_output", "kind"],
        },
        result_type="string",
        spell=False,
    )
