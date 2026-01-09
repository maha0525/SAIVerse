from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolResult, ToolSchema


def task_request_creation(
    summary: str,
    context: Optional[str] = None,
    priority: str = "normal",
) -> Tuple[str, ToolResult, None]:
    """Record a request for generating a new task via the dedicated creation module."""

    if not summary.strip():
        raise ValueError("summary must not be empty.")

    persona_id = _require_persona_id()
    persona_dir = _resolve_persona_dir()
    persona_dir.mkdir(parents=True, exist_ok=True)
    log_path = persona_dir / "task_requests.jsonl"

    entry = {
        "id": uuid.uuid4().hex,
        "persona_id": persona_id,
        "summary": summary.strip(),
        "context": context.strip() if context else None,
        "priority": priority,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with log_path.open("a", encoding="utf-8") as handler:
        handler.write(json.dumps(entry, ensure_ascii=False) + "\n")

    processed = False
    try:
        from persona.tasks.creation import TaskCreationProcessor

        processor = TaskCreationProcessor(persona_dir)
        processed_ids = processor.process_pending_requests()
        processed = entry["id"] in processed_ids
    except Exception as exc:  # pragma: no cover - logging only
        logging.exception("task_request_creation: processing failed: %s", exc)

    payload = {**entry, "processed": processed}
    snippet = ToolResult(history_snippet=json.dumps(payload, ensure_ascii=False))
    if processed:
        message = "Task creation request processed immediately."
    else:
        message = (
            "Task creation request queued. It will be processed by the creation module later."
        )
    return message, snippet, None


def schema() -> ToolSchema:
    return ToolSchema(
        name="task_request_creation",
        description=(
            "Record a request for generating a new task. The actual task creation should be "
            "handled by the dedicated creation module (e.g., Gemini)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short description of the desired task.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context or notes to guide task creation.",
                },
                "priority": {
                    "type": "string",
                    "default": "normal",
                    "description": "Relative priority (e.g., low, normal, high).",
                },
            },
            "required": ["summary"],
        },
        result_type="string",
    )


def _require_persona_id() -> str:
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError(
            "Active persona context is not set. Use tools.context.persona_context()."
        )
    return persona_id


def _resolve_persona_dir() -> Path:
    persona_path = get_active_persona_path()
    if persona_path is None:
        base = Path.home() / ".saiverse" / "personas"
        persona_id = _require_persona_id()
        return base / persona_id
    return persona_path
