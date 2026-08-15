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

import json
import logging
from typing import Any, Dict, Tuple, Union

from tools.context import (
    get_active_llm_messages,
    get_active_manager,
    get_active_persona_id,
    get_active_pulse_context,
    get_auto_mode,
    get_event_callback,
)
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

# `nested_subline_spell.md §6` 深さ制限。stack の depth でカウント:
# - メインライン Pulse 起動時に 1 frame push (root main_line)
# - `/run_playbook` ごとに 1 frame push される
# - つまり stack length = 5 → 深さ 4 (= 4 段の `/run_playbook` 入れ子) は許容
# - stack length = 6 になる起動 (= 5 段の `/run_playbook`) は拒否
_MAX_LINE_STACK_DEPTH = 5


def run_playbook(name: str) -> Union[str, Tuple[str, Dict[str, Any]]]:
    """Run a Playbook as a sub-line and return its `report_to_parent`.

    Args:
        name: Name of the Playbook to execute. Must be `router_callable=true`
              and present in the system prompt's "Playbook 一覧" section.

    Returns:
        Normal case: ``(report_text, metadata)`` tuple — the
        ``report_to_parent`` string plus a metadata dict that may carry
        ``{"media": [...]}`` so the parent line's next LLM round can attach
        sub-playbook media (image generation results, generated documents,
        etc.) as multimodal content.
        When the sub-playbook produced no media, the metadata dict is empty
        ``{}`` (still a tuple form for consistency).

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
        return _not_found_message(name, persona_id, building_id)

    if not _is_router_callable(playbook):
        return (
            f"[run_playbook error] Playbook '{name}' is not callable from spell "
            f"(router_callable=false). Internal sub_play only."
        )

    credential_error = _check_required_credentials(playbook, persona_id)
    if credential_error is not None:
        return credential_error

    permission_error = _check_playbook_permission(
        sea_runtime,
        manager,
        playbook,
        persona_obj,
        pulse_ctx,
    )
    if permission_error is not None:
        return permission_error

    # Build a minimal parent_state. Sub-line execution will:
    # - copy parent_state["_messages"] as base_messages (= snapshot of caller's
    #   LLM messages, captured by spell loop via persona_context(llm_messages=...))
    # - share parent_state["_pulse_context"] reference for line stack management
    # - write `report_to_parent` into parent_state on completion (output_schema-driven)
    #
    # The snapshot lets the sub-line inherit the parent line's actual
    # conversation context (intent A v0.14 §"子ラインは分岐であって独立ではない").
    # When invoked outside a spell loop (CLI / direct call), ``parent_messages``
    # is None — fall back to an empty list so the sub-line still runs but
    # without parent context.
    parent_messages = get_active_llm_messages() or []
    parent_state: dict = {
        "_messages": list(parent_messages),  # snapshot copy, never share reference
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
            auto_mode=get_auto_mode(),  # 呼び出し元 Pulse の実値を継承 (auto の子は auto)
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
        # Load-time PlaybookSchema validator enforces the report contract for
        # can_run_as_child=true Playbooks, so this branch means runtime nodes
        # failed to populate state['report_to_parent'] even though a static
        # path exists.
        LOGGER.warning(
            "[run_playbook] Sub-line '%s' completed without state['report_to_parent'] "
            "despite a static contract. Likely cause: an LLM/tool node failed to "
            "populate the expected field at runtime.",
            name,
        )
        return (
            f"[run_playbook] Playbook '{name}' completed but produced no report_to_parent."
        )

    # Forward sub-playbook media (image / file / etc.) to the parent line so
    # the spell loop can attach them to the next LLM round's messages.
    # The sub-playbook surfaces media via its tool nodes' metadata output_keys
    # (e.g. generate_image returns metadata={"media": [...]} which propagates
    # to parent_state["metadata"] via the playbook's output_schema).
    forwarded_metadata: Dict[str, Any] = {}
    sub_metadata = parent_state.get("metadata")
    if isinstance(sub_metadata, dict):
        sub_media = sub_metadata.get("media")
        if isinstance(sub_media, list) and sub_media:
            forwarded_metadata["media"] = list(sub_media)
            LOGGER.info(
                "[run_playbook] Forwarding %d media item(s) from sub-playbook '%s' to parent line",
                len(sub_media), name,
            )

    return str(report).strip(), forwarded_metadata


def _is_router_callable(playbook: object) -> bool:
    """Return True if the playbook is allowed to be invoked from spell."""
    val = getattr(playbook, "router_callable", None)
    if val is None:
        # PlaybookSchema may also expose externally_callable / spell_invokable
        # (rename candidates per nested_subline_spell.md §9). For now only
        # router_callable is canonical.
        return False
    return bool(val)


def _check_required_credentials(playbook: object, persona_id: str) -> str | None:
    from builtin_data.tools.list_available_playbooks import (
        has_required_playbook_credentials,
    )

    required = getattr(playbook, "required_credentials", None)
    if has_required_playbook_credentials(required, persona_id):
        return None
    LOGGER.warning(
        "[run_playbook] Required credentials unavailable: persona=%s playbook=%s required=%s",
        persona_id,
        getattr(playbook, "name", "<unknown>"),
        required,
    )
    return (
        f"[run_playbook error] Playbook '{getattr(playbook, 'name', '<unknown>')}' "
        "requires credentials that are not configured for this persona."
    )


def _check_playbook_permission(
    sea_runtime: object,
    manager: object,
    playbook: object,
    persona_obj: object,
    pulse_ctx: object,
) -> str | None:
    """Recheck city permission at the exact ``run_playbook`` execution point."""
    city_id = getattr(manager, "city_id", None)
    get_permission = getattr(sea_runtime, "_get_playbook_permission", None)
    if city_id is None or not callable(get_permission):
        return None

    playbook_name = str(getattr(playbook, "name", ""))
    permission = get_permission(city_id, playbook_name)
    LOGGER.info(
        "[run_playbook] Execute-time permission: playbook=%s city=%s permission=%s",
        playbook_name,
        city_id,
        permission,
    )
    if permission in {"blocked", "user_only"}:
        return (
            f"[run_playbook error] Playbook '{playbook_name}' is not available "
            f"(permission: {permission})."
        )
    if permission != "ask_every_time":
        return None

    active_line = pulse_ctx.current_line()
    active_aspect = getattr(active_line, "aspect", None)
    from sea.pulse_context import Aspect

    event_callback = get_event_callback()
    request_permission = getattr(sea_runtime, "_request_playbook_permission", None)
    if (
        active_aspect is not Aspect.CONVERSATION
        or event_callback is None
        or not callable(request_permission)
    ):
        return (
            f"[run_playbook error] Playbook '{playbook_name}' requires explicit "
            "user permission and cannot be started from this execution context."
        )

    response = request_permission(playbook_name, persona_obj, event_callback)
    if response == "always_allow":
        set_permission = getattr(sea_runtime, "_set_playbook_permission", None)
        if callable(set_permission):
            set_permission(city_id, playbook_name, "auto_allow")
        return None
    if response == "allow":
        return None
    if response == "never_use":
        set_permission = getattr(sea_runtime, "_set_playbook_permission", None)
        if callable(set_permission):
            set_permission(city_id, playbook_name, "user_only")
    reason = "timed out" if response == "timeout" else "was denied"
    return (
        f"[run_playbook error] User permission for Playbook '{playbook_name}' "
        f"{reason}."
    )


def _not_found_message(
    requested_name: str,
    persona_id: str,
    building_id: str,
) -> str:
    """List only playbooks authorized by the canonical availability gate."""
    try:
        from builtin_data.tools.list_available_playbooks import (
            list_available_playbooks,
        )

        available = json.loads(
            list_available_playbooks(
                persona_id=persona_id,
                building_id=building_id,
            )
        )
        names = sorted(
            item["name"]
            for item in available
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        listing = ", ".join(names) if names else "(none discovered)"
        return (
            f"[run_playbook error] Playbook '{requested_name}' not found. "
            f"Available authorized playbooks: {listing}"
        )
    except Exception:
        LOGGER.warning(
            "[run_playbook] Failed to build authorized not-found candidates",
            exc_info=True,
        )
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
