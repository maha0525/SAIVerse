"""meta_judgment_dispatch: Apply the meta-judgment LLM's structured decision.

Phase 1.2 / 1.3 (Intent A v0.14, Intent B v0.11). The meta-judgment Playbook
emits a structured response of the form::

    { "thought": "...", "action": "continue"|"switch",
      "switch_to_track_id": "...", "new_track_spec": {...},
      "current_disposition": "pause"|"complete"|"abort",
      "notify_to_track": "...", "close_reason": "..." }

The action set was reduced to **continue / switch** (旧 wait/close は switch
のサブセットに整理 — wait = switch + target なし + disposition=pause、
close = switch + disposition=complete)。

The LLM-node memorize step records this turn with ``scope='discardable'``
and stashes the inserted message id on ``state['_meta_judgment_message_id']``.
This dispatcher reads the structured output + that message id and:

- ``continue``: leave the discardable row untouched (it stays in the DB so
  meta-judgment-log retrieval can find it, but it's filtered out of regular
  context retrieval — that's the "branch turn discarded" property).
- ``switch``:
    1. Apply ``current_disposition`` to the currently-running Track (pause /
       complete / abort, default pause).
    2. If ``switch_to_track_id`` or ``new_track_spec`` is given, activate
       the target. If both are omitted, the persona ends up with no
       running Track (= 旧 wait に相当)。
    3. Promote the discardable row to ``scope='committed'`` so it lives on
       as the "Track move 来歴" inside the destination Track's main cache.

Track ops are applied through ``apply_track_op`` so they get deferred onto
``PulseContext.deferred_track_ops`` when called inside a Pulse, and run
immediately otherwise (CLI / test / MetaLayer-spawned Playbook).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from _track_common import (
    DEFERRED_NOTICE,
    apply_track_op,
    get_pulse_context,
    resolve_default_entry_line_role,
)
from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.meta_judgment_dispatch")
_track_manager = TrackManager(session_factory=SessionLocal)

_VALID_DISPOSITIONS = {"pause", "complete", "abort"}


def _promote_message_to_committed(message_id: str) -> bool:
    """Update the row's scope from 'discardable' to 'committed' (Phase 1.3).

    Direct SQL update against the persona's SAIMemory DB. We don't go
    through the SAIMemoryAdapter because the active persona's adapter
    isn't visible from this tool call — the persona context only exposes
    persona_id / persona_dir. We resolve memory.db from persona_dir (same
    convention as the rest of SAIMemory).
    """
    if not message_id:
        return False

    from tools.context import get_active_persona_path

    persona_dir = get_active_persona_path()
    if persona_dir is None:
        LOGGER.warning(
            "[meta_judgment_dispatch] No persona_dir on context — cannot promote message_id=%s",
            message_id,
        )
        return False

    db_path = persona_dir / "memory.db"
    if not db_path.exists():
        LOGGER.warning(
            "[meta_judgment_dispatch] memory.db missing at %s — cannot promote message_id=%s",
            db_path, message_id,
        )
        return False

    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "UPDATE messages SET scope = 'committed' "
                "WHERE id = ? AND scope = 'discardable'",
                (message_id,),
            )
            conn.commit()
            LOGGER.info(
                "[meta_judgment_dispatch] Promoted message id=%s from 'discardable' to 'committed'",
                message_id,
            )
            return True
        finally:
            conn.close()
    except Exception:
        LOGGER.exception(
            "[meta_judgment_dispatch] Failed to promote message id=%s", message_id,
        )
        return False


def _create_track_from_spec(spec: Dict[str, Any]) -> Optional[str]:
    """Create a new Track from new_track_spec and return its id.

    The activate step is enqueued separately by the caller so the switch
    lands at Pulse completion alongside any disposition op.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        return None
    track_type = (spec.get("track_type") or "autonomous").strip()
    title = spec.get("title") or "(無題)"
    intent = spec.get("intent")
    output_target = spec.get("output_target") or "none"
    is_persistent = bool(spec.get("is_persistent", False))

    metadata = {
        "entry_line_role": spec.get("entry_line_role")
            or resolve_default_entry_line_role(track_type),
    }
    extra_md = spec.get("metadata")
    if isinstance(extra_md, dict):
        metadata.update(extra_md)

    new_track_id = _track_manager.create(
        persona_id=persona_id,
        track_type=track_type,
        title=title,
        intent=intent,
        output_target=output_target,
        is_persistent=is_persistent,
        metadata=json.dumps(metadata, ensure_ascii=False),
    )
    LOGGER.info(
        "[meta_judgment_dispatch] Created new Track %s (type=%s title=%s) for switch",
        new_track_id, track_type, title,
    )
    return new_track_id


