"""run_playbook: Run a Playbook as a sub-line and return its `report_to_parent`.

Phase 3 段階 4-C 後の中核 Spell。メインライン (or 親サブライン) の LLM が
通常発話の中で `/run_playbook(name="...")` と書くと、指定された Playbook が
サブラインとして起動され、完了時に `report_to_parent` (string) が親に返る。

詳細仕様: docs/intent/persona_cognition/nested_subline_spell.md (v0.1, 2026-05-01)

主な仕様:

- **引数は Playbook 名のみ**。Playbook ごとの引数は呼ばれた側の最初の LLM
  ノードが構造化出力で決める (旧 router 方式の踏襲)。
- **戻り値は string** (= `report_to_parent`)。サブライン Playbook の `output_schema`
  に含まれる `report_to_parent` を取り出して返す。
- **router_callable=true 必須**。`router_callable=false` の Playbook は
  外部から呼べない (内部 sub_play 専用)。Spell は明示的にエラー文字列を返す。
- **深さ制限: 4 階層**。`PulseContext._line_stack` の深さで判定。
  メインライン = 深さ 1 (root frame)、最初の `/run_playbook` で 2、入れ子で 3, 4, 5。
  6 階層以上は拒否してエラー文字列を返す。
- **サブライン挙動**: `line="sub"` で起動 → 親 `_messages` のコピーをベースに
  軽量モデルで実行 → 完了時に親に report_to_parent を string で返す。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import (
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
)
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# `nested_subline_spell.md §6` 深さ制限。stack の depth でカウント:
# - メインライン Pulse 起動時に 1 frame push (root main_line)
# - `/run_playbook` ごとに 1 frame push される
# - つまり stack length = 5 → 深さ 4 (= 4 段の `/run_playbook` 入れ子) は許容
# - stack length = 6 になる起動 (= 5 段の `/run_playbook`) は拒否
_MAX_LINE_STACK_DEPTH = 5


def run_playbook(name: str) -> str:
    """Run a Playbook as a sub-line and return its `report_to_parent`.

    Args:
        name: Name of the Playbook to execute. Must be `router_callable=true`
              and present in the system prompt's "Playbook 一覧" section.

    Returns:
        The `report_to_parent` string produced by the sub-line Playbook.
        Error cases (Playbook not found, not callable, depth exceeded,
        sub-line failure) return an error message string so the parent
        line can continue execution.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        return "[run_playbook error] Active persona context is not set."

    manager = get_active_manager()
    if not manager:
        return "[run_playbook error] Manager reference is not available."

    pulse_ctx = get_active_pulse_context()
    if pulse_ctx is None:
        return (
            "[run_playbook error] No active PulseContext. "
            "/run_playbook must be invoked from within a Pulse."
        )

    # Depth check ─ MUST go before loading the playbook to avoid noise.
    current_depth = len(pulse_ctx._line_stack)
    if current_depth >= _MAX_LINE_STACK_DEPTH:
        msg = (
            f"[run_playbook error] Subline depth limit ({_MAX_LINE_STACK_DEPTH - 1}) "
            f"exceeded; cannot run playbook '{name}' (current line_stack depth={current_depth})."
        )
        LOGGER.warning("[run_playbook] %s", msg)
        return msg

    sea_runtime = getattr(manager, "sea_runtime", None)
    if sea_runtime is None:
        return "[run_playbook error] SEA runtime is not available on manager."

    personas = getattr(manager, "personas", {}) or {}
    persona_obj = personas.get(persona_id)
    if persona_obj is None:
        return f"[run_playbook error] Persona '{persona_id}' not found."

    building_id = getattr(persona_obj, "current_building_id", None)
    if not building_id:
        return f"[run_playbook error] Persona '{persona_id}' has no current building."

    # Load the playbook by name. _load_playbook_for resolves user_data → expansion → builtin
    # priority and returns a PlaybookSchema (or None).
    try:
        playbook = sea_runtime._load_playbook_for(name, persona_obj, building_id)
    except Exception as exc:
        LOGGER.exception("[run_playbook] Failed to load playbook '%s'", name)
        return f"[run_playbook error] Failed to load playbook '{name}': {type(exc).__name__}: {exc}"

    if playbook is None:
        return _not_found_message(sea_runtime, name)

    if not _is_router_callable(playbook):
        return (
            f"[run_playbook error] Playbook '{name}' is not callable from spell "
            f"(router_callable=false). Internal sub_play only."
        )

    # Build a minimal parent_state. Sub-line execution will:
    # - copy parent_state["_messages"] (= [] here, an empty list) as base_messages
    # - share parent_state["_pulse_context"] reference for line stack management
    # - write `report_to_parent` into parent_state on completion (output_schema-driven)
    #
    # NOTE: メインラインの会話履歴をサブラインに引き継ぐ MVP 経路はまだない。
    # 必要であれば spell loop 側に messages を contextvar 経由で渡す機構を追加する
    # が、現状は Playbook 内で必要な情報は input_schema 経由 / SAIMemory recall /
    # state.input から取得できる前提。
    parent_state: dict = {
        "_messages": [],
        "_pulse_context": pulse_ctx,
        "_pulse_id": pulse_ctx.pulse_id,
    }

    LOGGER.info(
        "[run_playbook] Spawning sub-line: persona=%s playbook=%s depth=%d→%d",
        persona_id, name, current_depth, current_depth + 1,
    )

    try:
        sea_runtime._run_playbook(
            playbook,
            persona_obj,
            building_id,
            user_input=None,
            auto_mode=False,
            record_history=True,
            parent_state=parent_state,
            line="sub",
            isolate_pulse_context=False,  # share parent PulseContext for line stack management
        )
    except Exception as exc:
        LOGGER.exception("[run_playbook] Sub-line execution failed for '%s'", name)
        return f"[run_playbook error] Sub-line failed for '{name}': {type(exc).__name__}: {exc}"

    report = parent_state.get("report_to_parent")
    if not report:
        LOGGER.warning(
            "[run_playbook] Sub-line '%s' completed without report_to_parent. "
            "Ensure the playbook's output_schema includes 'report_to_parent'.",
            name,
        )
        return (
            f"[run_playbook] Playbook '{name}' completed but produced no report_to_parent. "
            f"(Hint: include 'report_to_parent' in the playbook's output_schema.)"
        )

    return str(report).strip()


