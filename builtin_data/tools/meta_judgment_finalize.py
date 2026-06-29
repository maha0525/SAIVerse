"""meta_judgment_finalize: メタ判断 v2 後処理ツール。

メタ判断 v2 (構造化出力ベース) の各 Playbook (meta_judgment_alert /
_running / _idle_pending / _idle_empty) の最終ノードで呼ばれる。

役割:
1. judge LLM ノードが返した dict (構造化出力結果) を受け取る
2. 状況 (situation_kind) に応じて /spell 行を組み立てる
3. 各 /spell を内部実行 (既存の Track 操作スペル経由 → PulseContext.deferred_track_ops に enqueue)
4. 整形済みテキスト ``monologue + "\n\n" + /spell ...`` を SAIMemory に
   ``role='assistant', line_role='meta_judgment'`` として保存
   - Track 操作なし (= C状況の continue / 全状況で空の判断) なら scope='discardable'
   - Track 操作あり なら scope='committed'
5. ``meta_judgment_log`` テーブルにも書き込む (旧仕様互換)

メインキャッシュ (= persona thread の messages 履歴) に残るのは
**整形済み自然言語テキスト + /spell 行のみ**。LLM の生 JSON 応答は履歴に
残らない (Intent A v0.9 不変条件 11、新 Intent v0.1 不変条件 v2-A)。

詳細: ``docs/intent/persona_cognition/meta_judgment_structured.md``
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.meta_judgment_finalize")


def _format_spells(
    situation_kind: str,
    output: Dict[str, Any],
    running_track_id: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Build the list of (spell_name, args) pairs to fire for this situation.

    Returns an empty list if the structured output indicates "no Track op"
    (e.g. C with action='continue', or malformed output).
    """
    if situation_kind == "alert_present":
        track_id = output.get("activate_track_id")
        if track_id:
            return [("track_activate", {"track_id": track_id})]
        return []

    if situation_kind == "running_active":
        action = output.get("action")
        if action in (None, "continue"):
            return []
        if not running_track_id:
            return []
        if action == "pause":
            return [("track_pause", {"track_id": running_track_id})]
        if action == "complete":
            return [("track_complete", {"track_id": running_track_id})]
        if action == "abort":
            return [("track_abort", {"track_id": running_track_id})]
        return []

    if situation_kind == "idle_with_pending":
        decision = output.get("decision") or {}
        if decision.get("type") == "activate":
            track_id = decision.get("track_id")
            if track_id:
                return [("track_activate", {"track_id": track_id})]
            return []
        if decision.get("type") == "create":
            title = decision.get("title")
            track_type = decision.get("track_type") or "autonomous"
            intent = decision.get("intent") or ""
            if title:
                return [("track_create", {
                    "title": title,
                    "track_type": track_type,
                    "intent": intent,
                })]
            return []
        if decision.get("type") == "promote":
            # 候補 (desire ノートの Task) を Track 化 (autonomous_desire.md §6/§11.4)。
            # track_create が from_candidate で候補を新 Track へ張り替える (= 昇格)。
            candidate_ref = decision.get("candidate_ref")
            title = decision.get("title")
            intent = decision.get("intent") or ""
            if candidate_ref and title:
                return [("track_create", {
                    "title": title,
                    "track_type": "autonomous",
                    "intent": intent,
                    "from_candidate": candidate_ref,
                })]
            return []
        return []

    if situation_kind == "idle_no_pending":
        create = output.get("create") or {}
        title = create.get("title")
        track_type = create.get("track_type") or "autonomous"
        intent = create.get("intent") or ""
        if title:
            return [("track_create", {
                "title": title,
                "track_type": track_type,
                "intent": intent,
            })]
        return []

    if situation_kind == "life_purpose_unset":
        # 生きる目的のドラフト (autonomous_desire.md §4)。purpose 必須、interests /
        # vocations は配列。実行は tool_func(**args) で直接渡るため配列も問題ない
        # (/spell テキストは記録用のみ)。
        purpose = (output.get("purpose") or "").strip()
        if purpose:
            return [("life_purpose_set", {
                "purpose": purpose,
                "interests": output.get("interests") or [],
                "vocations": output.get("vocations") or [],
            })]
        return []

    LOGGER.warning(
        "[meta_judgment_finalize] unknown situation_kind=%r; no spells will fire",
        situation_kind,
    )
    return []


