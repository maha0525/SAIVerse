"""note_search: アクティブなペルソナの Note 一覧から検索する。

シンプルな type フィルタ + タイトル部分一致を提供する。
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from database.session import SessionLocal
from saiverse.note_manager import (
    InvalidNoteTypeError,
    NoteManager,
)
from tools.context import get_active_persona_id
from tools.core import ToolResult, ToolSchema

_note_manager = NoteManager(session_factory=SessionLocal)


def note_search(
    query: Optional[str] = None,
    note_type: Optional[str] = None,
    include_inactive: bool = False,
) -> Tuple[str, ToolResult, None]:
    """List/search notes for the active persona."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    try:
        notes = _note_manager.list_for_persona(
            persona_id=persona_id,
            note_type=note_type,
            include_inactive=include_inactive,
        )
    except InvalidNoteTypeError as exc:
        raise RuntimeError(f"note_search failed: {exc}") from exc

    if query:
        q = query.lower()
        notes = [n for n in notes if q in (n.title or "").lower()]

    payload = [
        {
            "note_id": n.note_id,
            "title": n.title,
            "note_type": n.note_type,
            "description": n.description,
            "is_active": n.is_active,
            "last_opened_at": n.last_opened_at.isoformat() if n.last_opened_at else None,
            "closed_at": n.closed_at.isoformat() if n.closed_at else None,
        }
        for n in notes
    ]
    snippet = ToolResult(history_snippet=json.dumps(payload, ensure_ascii=False))
    if not notes:
        return "No notes found.", snippet, None
    return f"Found {len(notes)} note(s).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="note_search",
        description=(
            "Search the persona's notes. Filter by note_type (person/project/"
            "vocation) and/or by case-insensitive substring match on title. "
            "By default archived notes (is_active=false) are excluded."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive substring to match against the note title.",
                },
                "note_type": {
                    "type": "string",
                    "enum": ["person", "project", "vocation"],
                    "description": "Filter by note type.",
                },
                "include_inactive": {
                    "type": "boolean",
                    "description": "If true, include archived notes.",
                    "default": False,
                },
            },
        },
        result_type="string",
    )