def _is_router_callable(playbook: object) -> bool:
    """Return True if the playbook is allowed to be invoked from spell."""
    val = getattr(playbook, "router_callable", None)
    if val is None:
        # PlaybookSchema may also expose externally_callable / spell_invokable
        # (rename candidates per nested_subline_spell.md §9). For now only
        # router_callable is canonical.
        return False
    return bool(val)


def _not_found_message(sea_runtime: object, requested_name: str) -> str:
    """Build an error message that lists available router-callable playbooks."""
    try:
        # Best-effort enumeration: grab all loaded playbooks from the runtime
        # cache plus DB. The exact API surface varies by sea_runtime
        # implementation; degrade gracefully when missing.
        cached = getattr(sea_runtime, "_playbook_cache", {}) or {}
        names = sorted(
            n for n, pb in cached.items() if _is_router_callable(pb)
        )
        if not names:
            # Fallback: try DB
            try:
                from database.session import SessionLocal
                from database.models import Playbook as PlaybookRow
                with SessionLocal() as db:
                    rows = db.query(PlaybookRow).filter(PlaybookRow.ROUTER_CALLABLE == 1).all()
                    names = sorted(r.NAME for r in rows)
            except Exception:
                names = []
        listing = ", ".join(names) if names else "(none discovered)"
        return (
            f"[run_playbook error] Playbook '{requested_name}' not found. "
            f"Available router_callable playbooks: {listing}"
        )
    except Exception:
        return f"[run_playbook error] Playbook '{requested_name}' not found."


def schema() -> ToolSchema:
    return ToolSchema(
        name="run_playbook",
        description=(
            "Run a Playbook as a sub-line and receive its report_to_parent (a "
            "string summary written by the sub-line). Use this when you need a "
            "specialized capability (memory research, deep web research, image "
            "generation, document creation, etc.) that the sub-line Playbook "
            "knows how to perform with structured LLM nodes / tools. "
            "Pass only the Playbook name; arguments are decided inside the "
            "called Playbook by its first LLM node based on the conversation "
            "context. Available Playbook names are listed in the 'Playbook 一覧' "
            "section of the system prompt (router_callable=true)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the Playbook to execute. Must be one of the "
                        "router_callable Playbooks listed in the system prompt."
                    ),
                },
            },
            "required": ["name"],
        },
        result_type="string",
        spell=True,
        spell_display_name="Playbook 起動",
    )


def _max_depth() -> int:
    """Expose the depth ceiling for tests."""
    return _MAX_LINE_STACK_DEPTH