def _spell_to_text(name: str, args: Dict[str, Any]) -> str:
    """Render a /spell line for the memory record (display only; execution uses args dict).

    正準形式 ``/spell name='ツール名' args={...}`` で出力する。KV 自由形式は配列・
    オブジェクト引数を解析できないため、見本としても正しい正準形式に統一する。
    """
    return f"/spell name='{name}' args={json.dumps(args, ensure_ascii=False)}"


def _build_prompt_snapshot(
    situation_text: str, trigger_context: str, recent_judgments: str,
) -> Optional[str]:
    """judge に渡された動的プロンプト (= デバッグで見たい内容) を再構成する。

    Playbook judge ノードの action テンプレートの静的部分は除き、MetaLayer が
    組み立てた可変部分 (トリガー情報 / 状況テキスト / 過去ログ) を結合する。
    Pulse タイムライン (paired_action_text) と meta_judgment_log.prompt_snapshot に
    載せ、メタ判断時のプロンプトを後から確認できるようにする。
    """
    parts: List[str] = []
    if trigger_context and trigger_context.strip() not in ("", "{}"):
        parts.append(f"トリガー情報: {trigger_context}")
    if situation_text and situation_text.strip():
        parts.append(situation_text.strip())
    if recent_judgments and recent_judgments.strip():
        parts.append(recent_judgments.strip())
    text = "\n\n".join(parts).strip()
    return text or None


