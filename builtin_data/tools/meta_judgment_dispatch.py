"""meta_judgment_dispatch: Apply the meta-judgment LLM's structured decision.

Phase 1.2 / 1.3 (Intent A v0.14, Intent B v0.11). The meta-judgment Playbook
emits a structured response of the form::

    { "thought": "...", "action": "continue"|"switch"|"wait"|"close",
      "switch_to_track_id": "...", "new_track_spec": {...},
      "notify_to_track": "...", "close_reason": "..." }

The LLM-node memorize step records this turn with ``scope='discardable'``
and stashes the inserted message id on ``state['_meta_judgment_message_id']``.
This dispatcher reads the structured output + that message id and:

- ``continue``: leave the discardable row untouched (it stays in the DB so
  meta-judgment-log retrieval can find it, but it's filtered out of regular
  context retrieval — that's the "branch turn discarded" property).
- ``switch``: enqueue the deferred Track ops (pause current running, then
  activate the target — possibly creating a new Track first), AND promote
  the discardable row to ``scope='committed'`` so it lives on as the "Track
  move 来歴" inside the destination Track's main cache.
- ``wait`` / ``close``: enqueue the appropriate Track op. (Track row stays
  discardable for now — when ``waiting`` resolves into a switch, the next
  meta-judgment turn carries the move-history; when closed, the persona's
  conscious flow continues without that turn in cache.)

The tool always returns a short result string the LLM can read in the same
Pulse. Track ops themselves are deferred (Intent A v0.14 / Intent B v0.11)
so the actual switch happens at Pulse completion.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple

from _track_common import (
    DEFERRED_NOTICE,
    enqueue_or_warn,
    get_pulse_context,
    resolve_default_entry_line_role,
)
from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

LOGGER = logging.getLogger("saiverse.tools.meta_judgment_dispatch")
_track_manager = TrackManager(session_factory=SessionLocal)


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


def _create_and_activate(spec: Dict[str, Any]) -> Optional[str]:
    """Create a new Track from new_track_spec and queue an activate op.

    Track creation runs immediately (so the new track_id can be referenced
    by the queued activate). The activate itself is enqueued like any other
    Track op so the switch lands at Pulse completion.
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
    notify_to_track: Optional[str] = None,
    close_reason: Optional[str] = None,
    judgment_message_id: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Apply the meta-judgment decision.

    Args mirror the meta-judgment Playbook's structured output.
    ``judgment_message_id`` is the discardable row id captured by the LLM
    node — required for ``switch`` to promote the row to committed.
    """
    if not get_active_persona_id():
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

    if action == "switch":
        target_id = switch_to_track_id
        if not target_id and isinstance(new_track_spec, dict):
            target_id = _create_and_activate(new_track_spec)
        if not target_id:
            raise RuntimeError(
                "meta_judgment_dispatch action='switch' requires switch_to_track_id "
                "or new_track_spec"
            )
        # Promote the discardable row to committed (Phase 1.3) so the move
        # history shows up in the destination Track's main cache.
        promoted = _promote_message_to_committed(judgment_message_id) \
            if judgment_message_id else False

        # Auto-pause whatever is running, then activate the target.
        # TrackManager.activate already does the auto-pause atomically — we
        # don't need a separate 'pause' op. Enqueueing both would risk the
        # current running being paused twice.
        enqueue_or_warn(pulse_ctx, "activate", track_id=target_id)

        result = {
            "action": "switch",
            "target_track_id": target_id,
            "promoted": promoted,
            "judgment_message_id": judgment_message_id,
        }
        return (
            f"Meta judgment: switch to track {target_id[:8]}…. {DEFERRED_NOTICE}",
            ToolResult(history_snippet=json.dumps(result, ensure_ascii=False)),
            None,
        )

    if action == "wait":
        # The current running Track moves to waiting at Pulse completion.
        # We use 'pause' here as the deferred-op vocabulary doesn't yet
        # include 'wait' — Phase 1.3 keeps wait routing minimal: pause
        # the current Track and let the next meta-judgment iteration pick
        # up the waiting decision once external events arrive.
        if pulse_ctx and switch_to_track_id:
            enqueue_or_warn(pulse_ctx, "pause", track_id=switch_to_track_id)
        return (
            f"Meta judgment: wait. Current Track will pause. {DEFERRED_NOTICE}",
            ToolResult(history_snippet=json.dumps(
                {"action": "wait", "target": switch_to_track_id},
                ensure_ascii=False,
            )),
            None,
        )

    if action == "close":
        target_id = switch_to_track_id
        if not target_id:
            raise RuntimeError(
                "meta_judgment_dispatch action='close' requires switch_to_track_id "
                "(the track to close)"
            )
        enqueue_or_warn(pulse_ctx, "complete", track_id=target_id)
        return (
            f"Meta judgment: close track {target_id[:8]}…. "
            f"reason={close_reason or '(unspecified)'}. {DEFERRED_NOTICE}",
            ToolResult(history_snippet=json.dumps(
                {"action": "close", "target": target_id, "reason": close_reason},
                ensure_ascii=False,
            )),
            None,
        )

    raise RuntimeError(
        f"meta_judgment_dispatch: unknown action '{action}'. "
        f"Expected one of: continue, switch, wait, close."
    )


def schema() -> ToolSchema:
    return ToolSchema(
        name="meta_judgment_dispatch",
        description=(
            "Apply the structured decision emitted by the meta-judgment LLM "
            "(continue / switch / wait / close). Phase 1.2/1.3."
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["continue", "switch", "wait", "close"],
                    "description": "Decision from the meta-judgment node.",
                },
                "switch_to_track_id": {
                    "type": "string",
                    "description": "Target Track id (switch / wait / close).",
                },
                "new_track_spec": {
                    "type": "object",
                    "description": "Spec for creating a new Track when switching.",
                },
                "notify_to_track": {
                    "type": "string",
                    "description": "Optional notice text to inject into the current Track on continue.",
                },
                "close_reason": {
                    "type": "string",
                    "description": "Optional reason text when action='close'.",
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
