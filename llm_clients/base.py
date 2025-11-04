"""Base classes and logging utilities for LLM clients."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterator, List

RAW_LOG_FILE = os.getenv("SAIVERSE_RAW_LLM_LOG", "raw_llm_responses.txt")

raw_logger = logging.getLogger("saiverse.llm.raw")
raw_logger.setLevel(logging.DEBUG)
if not any(
    isinstance(handler, logging.FileHandler)
    and getattr(handler, "baseFilename", None) == os.path.abspath(RAW_LOG_FILE)
    for handler in raw_logger.handlers
):
    file_handler = logging.FileHandler(RAW_LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    raw_logger.addHandler(file_handler)


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

    def _store_reasoning(self, entries: List[Dict[str, str]] | None) -> None:
        self._latest_reasoning = entries or []

    def consume_reasoning(self) -> List[Dict[str, str]]:
        entries = self._latest_reasoning
        self._latest_reasoning = []
        return entries

    def _store_attachment(self, metadata: Dict[str, Any]) -> None:
        if metadata:
            self._latest_attachments.append(metadata)

    def consume_attachments(self) -> List[Dict[str, Any]]:
        attachments = self._latest_attachments
        self._latest_attachments = []
        return attachments


__all__ = ["LLMClient", "raw_logger", "RAW_LOG_FILE"]
