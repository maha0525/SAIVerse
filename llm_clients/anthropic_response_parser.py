"""Anthropic response parsing helpers."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from anthropic.types import Message, TextBlock, ToolUseBlock


def _extract_text_from_response(message: Message) -> str:
    texts: List[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            texts.append(block.text)
        elif hasattr(block, "text"):
            texts.append(block.text)
    return "".join(texts)


def _extract_thinking_from_response(message: Message) -> List[Dict[str, str]]:
    reasoning_entries: List[Dict[str, str]] = []
    thinking_idx = 0
    for block in message.content:
        if getattr(block, "type", None) == "thinking":
            thinking_text = getattr(block, "thinking", "")
            if thinking_text and thinking_text.strip():
                thinking_idx += 1
                reasoning_entries.append({"title": f"Thought {thinking_idx}", "text": thinking_text.strip()})
    return reasoning_entries


def _extract_tool_use_from_response(message: Message) -> Optional[Dict[str, Any]]:
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            return {"id": block.id, "name": block.name, "arguments": block.input}
        if hasattr(block, "type") and getattr(block, "type", None) == "tool_use":
            return {
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "arguments": getattr(block, "input", {}),
            }
    return None