def meta_judgment_finalize(
    judgment_output: Optional[Dict[str, Any]] = None,
    situation_kind: str = "",
    running_track_id: str = "",
    trigger_type: str = "",
    trigger_context: str = "",
    track_at_judgment_id: str = "",
    situation_text: str = "",
    recent_judgments: str = "",
) -> Tuple[str, ToolResult, None]:
    """Finalize a meta_judgment v2 turn (see module docstring)."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "meta_judgment_finalize requires an active persona context"
        )

    output = judgment_output if isinstance(judgment_output, dict) else {}
    monologue = (output.get("monologue") or "").strip()
    prompt_snapshot = _build_prompt_snapshot(
        situation_text, trigger_context, recent_judgments,
    )

    spells_to_fire = _format_spells(situation_kind, output, running_track_id)
    spell_lines = [_spell_to_text(name, args) for name, args in spells_to_fire]
    spell_block = "\n".join(spell_lines)
    if monologue and spell_block:
        final_text = f"{monologue}\n\n{spell_block}"
    elif spell_block:
        final_text = spell_block
    else:
        final_text = monologue or "(empty meta judgment)"

    scope = "committed" if spells_to_fire else "discardable"

    # Execute spells. Each Track-mutating spell enqueues onto PulseContext
    # (deferred-track-ops); the runtime applies them at Pulse completion.
    from tools import TOOL_REGISTRY

    spells_record: List[Dict[str, Any]] = []
    for spell_name, spell_args in spells_to_fire:
        tool_func = TOOL_REGISTRY.get(spell_name)
        if tool_func is None:
            LOGGER.warning(
                "[meta_judgment_finalize] spell %r not found in registry", spell_name
            )
            spells_record.append({
                "name": spell_name,
                "args": dict(spell_args),
                "result": "tool not found in registry",
            })
            continue
        try:
            result = tool_func(**spell_args)
            if isinstance(result, tuple):
                result_str = str(result[0]) if result else ""
            else:
                result_str = str(result)
            spells_record.append({
                "name": spell_name,
                "args": dict(spell_args),
                "result": result_str,
            })
        except Exception as exc:
            LOGGER.exception(
                "[meta_judgment_finalize] spell %r raised", spell_name
            )
            spells_record.append({
                "name": spell_name,
                "args": dict(spell_args),
                "result": f"error: {exc}",
            })

    # Persist as assistant message in SAIMemory (line_role='meta_judgment').
    # Intent v2 §6: メインキャッシュには整形済みテキストのみ残す。
    manager = get_active_manager()
    persona = (
        (getattr(manager, "personas", None) or {}).get(persona_id)
        if manager else None
    )
    if persona is not None:
        adapter = getattr(persona, "sai_memory", None)
        if adapter is not None:
            pulse_ctx = get_active_pulse_context()
            pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
            try:
                adapter.append_persona_message({
                    "role": "assistant",
                    "content": final_text,
                    # tz-aware UTC ISO 文字列で渡す。naive ISO
                    # (datetime.utcnow().isoformat()) を渡すと adapter の
                    # _timestamp_to_epoch が naive をプロセスの system TZ で
                    # 解釈し、created_at が ±9h ずれる
                    # (docs/issues/history_manager_timestamp_tz_drift.md と同根)。
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "metadata": {"tags": ["meta_judgment"]},
                    "line_role": "meta_judgment",
                    "scope": scope,
                    "pulse_id": pulse_id,
                    # メタ判断時のプロンプトを Pulse タイムラインで見えるようにする
                    # (paired_action_text が「渡されたプロンプト」として表示される)。
                    "paired_action_text": prompt_snapshot,
                })
            except Exception:
                LOGGER.exception(
                    "[meta_judgment_finalize] Failed to append persona message"
                )

    # Persist to meta_judgment_log table (legacy compatibility).
    if manager is not None and hasattr(manager, "SessionLocal"):
        try:
            from database.models import MetaJudgmentLog
            session_factory = manager.SessionLocal
            db = session_factory()
            try:
                row = MetaJudgmentLog(
                    judgment_id=str(uuid.uuid4()),
                    persona_id=persona_id,
                    track_at_judgment_id=track_at_judgment_id or None,
                    trigger_type=trigger_type or "unknown",
                    trigger_context=trigger_context or None,
                    prompt_snapshot=prompt_snapshot,
                    judgment_thought=monologue or None,
                    spells_emitted=(
                        json.dumps(spells_record, ensure_ascii=False)
                        if spells_record else None
                    ),
                    committed_to_main_cache=bool(spells_to_fire),
                )
                db.add(row)
                db.commit()
            except Exception:
                db.rollback()
                LOGGER.exception(
                    "[meta_judgment_finalize] meta_judgment_log write failed"
                )
            finally:
                db.close()
        except Exception:
            LOGGER.exception(
                "[meta_judgment_finalize] meta_judgment_log path failed"
            )

    summary = (
        f"Meta judgment finalized "
        f"(kind={situation_kind}, spells={len(spells_to_fire)}, scope={scope})"
    )
    return summary, ToolResult(history_snippet=summary), None


def schema() -> ToolSchema:
    return ToolSchema(
        name="meta_judgment_finalize",
        description=(
            "Internal tool for meta_judgment v2 Playbooks only. Receives the "
            "judge node's structured output (dict), formats /spell lines for "
            "the situation, executes them (deferred via PulseContext), and "
            "persists the resulting monologue + /spell text to SAIMemory + "
            "meta_judgment_log."
        ),
        parameters={
            "type": "object",
            "properties": {
                "judgment_output": {"type": "object"},
                "situation_kind": {"type": "string"},
                "running_track_id": {"type": "string"},
                "trigger_type": {"type": "string"},
                "trigger_context": {"type": "string"},
                "track_at_judgment_id": {"type": "string"},
                "situation_text": {"type": "string"},
                "recent_judgments": {"type": "string"},
            },
            "required": ["judgment_output", "situation_kind"],
        },
        result_type="string",
        spell=False,
    )
