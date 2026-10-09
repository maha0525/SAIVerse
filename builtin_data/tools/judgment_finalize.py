"""judgment_finalize: 判断点 (judgment_points.md) の後処理ツール。

判断点 Playbook (``judgment_on_event``) の最終ノードで呼ばれる:

1. judge LLM ノードが返した dict (構造化出力結果) を受け取る
2. 検証・適用する。不正な項目は **該当項目だけ棄却 + WARN** (判断全体を
   落とさない。握り潰さない)
   - on_event: reaction (engage_now は結果への反映のみ — 応対の起動は
     呼び出し側の責務 / add_task はタスク帳へシステムタスクを一件積む /
     note_only は覚え書きを判断の記録に載せるだけ (別置きしない) / ignore は
     記録のみ)。alert では engage_now 以外を棄却 (スキーマ縮退の二重ガード)
3. 整形済みテキスト ``monologue + 適用結果の要約行`` を SAIMemory に
   ``role='assistant', line_role='meta_judgment'`` で保存する。
   メインキャッシュに LLM の生 JSON は残らない (不変条件 v2-A 継承)。

v2 の他の判断点 (起床 day_open / セッション終了 post_session / 就寝 day_close)
の適用は、判断点ごと退役した (autonomous_behavior_v3.md §8 /
autonomous_behavior_v04_plan.md 段 1-4)。会話終了判断 (post_conversation) は
2026-08-16 の裁定で退役済み。

W1 Chunk B (A8/A9/A11): ``manager.execution_ledger`` と
``judgment_context.execution_id`` が両方あるとき (tracked) は実行台帳フロー —
入口で台帳 status=running を検査して再 finalize の二重適用を封じ、SAIMemory
判断行は直書きでなく ``mark_applied`` の outbox (``saimemory.append``) に凍結
する (配送失敗は「適用済み・記録待ち」として pending に残り関所/回復 tick が
引き継ぐ)。台帳が無い環境 (旧テスト) は従来の直書きに degrade する。

⚠ **判断が直接スペルを撃つ経路は束 6c (2026-08-22) で消えた。** 「失敗した
spell を本人の /spell 行にせず、システム名義の適用失敗通知 (``perception.push``)
で届ける」という A11/不変条件 7 の規律は、供給源が消えたので発火する場面が
無くなった — 規律そのものは正しいので、判断がまたスペルを撃つ形になったときは
ここへ書き戻す。

詳細: ``docs/intent/persona_cognition/judgment_points.md`` /
``docs/handoff/2026-07-19_w1_judgment_ledger_handoff.md`` D6〜D10
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from saiverse.judgment_points import (
    KIND_ON_EVENT,
    REACTION_ADD_TASK,
    REACTION_ENGAGE_NOW,
    REACTION_IGNORE,
    REACTION_NOTE_ONLY,
)
from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.judgment_finalize")


# ---------------------------------------------------------------------------
# on_event
# ---------------------------------------------------------------------------


def _add_event_task(
    manager: Any,
    persona_id: str,
    task_text: str,
    *,
    event_text: Optional[str],
    stimulus_id: Optional[str],
) -> Dict[str, Any]:
    """add_task: イベントを受けて「あとで取り組む」一件をタスク帳に積む。

    タスク帳のシステムタスク (``ORIGIN_SYSTEM``、期限なし・相手なし) として
    追加する — 機械がペルソナに差し込む急ぎでない依頼の亜種で、引き当て順は
    締め切りの後 (saiverse/task_book.py)。中身は判断の意図 (``task``) と、
    きっかけのイベントの本文の抜粋。

    出どころ参照 (``origin_ref``) と冪等キーは刺激の ID から作る — 同じ刺激に
    対する finalize の再実行で同じ一件が増えない。刺激の ID が無い (義務化
    以前の台帳行の再発火) ときは冪等キーなしで積む。
    """
    from saiverse.task_book import ORIGIN_SYSTEM, add_entry

    event_excerpt = str(event_text or "").strip()
    content = task_text
    if event_excerpt:
        content = f"{task_text}\n\n（きっかけのイベント: {event_excerpt}）"
    meta: Dict[str, Any] = {"source": "on_event"}
    if event_excerpt:
        meta["event_text"] = event_excerpt
    sid = stimulus_id if isinstance(stimulus_id, str) and stimulus_id.strip() else None
    return add_entry(
        manager,
        persona_id,
        content,
        origin=ORIGIN_SYSTEM,
        origin_ref=sid,
        meta=meta,
        idem_key=f"on_event:{sid}" if sid else None,
    )


def _finalize_on_event(
    manager: Any,
    persona_id: str,
    output: Dict[str, Any],
    ctx: Dict[str, Any],
    lines: List[str],
    warnings: List[str],
    summary_extras: List[str],
) -> bool:
    """イベント到着判断の適用。

    engage_now は状態を変えない — 「今すぐ応対する」の実行 (Pulse 起動 /
    Track alert 処理) は呼び出し側の責務で、ここでは判断結果を要約
    (summary_extras) と記録テキストに反映するのみ (本番の応対起動は
    saiverse/autonomy_wiring.py の handle_external_event が judgment_applied
    イベント経由で reaction を読んで行う)。
    """
    applied = False
    is_alert = bool(ctx.get("is_alert"))

    reaction = output.get("reaction")
    rtype = reaction.get("type") if isinstance(reaction, dict) else None
    if not isinstance(reaction, dict) or not rtype:
        warnings.append(
            f"reaction rejected: type を持つ object が必要 (got {reaction!r})"
        )
    elif is_alert and rtype != REACTION_ENGAGE_NOW:
        # スキーマ縮退 (engage_now のみ) の二重ガード
        warnings.append(
            f"reaction rejected: alert イベントでは engage_now のみ選べます "
            f"(got {rtype!r})"
        )
    elif rtype == REACTION_ENGAGE_NOW:
        summary_extras.append("reaction=engage_now")
        lines.append("（このイベントに今すぐ応対する）")
        applied = True
    elif rtype == REACTION_ADD_TASK:
        summary_extras.append("reaction=add_task")
        task_text = str(reaction.get("task") or "").strip()
        if not task_text:
            warnings.append("add_task rejected: task が空です")
        else:
            try:
                _add_event_task(
                    manager, persona_id, task_text,
                    event_text=ctx.get("event_text"),
                    stimulus_id=ctx.get("stimulus_id"),
                )
                applied = True
                lines.append(f"（あとで取り組むためにタスク帳へ積む: {task_text}）")
            except Exception as exc:
                LOGGER.exception("[judgment_finalize] add_task raised")
                warnings.append(f"タスク帳への追加に失敗: {exc}")
    elif rtype == REACTION_NOTE_ONLY:
        # 覚え書きは判断の記録 (この関数が組む lines → ペルソナの記憶に残る
        # 判断行) に載るだけで、別の置き場には書かない (2026-10-09 決定 —
        # 旧実装は day_plan の meta.event_memos にも積んでいた)。
        summary_extras.append("reaction=note_only")
        memo_text = str(reaction.get("memo") or "").strip()
        if not memo_text:
            warnings.append("note_only rejected: memo が空です")
        else:
            applied = True
            lines.append(f"（覚え書きに留める: {memo_text}）")
    elif rtype == REACTION_IGNORE:
        summary_extras.append("reaction=ignore")
        lines.append("（このイベントには反応しない）")
    else:
        warnings.append(f"reaction rejected: 未知の type {rtype!r}")

    return applied


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def _resolve_tracked_ledger(
    manager: Any, ctx: Dict[str, Any], kind: str,
) -> Tuple[Any, Optional[str], Optional[str]]:
    """台帳フローで走るか (tracked) を判定する (D6 の degrade 分岐)。

    tracked = ``manager.execution_ledger`` と ``judgment_context.execution_id``
    (Chunk A の run_judgment_point が同乗させる) が**両方**あるとき。どちらかが
    無い環境 (台帳なし・旧テスト) は従来挙動 (SAIMemory 直書き) に degrade する。
    台帳 status の読み取り失敗も WARN + degrade (可用性優先、従来挙動)。

    Returns:
        ``(ledger, execution_id, status)``。untracked なら ``(None, None, None)``。
    """
    execution_id = str(ctx.get("execution_id") or "").strip() or None
    ledger = getattr(manager, "execution_ledger", None)
    if ledger is None or execution_id is None:
        return None, None, None
    try:
        status = ledger.get_execution(execution_id).get("status")
    except Exception:
        LOGGER.warning(
            "[judgment_finalize] ledger status read failed; degrading to "
            "untracked finalize (kind=%s execution=%s)", kind, execution_id,
            exc_info=True,
        )
        return None, None, None
    return ledger, execution_id, status


def _build_judgment_message(
    kind: str,
    final_text: str,
    monologue: str,
    scope: str,
    situation_text: str,
) -> Dict[str, Any]:
    """SAIMemory 判断行の message dict (tracked/untracked 共通の凍結形)。"""
    pulse_ctx = get_active_pulse_context()
    pulse_id = getattr(pulse_ctx, "pulse_id", None) if pulse_ctx else None
    paired = situation_text.strip()
    return {
        "role": "assistant",
        "content": final_text,
        # tz-aware UTC ISO で渡す (naive ISO は adapter 側で ±9h ずれる。
        # docs/issues/history_manager_timestamp_tz_drift.md と同根)。
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # metadata.judgment: 独白本文を適用エコー行と構造的に分離して持つ
        # (content は独白+要約行の全文 — ペルソナの文脈に乗る内容)。
        "metadata": {
            "tags": ["meta_judgment", f"judgment:{kind}"],
            "judgment": {"kind": kind, "monologue": monologue},
        },
        "line_role": "meta_judgment",
        "scope": scope,
        "pulse_id": str(pulse_id) if pulse_id is not None else None,
        # 判断時に渡された状況テキストを Pulse タイムラインで見えるようにする。
        "paired_action_text": paired or None,
    }


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

    # --- 実行単位の冪等 (A8/D6): tracked で running でなければ再適用しない ----
    from saiverse.execution_ledger import STATUS_RUNNING
    from saiverse.execution_ledger_wiring import TARGET_SAIMEMORY_APPEND

    ledger, execution_id, ledger_status = _resolve_tracked_ledger(manager, ctx, kind)
    tracked = ledger is not None
    if tracked and ledger_status != STATUS_RUNNING:
        # 再 finalize (適用済み/終端) — 世界更新の二重適用の口を閉じる。
        msg = (
            f"Judgment already finalized (execution={execution_id}, "
            f"status={ledger_status}); nothing applied"
        )
        LOGGER.info("[judgment_finalize] %s (persona=%s kind=%s)",
                    msg, persona_id, kind)
        return msg, ToolResult(history_snippet=msg), None

    lines: List[str] = []
    warnings: List[str] = []
    summary_extras: List[str] = []

    if kind == KIND_ON_EVENT:
        committed = _finalize_on_event(
            manager, persona_id, output, ctx, lines, warnings, summary_extras,
        )
    else:
        LOGGER.warning("[judgment_finalize] unknown kind=%r; nothing applied", kind)
        warnings.append(f"unknown judgment kind: {kind!r}")
        committed = False

    for w in warnings:
        LOGGER.warning("[judgment_finalize] (%s/%s) %s", persona_id, kind, w)

    # --- 整形済みテキスト (JSON 非混入; 不変条件 v2-A) --------------------
    body_parts = [p for p in (monologue, "\n".join(lines)) if p]
    final_text = "\n\n".join(body_parts) or "(empty judgment)"
    scope = "committed" if committed else "discardable"

    message = _build_judgment_message(
        kind, final_text, monologue, scope, situation_text,
    )

    if tracked:
        # --- 台帳化 (A8/D6): 直書きを廃し mark_applied + outbox に載せ替え ---
        # RESULT_JSON 標準 (D6/§11-1): 照合 (#5) と呼び出し側読み出しに足る最小。
        result_json: Dict[str, Any] = {
            "kind": kind,
            "committed": committed,
            "scope": scope,
            "warnings": len(warnings),
        }
        if kind == KIND_ON_EVENT:
            reaction_type = next(
                (e.split("=", 1)[1] for e in summary_extras
                 if e.startswith("reaction=")),
                None,
            )
            if reaction_type:
                result_json["reaction"] = reaction_type

        # wiring の saimemory.append handler の payload 契約
        # (execution_ledger_wiring._make_saimemory_append_handler):
        # {"message": {...}, "building_id": ..., "thread_suffix": ...}。
        # 現行 append_persona_message(message) は building_id=None /
        # thread_suffix=None 相当 (persona スレッド直書き) — それを正確に写す。
        outbox_items: List[Dict[str, Any]] = [{
            "target": TARGET_SAIMEMORY_APPEND,
            "persona_id": persona_id,
            "payload": {
                "message": message,
                "building_id": None,
                "thread_suffix": None,
            },
        }]
        # mark_applied の失敗 (台帳遷移例外) は素直に raise する: 世界更新は
        # 済み・台帳は running のまま → run_judgment_point が unknown 化 →
        # 照合対象として観測面に残る (intent の「分裂を見えるようにする」)。
        # 配送失敗は mark_applied 内で処理される (pending 残存 → 関所/回復 tick)。
        ledger.mark_applied(
            execution_id, result=result_json,
            outbox_items=outbox_items, deliver=True,
        )
    else:
        # --- 従来経路 (台帳なし環境・旧テスト): SAIMemory 直書き -----------
        persona = (getattr(manager, "personas", None) or {}).get(persona_id)
        if persona is not None:
            adapter = getattr(persona, "sai_memory", None)
            if adapter is not None:
                try:
                    adapter.append_persona_message(message)
                except Exception:
                    LOGGER.exception(
                        "[judgment_finalize] Failed to append persona message"
                    )

    summary = (
        f"Judgment finalized (kind={kind}, applied={committed}, "
        f"warnings={len(warnings)}, scope={scope})"
    )
    if summary_extras:
        # on_event の reaction 等、呼び出し側が読む判断結果。
        summary += " [" + ", ".join(summary_extras) + "]"

    # 適用結果を Pulse の event_callback へ通知する (best-effort)。
    # run_judgment_point (saiverse/judgment_points.py) がこれを捕捉して
    # applied_events として呼び出し側へ返す — on_event の engage_now で
    # 応対 Pulse を起動するか等を、本番配線 (saiverse/autonomy_wiring.py) が
    # 判断結果に基づいて選ぶための唯一の戻り経路。
    try:
        from tools.context import get_event_callback

        _cb = get_event_callback()
        if callable(_cb):
            _cb({
                "type": "judgment_applied",
                "kind": kind,
                "applied": committed,
                "extras": list(summary_extras),
            })
    except Exception:
        LOGGER.debug(
            "[judgment_finalize] failed to emit judgment_applied event",
            exc_info=True,
        )
    return summary, ToolResult(history_snippet=summary), None


def schema() -> ToolSchema:
    return ToolSchema(
        name="judgment_finalize",
        description=(
            "Internal tool for the judgment-point Playbook only "
            "(judgment_on_event). Receives the judge node's structured output "
            "(dict), validates and applies the event reaction, and persists "
            "the resulting monologue + summary text to SAIMemory. Invalid "
            "items are rejected individually with warnings."
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
