"""Base classes and logging utilities for LLM clients."""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class UsageInfo:
    """Token usage information from LLM API response."""
    model: str
    input_tokens: int
    output_tokens: int
    timestamp: float  # time.time() when recorded
    cached_tokens: int = 0  # Tokens served FROM cache (cache read)
    cache_write_tokens: int = 0  # Tokens written TO cache (Anthropic: 1.25x cost for 5m, 2x for 1h)
    cache_ttl: str = ""  # Cache TTL used for this request ("5m", "1h", or "" if no cache)

# LLM logging is now handled by logging_config module
# Import convenience functions for backward compatibility
try:
    from saiverse.logging_config import log_llm_request, log_llm_response, get_llm_logger
except ImportError:
    # Fallback if logging_config not available (e.g., standalone script usage)
    def log_llm_request(*args, **kwargs): pass
    def log_llm_response(*args, **kwargs): pass
    def get_llm_logger(): return logging.getLogger("saiverse.llm")


class LLMClient:
    """Base class for LLM clients."""

    def __init__(
        self,
        supports_images: bool = False,
        supports_audio: bool = False,
        supports_video: bool = False,
    ) -> None:
        self._latest_reasoning: List[Dict[str, str]] = []
        self._latest_reasoning_details: Any = None
        self._latest_attachments: List[Dict[str, Any]] = []
        self._latest_tool_detection: Dict[str, Any] | None = None
        self._latest_usage: Optional[UsageInfo] = None
        self.supports_images = supports_images
        self.supports_audio = supports_audio
        self.supports_video = supports_video
        self.model: str = ""  # Set by subclasses (API model name)
        self.config_key: str = ""  # Config file key for pricing lookup

    def _inject_unsupported_media_summaries(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """If this client lacks supports_audio / supports_video, append summary
        text notes for any audio/video media in message metadata.

        Returns a new message list with text content modified for affected messages.
        For Gemini (which natively handles audio/video), this is a no-op.
        """
        if self.supports_audio and self.supports_video:
            return messages

        from .utils import inject_audio_video_summaries
        from saiverse.media_utils import iter_audio_media, iter_video_media

        new_msgs: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                new_msgs.append(msg)
                continue
            metadata = msg.get("metadata")
            if not isinstance(metadata, dict):
                new_msgs.append(msg)
                continue

            audio_items = iter_audio_media(metadata) if not self.supports_audio else []
            video_items = iter_video_media(metadata) if not self.supports_video else []
            if not audio_items and not video_items:
                new_msgs.append(msg)
                continue

            content = msg.get("content", "")
            if not isinstance(content, str):
                # Complex content (e.g. tool_calls); skip injection rather than risk
                # breaking the structure.
                new_msgs.append(msg)
                continue

            skip_audio = bool(metadata.get("__skip_audio_summary__"))
            skip_video = bool(metadata.get("__skip_video_summary__"))
            filtered_media = list(audio_items) + list(video_items)
            new_text = inject_audio_video_summaries(
                {"media": filtered_media}, content,
                skip_audio=skip_audio, skip_video=skip_video,
            )
            new_msg = dict(msg)
            new_msg["content"] = new_text
            new_msgs.append(new_msg)
        return new_msgs

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        response_schema: Dict[str, Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str | Dict[str, Any]:
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

    def _store_reasoning_details(self, details: Any) -> None:
        self._latest_reasoning_details = details

    def consume_reasoning_details(self) -> Any:
        details = self._latest_reasoning_details
        self._latest_reasoning_details = None
        return details

    def _store_tool_detection(self, result: Dict[str, Any] | None) -> None:
        """Store tool detection result for later retrieval."""
        self._latest_tool_detection = result

    def consume_tool_detection(self) -> Dict[str, Any] | None:
        """Retrieve and clear the latest tool detection result.

        Returns:
            Dict with keys:
                - type: "text" | "tool_call" | "both"
                - content: Generated text (if type is "text" or "both")
                - tool_name: Tool name (if type is "tool_call" or "both")
                - tool_args: Tool arguments dict (if type is "tool_call" or "both")
            Or None if no tool detection was performed.
        """
        result = self._latest_tool_detection
        self._latest_tool_detection = None
        return result

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

    def _store_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str | None = None,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_ttl: str = "",
    ) -> None:
        """Store token usage information for later retrieval.

        Args:
            input_tokens: Number of input/prompt tokens (total)
            output_tokens: Number of output/completion tokens
            model: Model ID (uses self.config_key or self.model if not provided)
            cached_tokens: Number of tokens served FROM cache (cache read)
            cache_write_tokens: Number of tokens written TO cache
            cache_ttl: Cache TTL used ("5m", "1h") - affects write cost calculation
        """
        # Prefer config_key for pricing lookup, fall back to model
        import logging
        logging.debug("[DEBUG] _store_usage: model param=%s, self.config_key=%s, self.model=%s",
                    model, self.config_key, self.model)
        model_for_pricing = model or self.config_key or self.model
        logging.debug("[DEBUG] _store_usage: model_for_pricing=%s, cached_tokens=%s, cache_write_tokens=%s",
                    model_for_pricing, cached_tokens, cache_write_tokens)
        self._latest_usage = UsageInfo(
            model=model_for_pricing,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=time.time(),
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_ttl=cache_ttl,
        )

    def consume_usage(self) -> Optional[UsageInfo]:
        """Retrieve and clear the latest token usage information.

        Returns:
            UsageInfo with model, input_tokens, output_tokens, timestamp
            Or None if no usage was recorded.
        """
        usage = self._latest_usage
        self._latest_usage = None
        return usage


class IncompleteStreamError(RuntimeError):
    """Raised when a streamed response ends without a completion signal."""


class EmptyResponseError(RuntimeError):
    """Raised when LLM returns an empty response (no text or function call)."""


__all__ = ["LLMClient", "UsageInfo", "log_llm_request", "log_llm_response", "get_llm_logger", "IncompleteStreamError", "EmptyResponseError"]
