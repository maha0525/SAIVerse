"""note_create: 新規 Note を作成する。

Note は person / project / vocation の 3 種のみ (Intent A v0.6 確定)。
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


def note_create(
    title: str,
    note_type: str,
    description: Optional[str] = None,
    metadata: Optional[str] = None,
) -> Tuple[str, ToolResult, None]:
    """Create a new note for the active persona."""
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    try:
        note_id = _note_manager.create(
            persona_id=persona_id,
            title=title,
            note_type=note_type,
            description=description,
            metadata=metadata,
        )
    except (ValueError, InvalidNoteTypeError) as exc:
        raise RuntimeError(f"note_create failed: {exc}") from exc

    snippet = ToolResult(
        history_snippet=json.dumps(
            {"note_id": note_id, "title": title, "note_type": note_type},
            ensure_ascii=False,
        )
    )
    return f"Created {note_type} note '{title}' ({note_id[:8]}…).", snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="note_create",
        description=(
            "Create a new Note. Notes hold a 'cluster of interest' — collected "
            "Memopedia pages and conversation messages tied to a specific topic. "
            "Notes persist permanently as the persona's lasting assets. "
            "There are exactly three note_type values: "
            "'person' (per-counterpart relationship), "
            "'project' (time-bounded undertaking), "
            "'vocation' (permanent expertise/identity, keep these few). "
            "Avoid creating multiple vocation notes — consolidate know-how instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title of the note (e.g. counterpart name, project name, profession).",
                },
                "note_type": {
                    "type": "string",
                    "enum": ["person", "project", "vocation"],
                    "description": "One of person / project / vocation.",
                },
                "description": {
                    "type": "string",
                    "description": "Description of the note's purpose (optional).",
                },
                "metadata": {
                    "type": "string",
                    "description": "JSON string with additional metadata (e.g., target persona_id).",
                },
            },
            "required": ["title", "note_type"],
        },
        result_type="string",
        spell=True,
        spell_display_name="ノート作成",
    )
