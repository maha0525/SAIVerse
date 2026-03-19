from __future__ import annotations

from unittest.mock import MagicMock

from llm_clients.anthropic_response_parser import (
    _extract_text_from_response,
    _extract_thinking_from_response,
    _extract_tool_use_from_response,
)


def test_extract_text_from_response_concatenates_text_blocks() -> None:
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "hello"
    another = MagicMock()
    another.type = "text"
    another.text = " world"

    message = MagicMock()
    message.content = [text_block, another]

    assert _extract_text_from_response(message) == "hello world"


def test_extract_thinking_from_response_returns_indexed_entries() -> None:
    thought = MagicMock()
    thought.type = "thinking"
    thought.thinking = "  reason  "

    message = MagicMock()
    message.content = [thought]

    assert _extract_thinking_from_response(message) == [{"title": "Thought 1", "text": "reason"}]


def test_extract_tool_use_from_response_extracts_tool_call() -> None:
    tool = MagicMock()
    tool.type = "tool_use"
    tool.id = "tool_1"
    tool.name = "Decision"
    tool.input = {"answer": "ok"}

    message = MagicMock()
    message.content = [tool]

    assert _extract_tool_use_from_response(message) == {
        "id": "tool_1",
        "name": "Decision",
        "arguments": {"answer": "ok"},
    }
