from __future__ import annotations

from typing import Optional

from saiverse_memory import SAIMemoryAdapter
from tools.context import get_active_persona_id, get_active_persona_path
from tools.defs import ToolSchema


def memory_recall(query: str, max_chars: int = 1200, topk: int = 4) -> str:
    """Recall relevant messages from SAIMemory for the active persona.

    - query: recall query text
    - max_chars: truncate output to this many characters
    - topk: number of recall seeds
    """

    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set (use tools.context.persona_context)")

    persona_dir = get_active_persona_path()
    adapter: Optional[SAIMemoryAdapter]
    try:
        adapter = SAIMemoryAdapter(persona_id, persona_dir=persona_dir, resource_id=persona_id)
    except Exception as exc:
        raise RuntimeError(f"Failed to init SAIMemory for {persona_id}: {exc}")

    if not adapter.is_ready():
        raise RuntimeError(f"SAIMemory not ready for {persona_id}")

    return adapter.recall_snippet(None, query_text=query, max_chars=max_chars, topk=topk) or "(no relevant memory)"


def schema() -> ToolSchema:
    return ToolSchema(
        name="memory_recall",
        description="Recall relevant past messages from SAIMemory for the active persona.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to recall"},
                "max_chars": {"type": "integer", "default": 1200},
                "topk": {"type": "integer", "default": 4}
            },
            "required": ["query"],
        },
        result_type="string",
    )

