from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from saiverse_memory import SAIMemoryAdapter
from sai_memory.memory.storage import get_messages_last
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolResult, ToolSchema


DEFAULT_RANGE_BEFORE = 8
DEFAULT_RANGE_AFTER = 0


def switch_active_thread(
    target_thread: str,
    summary: Optional[str] = None,
    range_before: Optional[int] = None,
    persona_path: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Switch persona focus to another thread while carrying recent context metadata."""

    persona_id = _resolve_persona_id()
    adapter = _build_adapter(persona_id, persona_path)
    if not adapter.is_ready():
        adapter.close()
        raise RuntimeError(f"SAIMemory adapter is not ready for persona '{persona_id}'")

    try:
        target_full, target_suffix = _normalise_thread_id(persona_id, target_thread)
        origin_full = _resolve_origin_thread(adapter, persona_id)
        if target_suffix == "":
            raise ValueError("target_thread must not be empty")
        if target_full == origin_full:
            raise ValueError(f"Target thread '{target_full}' is already active.")

        anchor_msg = _resolve_anchor_message(adapter, origin_full)
        link_range_before = max(0, int(range_before if range_before is not None else DEFAULT_RANGE_BEFORE))
        link_range_after = DEFAULT_RANGE_AFTER

        iso_timestamp = _normalise_timestamp(None)
        note = summary.strip() if summary and summary.strip() else f"[thread switch] moved from {origin_full}"

        message_payload = {
            "role": "system",
            "content": note,
            "timestamp": iso_timestamp,
            "embedding_chunks": 0,
        }
        if anchor_msg is not None:
            message_payload["metadata"] = {
                "other_thread_messages": [
                    {
                        "thread_id": origin_full,
                        "message_id": anchor_msg.id,
                        "range_before": link_range_before,
                        "range_after": link_range_after,
                    }
                ]
            }
        adapter.append_persona_message(message_payload, thread_suffix=target_suffix)

        _write_active_state(adapter.persona_dir / adapter._ACTIVE_STATE_FILENAME, target_suffix, iso_timestamp)

        if anchor_msg is not None:
            result = (
                f"Switched active thread to {target_full}. "
                f"Linked origin {origin_full} @ {anchor_msg.id} "
                f"(range_before={link_range_before}, range_after={link_range_after})."
            )
            snippet_payload = {
                "source_thread": origin_full,
                "target_thread": target_full,
                "anchor_message": anchor_msg.id,
                "range_before": link_range_before,
                "range_after": link_range_after,
                "timestamp": iso_timestamp,
            }
        else:
            result = f"Switched active thread to {target_full}. No origin messages available to link."
            raise ValueError(
                f"Origin thread '{origin_full}' has no messages to reference."
            )
        snippet = ToolResult(history_snippet=json.dumps(snippet_payload, ensure_ascii=False))
        return result, snippet, None
    finally:
        adapter.close()


def schema() -> ToolSchema:
    return ToolSchema(
        name="switch_active_thread",
        description=(
            "Record a persona thread switch by inserting a system message that references "
            "messages from another thread, and update the active thread pointer."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_thread": {
                    "type": "string",
                    "description": (
                        "New thread identifier or suffix (e.g. conversation UUID). "
                        "If only the suffix is supplied, it is combined as '<persona_id>:<suffix>'."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "Optional narrative for the inserted system message.",
                },
                "range_before": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "How many preceding messages from the current thread you want to reference together with the anchor."
                    ),
                },
            },
            "required": ["target_thread"],
        },
        result_type="string",
    )


def _build_adapter(persona_id: str, persona_path: Optional[str]) -> SAIMemoryAdapter:
    if persona_path:
        persona_dir = Path(persona_path).expanduser()
        if not persona_dir.is_absolute():
            persona_dir = Path.home() / ".saiverse" / "personas" / persona_path
        persona_dir = persona_dir.resolve()
    else:
        ctx_path = get_active_persona_path()
        persona_dir = ctx_path if ctx_path else None
    return SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)


def _normalise_thread_id(persona_id: str, value: str) -> Tuple[str, str]:
    token = value.strip()
    if not token:
        raise ValueError("Thread identifier must not be blank")
    if ":" in token:
        prefix, suffix = token.split(":", 1)
        if not suffix:
            raise ValueError("Thread identifier suffix must not be blank")
        if prefix != persona_id:
            raise ValueError(f"Thread prefix '{prefix}' does not match persona '{persona_id}'")
        return token, suffix
    return f"{persona_id}:{token}", token


def _resolve_origin_thread(adapter: SAIMemoryAdapter, persona_id: str) -> str:
    suffix = adapter._active_persona_suffix() or adapter._PERSONA_THREAD_SUFFIX  # type: ignore[attr-defined]
    return f"{persona_id}:{suffix}"


def _resolve_anchor_message(adapter: SAIMemoryAdapter, thread_id: str):
    with adapter._db_lock:  # type: ignore[attr-defined]
        latest = get_messages_last(adapter.conn, thread_id, 1)
        if not latest:
            return None
        return latest[-1]


def _normalise_timestamp(ts: Optional[str]) -> str:
    if ts:
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError as exc:
            raise ValueError(f"timestamp is not a valid ISO-8601 string: {exc}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _write_active_state(path: Path, suffix: str, iso_timestamp: str) -> None:
    payload = {"active_thread_id": suffix, "updated_at": iso_timestamp}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_persona_id() -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise ValueError("Active persona context is not set. Use tools.context.persona_context().")
    return persona_id
