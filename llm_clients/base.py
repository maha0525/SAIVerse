"""Base classes and logging utilities for LLM clients."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterator, List

# LLM logging is now handled by logging_config module
# Import convenience functions for backward compatibility
try:
    from logging_config import log_llm_request, log_llm_response, get_llm_logger
except ImportError:
    # Fallback if logging_config not available (e.g., standalone script usage)
    def log_llm_request(*args, **kwargs): pass
    def log_llm_response(*args, **kwargs): pass
    def get_llm_logger(): return logging.getLogger("saiverse.llm")


class LLMClient:
    """Base class for LLM clients."""

    def __init__(self, supports_images: bool = False) -> None:
        self._latest_reasoning: List[Dict[str, str]] = []
        self._latest_attachments: List[Dict[str, Any]] = []
        self.supports_images = supports_images

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str:
        raise NotImplementedError

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        raise NotImplementedError

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """Generate response with tool call detection (do not execute tools).

        Returns:
            {"type": "text", "content": str} if no tool call
            {"type": "tool_call", "tool_name": str, "tool_args": dict} if tool call detected
        """
        raise NotImplementedError

    def _store_reasoning(self, entries: List[Dict[str, str]] | None) -> None:
        self._latest_reasoning = entries or []

    def consume_reasoning(self) -> List[Dict[str, str]]:
        entries = self._latest_reasoning
        self._latest_reasoning = []
        return entries

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Apply model-specific request parameters (subclasses may override)."""
        _ = parameters

    def _store_attachment(self, metadata: Dict[str, Any]) -> None:
        if metadata:
            self._latest_attachments.append(metadata)

    def consume_attachments(self) -> List[Dict[str, Any]]:
        attachments = self._latest_attachments
        self._latest_attachments = []
        return attachments


class IncompleteStreamError(RuntimeError):
    """Raised when a streamed response ends without a completion signal."""


class EmptyResponseError(RuntimeError):
    """Raised when LLM returns an empty response (no text or function call)."""


__all__ = ["LLMClient", "log_llm_request", "log_llm_response", "get_llm_logger", "IncompleteStreamError", "EmptyResponseError"]
