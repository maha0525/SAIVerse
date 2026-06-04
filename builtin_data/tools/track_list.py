"""track_list: アクティブなペルソナの Track 一覧を取得する。"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional, Tuple

from database.session import SessionLocal
from saiverse.track_manager import TrackManager
from tools.context import get_active_manager, get_active_persona_id
from tools.core import ToolResult, ToolSchema

_track_manager = TrackManager(session_factory=SessionLocal)


def _format_relative(dt: Optional[datetime]) -> Optional[str]:
    """Return a coarse "N時間前" / "N分前" string for the given datetime.

    Used in the meta-judgment Track listing so the persona can see how stale
    each Track is at a glance — past monologues claiming "Track X is running
    steadily" cannot drown out the actual elapsed time when this is on the
    page.
    """
    if dt is None:
        return None
    diff_sec = int((datetime.now() - dt).total_seconds())
    if diff_sec < 0:
        return "未来"
    if diff_sec < 60:
        return f"{diff_sec}秒前"
    minutes = diff_sec // 60
    if minutes < 60:
        return f"{minutes}分前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}時間前"
    days = hours // 24
    if days < 30:
        return f"{days}日前"
    months = days // 30
    if months < 12:
        return f"{months}ヶ月前"
    years = days // 365
    return f"{years}年前"


def _resolve_last_message_times(persona_id: str, track_ids: List[str]) -> dict:
    """Look up MAX(messages.created_at) per track via the persona's SAIMemory.

    Returns an empty dict when the manager / persona / adapter is unavailable
    (e.g. tool exercised from CLI without a running manager) — the listing
    still works, just without the last_message_at field.
    """
    manager = get_active_manager()
    if manager is None or not track_ids:
        return {}
    persona = (getattr(manager, "personas", None) or {}).get(persona_id)
    if persona is None:
        return {}
    adapter = getattr(persona, "sai_memory", None)
    if adapter is None:
        return {}
    try:
        return adapter.get_track_last_message_times(track_ids)
    except Exception:
        return {}


def track_list(
    statuses: Optional[List[str]] = None,
    include_forgotten: bool = False,
) -> Tuple[str, ToolResult, None]:
    """List tracks for the active persona."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    tracks = _track_manager.list_for_persona(
        persona_id=persona_id,
        statuses=statuses,
        include_forgotten=include_forgotten,
    )
    last_msg_times = _resolve_last_message_times(
        persona_id, [t.track_id for t in tracks]
    )
    payload = []
    for t in tracks:
        last_dt = last_msg_times.get(t.track_id)
        short_id_str = f"t:{t.short_id}" if t.short_id is not None else None
        tasks = json.loads(t.tasks_json) if t.tasks_json else []
        tasks_done = sum(1 for tk in tasks if tk.get("done"))
        if tasks:
            task_lines = []
            for tk in tasks:
                mark = "[x]" if tk.get("done") else "[ ]"
                task_lines.append(f"{mark} {tk.get('title', '')}")
            tasks_summary = f"{tasks_done}/{len(tasks)}: " + "; ".join(task_lines)
        else:
            tasks_summary = None
        entry = {
            "short_id": short_id_str,
            "title": t.title,
            "track_type": t.track_type,
            "status": t.status,
            "is_persistent": t.is_persistent,
            "is_forgotten": t.is_forgotten,
            "intent": t.intent,
            "last_active_at": t.last_active_at.isoformat() if t.last_active_at else None,
            "last_message_at": last_dt.isoformat() if last_dt else None,
            "last_message_relative": _format_relative(last_dt),
        }
        if tasks_summary:
            entry["tasks"] = tasks_summary
        payload.append(entry)
    snippet = ToolResult(history_snippet=json.dumps(payload, ensure_ascii=False))
    if not tracks:
        return "No tracks found.", snippet, None
    return f"Found {len(tracks)} track(s).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="track_list",
        description=(
            "List the persona's tracks. By default, forgotten tracks are excluded. "
            "Use 'statuses' to filter by status (e.g., ['running', 'pending', 'waiting'])."
        ),
        parameters={
            "type": "object",
            "properties": {
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by status. Values: running, alert, pending, waiting, unstarted, completed, aborted.",
                },
                "include_forgotten": {
                    "type": "boolean",
                    "description": "If true, include tracks with is_forgotten=true.",
                    "default": False,
                },
            },
        },
        result_type="string",
        spell=True,
        spell_display_name="トラック一覧",
    )