def meta_judgment_dispatch(
    action: str,
    *,
    switch_to_track_id: Optional[str] = None,
    new_track_spec: Optional[Dict[str, Any]] = None,
    current_disposition: Optional[str] = None,
    notify_to_track: Optional[str] = None,
    close_reason: Optional[str] = None,
    judgment_message_id: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Apply the meta-judgment decision.

    Args mirror the meta-judgment Playbook's structured output.
    ``judgment_message_id`` is the discardable row id captured by the LLM
    node — required for ``switch`` to promote the row to committed.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )

    pulse_ctx = get_pulse_context()
    action = (action or "").strip().lower()

    if action == "continue":
        # Discardable row stays as-is. Nothing to enqueue. The meta-judgment
        # log will eventually inject this turn as 参考情報 next round.
        # Optional notify_to_track text is deferred to a future iteration —
        # the cleanest path is the existing inject_persona_event mechanism
        # but it requires building_id + addr resolution. For now return.
        result = {
            "action": "continue",
            "promoted": False,
            "message_id": judgment_message_id,
        }
        if notify_to_track:
            result["notify_to_track"] = notify_to_track[:200]
        return (
            "Meta judgment: continue. Branch turn kept as discardable.",
            ToolResult(history_snippet=json.dumps(result, ensure_ascii=False)),
            None,
        )

    if action != "switch":
        raise RuntimeError(
            f"meta_judgment_dispatch: unknown action '{action}'. "
            "Expected one of: continue, switch."
        )

    # ---- action == "switch" ----
    disposition = (current_disposition or "pause").strip().lower()
    if disposition not in _VALID_DISPOSITIONS:
        raise RuntimeError(
            f"meta_judgment_dispatch: invalid current_disposition "
            f"'{current_disposition}'. Expected one of: pause, complete, abort."
        )

    # Resolve target: either an existing track_id or a new one from the spec.
    target_id: Optional[str] = (switch_to_track_id or "").strip() or None
    if target_id is None and isinstance(new_track_spec, dict) and new_track_spec:
        target_id = _create_track_from_spec(new_track_spec)

    # Resolve the currently-running Track (None if persona has no running Track).
    running = _track_manager.get_running(persona_id)
    running_id = running.track_id if running is not None else None

    deferred_any = False
    applied: list[str] = []

    # 1. Apply current_disposition to running Track.
    #    Skip if there is no running Track. When disposition=pause AND we are
    #    going to activate a target, TrackManager.activate auto-pauses the
    #    running one — we skip the explicit pause op to avoid a double-pause.
    if running_id is not None:
        skip_pause = (disposition == "pause" and target_id is not None)
        if not skip_pause:
            op_result = apply_track_op(
                pulse_ctx, disposition,
                track_id=running_id, track_manager=_track_manager,
            )
            deferred_any = deferred_any or op_result.deferred
            applied.append(f"{disposition}({running_id[:8]}…)")

    # 2. Activate target if we have one.
    if target_id is not None:
        op_result = apply_track_op(
            pulse_ctx, "activate",
            track_id=target_id, track_manager=_track_manager,
        )
        deferred_any = deferred_any or op_result.deferred
        applied.append(f"activate({target_id[:8]}…)")

    # 3. Promote the discardable row to committed (Phase 1.3) so the move
    #    history shows up in the destination Track's main cache.
    promoted = (
        _promote_message_to_committed(judgment_message_id)
        if judgment_message_id else False
    )

    summary_parts = [f"disposition={disposition}"]
    if target_id:
        summary_parts.append(f"target={target_id[:8]}…")
    else:
        summary_parts.append("target=(no active track)")
    if disposition == "complete" and close_reason:
        summary_parts.append(f"reason={close_reason}")

    notice = DEFERRED_NOTICE if deferred_any else "Applied immediately."
    summary = "Meta judgment: switch [" + ", ".join(summary_parts) + f"]. {notice}"

    result_payload = {
        "action": "switch",
        "current_disposition": disposition,
        "target_track_id": target_id,
        "running_track_id": running_id,
        "applied_ops": applied,
        "promoted": promoted,
        "judgment_message_id": judgment_message_id,
        "deferred": deferred_any,
    }
    if disposition == "complete" and close_reason:
        result_payload["close_reason"] = close_reason

    return (
        summary,
        ToolResult(history_snippet=json.dumps(result_payload, ensure_ascii=False)),
        None,
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="meta_judgment_dispatch",
        description=(
            "Apply the structured decision emitted by the meta-judgment LLM "
            "(continue / switch). On switch, the currently-running Track is "
            "disposed of via current_disposition (pause/complete/abort), then "
            "switch_to_track_id or new_track_spec activates the next Track "
            "(both omitted = land on no active Track)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["continue", "switch"],
                    "description": "Decision from the meta-judgment node.",
                },
                "switch_to_track_id": {
                    "type": "string",
                    "description": "Existing Track id to activate on switch (omit for new spec or no-target switch).",
                },
                "new_track_spec": {
                    "type": "object",
                    "description": "Spec for creating a new Track when switching.",
                },
                "current_disposition": {
                    "type": "string",
                    "enum": ["pause", "complete", "abort"],
                    "description": "How to dispose of the currently-running Track on switch (default pause).",
                },
                "notify_to_track": {
                    "type": "string",
                    "description": "Optional notice text to inject into the current Track on continue.",
                },
                "close_reason": {
                    "type": "string",
                    "description": "Optional reason text when current_disposition='complete'.",
                },
                "judgment_message_id": {
                    "type": "string",
                    "description": "ID of the discardable meta-judgment row to promote on switch.",
                },
            },
            "required": ["action"],
        },
        result_type="string",
        spell=False,  # internal — Playbook-only
    )
