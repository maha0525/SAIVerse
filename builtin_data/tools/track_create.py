"""track_create: 新規 Track を作成する。

Cognitive model (Intent A v0.9 / Intent B v0.6) の Track 機構の入口。
作成された Track は unstarted 状態で、track_activate を呼ぶまで稼働しない。

Intent A v0.14 / Intent B v0.11 以降:
- create 自体は即時実行 (track_id を同 round で参照可能にするため)
- activate=True で指定された場合、activate 部分だけ Pulse 完了時に deferred
  実行される (Track 切替が Pulse 境界で起きることを保証)
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

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

_track_manager = TrackManager(session_factory=SessionLocal)


def track_create(
    track_type: str,
    title: Optional[str] = None,
    intent: Optional[str] = None,
    output_target: str = "none",
    is_persistent: bool = False,
    metadata: Optional[str] = None,
    activate: bool = False,
    entry_line_role: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Create a new action track for the active persona.

    activate=True を指定した場合の挙動 (Intent A v0.14 以降):
    - Pulse 内: track 自体は即時作成 (unstarted)、activate を Pulse 完了時に
      deferred 実行する。戻り値で「Pulse 完了時に自動 activate されます」と
      ペルソナに伝え、追加スペルを抑制する。
    - Pulse 外 (CLI / 直接テスト): create 直後に activate も即時実行 (旧挙動)。

    create が失敗すれば activate も走らない (immediate / deferred いずれも)。
    """
    persona_id = _require_persona_id()

    # Resolve entry_line_role: explicit arg > Handler default > 'main_line' fallback.
    # Inject into metadata JSON so the runtime can read it at Pulse start time
    # (Intent A v0.14, Intent B v0.11). Track-lifetime fixed value.
    resolved_entry_line_role = entry_line_role or resolve_default_entry_line_role(track_type)
    metadata = _inject_entry_line_role_into_metadata(metadata, resolved_entry_line_role)

    try:
        track_id = _track_manager.create(
            persona_id=persona_id,
            track_type=track_type,
            title=title,
            intent=intent,
            output_target=output_target,
            is_persistent=is_persistent,
            metadata=metadata,
        )
    except ValueError as exc:
        raise RuntimeError(f"track_create failed: {exc}") from exc

    final_status = "unstarted"
    activate_error: Optional[str] = None
    activate_queued = False

    if activate:
        pulse_ctx = get_pulse_context()
        if enqueue_or_warn(pulse_ctx, "activate", track_id=track_id):
            activate_queued = True
            # final_status stays 'unstarted'; runtime will set 'running' at flush
        else:
            # No PulseContext: fall back to immediate activate (CLI / test path).
            try:
                _track_manager.activate(track_id)
                final_status = "running"
            except Exception as exc:
                activate_error = f"{type(exc).__name__}: {exc}"

    snippet = ToolResult(
        history_snippet=json.dumps(
            {
                "track_id": track_id,
                "track_type": track_type,
                "title": title,
                "is_persistent": is_persistent,
                "status": final_status,
                "activate_queued": activate_queued,
                "activate_error": activate_error,
            },
            ensure_ascii=False,
        )
    )
    label = title or track_type
    if activate_error:
        message = (
            f"Created track '{label}' ({track_id[:8]}…, unstarted); "
            f"activate failed: {activate_error}."
        )
    elif activate_queued:
        message = (
            f"Created track '{label}' ({track_id[:8]}…, unstarted). "
            f"Activate scheduled for end of Pulse. {DEFERRED_NOTICE}"
        )
    elif activate:
        # Immediate activate path (no Pulse): preserved legacy behavior.
        message = f"Created and activated track '{label}' ({track_id[:8]}…, running)."
    else:
        message = f"Created track '{label}' ({track_id[:8]}…, unstarted)."
    return message, snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_create",
        description=(
            "Create a new action track for the persona. Tracks represent ongoing "
            "work contexts. The new track starts in 'unstarted' state and must "
            "be activated via track_activate to begin running. "
            "Common track_type values: 'autonomous' (project/task work), "
            "'social' (conversations with other personas, persistent), "
            "'user_conversation' (per-user conversation track, persistent), "
            "'external' (external communication). "
            "Use is_persistent=True only for permanent core tracks "
            "(social, user_conversation) — these cannot be completed or aborted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track_type": {
                    "type": "string",
                    "description": "Type of the track (autonomous / social / user_conversation / external / etc).",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title.",
                },
                "intent": {
                    "type": "string",
                    "description": "Natural language description of what this track aims to accomplish.",
                },
                "output_target": {
                    "type": "string",
                    "description": (
                        "Where speech from this track is delivered: "
                        "'none' (internal monologue only), 'building:current' "
                        "(everyone in current building), or 'external:<channel>:<address>'."
                    ),
                    "default": "none",
                },
                "is_persistent": {
                    "type": "boolean",
                    "description": "If true, the track cannot be completed/aborted. Permanent core tracks only.",
                    "default": False,
                },
                "metadata": {
                    "type": "string",
                    "description": "JSON string with additional metadata (e.g., target persona_id for social tracks).",
                },
                "activate": {
                    "type": "boolean",
                    "description": (
                        "If true, activate the newly created track immediately "
                        "(equivalent to track_create + track_activate in 1 spell). "
                        "On activate failure, the track remains unstarted; "
                        "the error is returned in the result so the next turn can react."
                    ),
                    "default": False,
                },
                "entry_line_role": {
                    "type": "string",
                    "description": (
                        "Which model/cache type drives the Track's pulse: 'main_line' "
                        "(heavyweight model, for tracks that talk to others — user, "
                        "social, external) or 'sub_line' (lightweight model, for "
                        "autonomous work that runs many short steps). "
                        "Defaults to the Handler's preset for the given track_type, "
                        "so explicit override is only needed for unusual cases."
                    ),
                    "enum": ["main_line", "sub_line"],
                },
            },
            "required": ["track_type"],
        },
        result_type="string",
        spell=True,
        spell_display_name="トラック作成",
    )


def _require_persona_id() -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    return persona_id


def _inject_entry_line_role_into_metadata(
    metadata: Optional[str],
    entry_line_role: str,
) -> str:
    """Merge ``entry_line_role`` into the metadata JSON string.

    The Track's entry-line role is fixed at create time and read by the
    runtime at Pulse start (Intent A v0.14, Intent B v0.11). It lives inside
    track_metadata JSON to avoid early schema normalization (Intent B v0.7).
    """
    if metadata:
        try:
            data = json.loads(metadata)
            if not isinstance(data, dict):
                data = {"_invalid_existing_metadata": data}
        except (TypeError, ValueError):
            data = {"_invalid_existing_metadata": metadata}
    else:
        data = {}
    data["entry_line_role"] = entry_line_role
    return json.dumps(data, ensure_ascii=False)
