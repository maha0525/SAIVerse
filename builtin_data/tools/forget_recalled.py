"""Clear recalled memories from working memory."""
from __future__ import annotations

import logging
from typing import Optional

from tools.context import get_active_persona_id, get_active_manager
from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)


def forget_recalled(source_id: Optional[str] = None) -> str:
    """Forget recalled memories from working memory.

    If source_id is provided, only that specific memory is forgotten.
    Otherwise, all recalled memories are cleared.

    Args:
        source_id: Specific memory ID to forget (optional).

    Returns:
        Confirmation message.
    """
    persona_id = get_active_persona_id()
    if not persona_id:
        raise RuntimeError("Active persona is not set")

    manager = get_active_manager()
    if not manager:
        raise RuntimeError("Manager reference is not available")

    persona = manager.all_personas.get(persona_id)
    if not persona:
        raise RuntimeError(f"Persona {persona_id} not found in manager")

    sai_mem = getattr(persona, "sai_memory", None)
    if not sai_mem or not sai_mem.is_ready():
        raise RuntimeError("SAIMemory is not available")

    if source_id:
        removed = sai_mem.remove_recalled_id(source_id)
        if removed:
            return f"記憶を忘れました: {source_id}"
        else:
            return f"指定された記憶はワーキングメモリにありません: {source_id}"
    else:
        count = sai_mem.clear_recalled_ids()
        if count > 0:
            return f"ワーキングメモリから{count}件の記憶をクリアしました。"
        else:
            return "ワーキングメモリに保持されている記憶はありません。"


def schema() -> ToolSchema:
    return ToolSchema(
        name="forget_recalled",
        description=(
            "想起した記憶をワーキングメモリから忘れます。"
            "source_idを指定すると特定の記憶だけ忘れます。"
            "省略するとすべての想起記憶をクリアします。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "忘れる記憶のID（省略すると全クリア）",
                },
            },
            "required": [],
        },
        result_type="string",
    )
