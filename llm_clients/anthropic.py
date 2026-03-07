"""Native Anthropic Claude client with prompt caching support."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional

import anthropic
from anthropic import Anthropic
from anthropic.types import Message

from .base import EmptyResponseError, LLMClient, get_llm_logger
from .anthropic_request_builder import build_request_params
from .anthropic_response_parser import (
    _extract_text_from_response,
    _extract_thinking_from_response,
    _extract_tool_use_from_response,
)
from .anthropic_retry_policy import (
    _convert_to_llm_error,
    _is_content_policy_error,
    _should_retry,
)
from .exceptions import (
    AuthenticationError,
    EmptyResponseError as LLMEmptyResponseError,
    InvalidRequestError,
    LLMError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    SafetyFilterError,
    ServerError,
)


# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds

class AnthropicClient(LLMClient):
    """Native Anthropic Claude client with prompt caching support."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-5-20250514",
        config: Optional[Dict[str, Any]] = None,
        supports_images: bool = True,
    ) -> None:
        super().__init__(supports_images=supports_images)

        api_key = os.getenv("CLAUDE_API_KEY")
        if not api_key:
            raise AuthenticationError(
                "CLAUDE_API_KEY environment variable is not set.",
                user_message="Anthropic APIキーが設定されていません。管理者にお問い合わせください。"
            )

        self.client = Anthropic(api_key=api_key)
        self.model = model

        # Anthropic has a 5MB limit for images
        self.max_image_bytes = 5 * 1024 * 1024

        cfg = config or {}

        # Extended thinking configuration
        self._thinking_config: Optional[Dict[str, Any]] = None
        self._thinking_effort: Optional[str] = None  # "low", "medium", "high", "max"
        thinking_type = cfg.get("thinking_type") or os.getenv("ANTHROPIC_THINKING_TYPE")
        thinking_budget = cfg.get("thinking_budget") or os.getenv("ANTHROPIC_THINKING_BUDGET")
        thinking_effort = cfg.get("thinking_effort") or os.getenv("ANTHROPIC_THINKING_EFFORT")

        # Validate and store thinking_effort
        valid_efforts = ("low", "medium", "high", "max")
        if thinking_effort and thinking_effort in valid_efforts:
            self._thinking_effort = thinking_effort

        if thinking_budget:
            try:
                thinking_budget = int(thinking_budget)
                if thinking_budget <= 0:
                    logging.warning("Anthropic thinking_budget must be positive; ignoring value=%s", thinking_budget)
                    thinking_budget = None
            except (TypeError, ValueError):
                thinking_budget = None

        if thinking_type == "adaptive":
            # Adaptive thinking (Opus 4.6+): Claude decides when and how much to think
            # budget_tokens is not used with adaptive mode
            self._thinking_config = {"type": "adaptive"}
            logging.debug("[anthropic] Using adaptive thinking mode (effort=%s)", self._thinking_effort or "default")
        elif thinking_type or thinking_budget:
            # Manual thinking (legacy): explicit budget_tokens
            self._thinking_config = {}
            if thinking_type:
                self._thinking_config["type"] = thinking_type
            if thinking_budget:
                self._thinking_config["budget_tokens"] = thinking_budget
            self._thinking_config.setdefault("type", "enabled")

        # Max output tokens
        # Note: For manual thinking, max_tokens must be greater than thinking.budget_tokens
        # For adaptive thinking, a higher default is recommended since thinking tokens are dynamic
        self._max_tokens = 4096  # Default
        if thinking_type == "adaptive":
            self._max_tokens = 16000  # Higher default for adaptive thinking
        max_output = cfg.get("max_output_tokens") or os.getenv("ANTHROPIC_MAX_OUTPUT_TOKENS")
        if max_output:
            try:
                self._max_tokens = int(max_output)
            except (TypeError, ValueError):
                pass

        # Ensure max_tokens > thinking_budget when manual thinking is enabled
        if thinking_budget and self._max_tokens <= thinking_budget:
            # max_tokens must include both thinking budget and actual output
            self._max_tokens = thinking_budget + 4096
            logging.debug(
                "[anthropic] Adjusted max_tokens to %d (thinking_budget=%d + 4096)",
                self._max_tokens, thinking_budget
            )

        # Request parameters
        self._extra_params: Dict[str, Any] = {}
        if cfg.get("temperature") is not None:
            self._extra_params["temperature"] = cfg["temperature"]

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Configure model parameters from UI settings."""
        if not isinstance(parameters, dict):
            return
        allowed_params = {"temperature", "top_p", "top_k", "max_tokens"}
        valid_efforts = ("low", "medium", "high", "max")
        for key, value in parameters.items():
            # Handle thinking_effort specially (stored on instance, not in _extra_params)
            if key == "thinking_effort":
                if value in valid_efforts:
                    self._thinking_effort = value
                elif value is None:
                    self._thinking_effort = None
                continue
            if key not in allowed_params:
                continue
            if value is None:
                self._extra_params.pop(key, None)
            else:
                self._extra_params[key] = value

    def _store_usage_from_response(self, usage: Any, cache_ttl: str = "") -> None:
        """Extract and store usage information from Anthropic response."""
        if not usage:
            return

        # Anthropic returns:
        # - input_tokens: tokens AFTER the last cache breakpoint (uncached)
        # - cache_read_input_tokens: tokens read FROM cache
        # - cache_creation_input_tokens: tokens being written TO cache
        # Total input = input_tokens + cache_read + cache_creation
        raw_input = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

        # Calculate total input tokens
        total_input_tokens = raw_input + cache_read + cache_creation

        # cached_tokens = tokens read from cache (discounted rate)
        # cache_write_tokens = tokens written to cache (1.25x rate for 5m TTL)
        cached_tokens = cache_read
        cache_write_tokens = cache_creation

        logging.debug(
            "[anthropic] Usage: raw_input=%d, cache_read=%d, cache_write=%d, total_input=%d, output=%d",
            raw_input, cache_read, cache_write_tokens, total_input_tokens, output_tokens
        )

        self._store_usage(
            input_tokens=total_input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
            cached_tokens=cached_tokens,
            cache_ttl=cache_ttl,
        )

    def _execute_with_retry(
        self,
        callable: Any,
        context: str,
        is_stream: bool = False,
    ) -> Any:
        """Execute an Anthropic API operation with unified retry and error handling."""
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                return callable()
            except anthropic.BadRequestError as e:
                logging.error("[anthropic] Bad request: %s", e)
                if _is_content_policy_error(e):
                    raise SafetyFilterError(
                        f"Anthropic {context} content policy violation: {e}",
                        e,
                        user_message="入力内容がAnthropicのコンテンツポリシーによりブロックされました。入力内容を変更してお試しください。",
                    )
                raise InvalidRequestError(f"Anthropic {context} error: {e}", e)
            except Exception as e:
                last_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    label = "streaming " if is_stream else ""
                    logging.warning(
                        "[anthropic] Retryable %serror (attempt %d/%d): %s. Retrying in %.1fs...",
                        label,
                        attempt + 1,
                        MAX_RETRIES,
                        type(e).__name__,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("[anthropic] %s failed", context[:1].upper() + context[1:])
                raise _convert_to_llm_error(e, context)

        if last_error:
            raise _convert_to_llm_error(last_error, f"{context} after {MAX_RETRIES} retries")
        raise LLMEmptyResponseError(f"Anthropic {context} failed after {MAX_RETRIES} retries with no response")

    def parse_structured_response(
        self,
        response: Message,
        use_native_structured_output: bool,
    ) -> str | Dict[str, Any]:
        """Parse structured output response while preserving legacy return compatibility."""
        if use_native_structured_output:
            text = _extract_text_from_response(response)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                logging.warning("[anthropic] Failed to parse native structured output as JSON, returning raw text")
                return text

        tool_use = _extract_tool_use_from_response(response)
        if tool_use:
            return tool_use["arguments"]
        return _extract_text_from_response(response)

    def parse_tool_response(self, response: Message) -> Dict[str, Any]:
        """Parse tool mode response and store tool detection."""
        tool_use = _extract_tool_use_from_response(response)
        if tool_use:
            tool_call = {
                "type": "tool_call",
                "tool_name": tool_use["name"],
                "tool_args": tool_use["arguments"],
            }
            self._store_tool_detection(tool_call)
            return tool_call

        text = _extract_text_from_response(response)
        if not text.strip():
            logging.error(
                "[anthropic] Empty text response without tool call. "
                "Model returned empty content. stop_reason=%s",
                getattr(response, "stop_reason", None)
            )
            raise LLMEmptyResponseError("Anthropic returned empty response without tool call")
        text_response = {
            "type": "text",
            "content": text,
        }
        self._store_tool_detection(text_response)
        return text_response

    def parse_text_response(self, response: Message) -> str:
        """Parse normal text response and validate non-empty text."""
        text = _extract_text_from_response(response)
        if not text.strip():
            logging.error(
                "[anthropic] Empty text response. "
                "Model returned empty content. stop_reason=%s",
                getattr(response, "stop_reason", None)
            )
            raise LLMEmptyResponseError("Anthropic returned empty response")
        return text

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = True,
        cache_ttl: str = "5m",
        **_: Any,
    ) -> str | Dict[str, Any]:
        """Generate a response using Anthropic Claude API.

        Args:
            messages: Conversation messages
            tools: Tool specifications (optional)
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            enable_cache: Whether to enable prompt caching
            cache_ttl: Cache TTL ("5m" or "1h")

        Returns:
            str: Text response when no tools or response_schema
            Dict: Structured response or tool call result
        """
        self._store_reasoning([])

        build_result = build_request_params(
            messages=messages,
            tools=tools,
            response_schema=response_schema,
            temperature=temperature,
            enable_cache=enable_cache,
            cache_ttl=cache_ttl,
            model=self.model,
            max_tokens=self._max_tokens,
            extra_params=self._extra_params,
            thinking_config=self._thinking_config,
            thinking_effort=self._thinking_effort,
            supports_images=self.supports_images,
            max_image_bytes=self.max_image_bytes,
        )
        request_params = build_result["request_params"]
        use_tools = bool(build_result["use_tools"])
        use_native_structured_output = bool(build_result["use_native_structured_output"])

        if not request_params.get("messages"):
            logging.warning("[anthropic] No valid messages to send")
            return ""

        def _call() -> Any:
            get_llm_logger().debug(
                "[anthropic] Request: %s",
                json.dumps(request_params, indent=2, ensure_ascii=False, default=str),
            )
            result = self.client.messages.create(**request_params)
            get_llm_logger().debug("[anthropic] Response: %s", result.model_dump_json(indent=2))
            return result

        response = self._execute_with_retry(_call, "API call")

        # Store usage
        self._store_usage_from_response(response.usage, cache_ttl=cache_ttl)

        # Extract and store thinking content
        self._store_reasoning(_extract_thinking_from_response(response))

        if response_schema and not use_tools:
            return self.parse_structured_response(response, use_native_structured_output)

        # Handle tool mode
        if use_tools:
            return self.parse_tool_response(response)

        return self.parse_text_response(response)

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = True,
        cache_ttl: str = "5m",
        **_: Any,
    ) -> Iterator[str]:
        """Generate a streaming response using Anthropic Claude API.

        Args:
            messages: Conversation messages
            tools: Tool specifications (optional)
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            enable_cache: Whether to enable prompt caching
            cache_ttl: Cache TTL ("5m" or "1h")

        Yields:
            Text chunks as they are generated
        """
        self._store_reasoning([])

        # For structured output, use non-streaming generate
        if response_schema:
            result = self.generate(
                messages, tools, response_schema,
                temperature=temperature, enable_cache=enable_cache, cache_ttl=cache_ttl
            )
            if isinstance(result, dict):
                yield json.dumps(result, ensure_ascii=False)
            else:
                yield str(result)
            return

        build_result = build_request_params(
            messages=messages,
            tools=tools,
            response_schema=None,
            temperature=temperature,
            enable_cache=enable_cache,
            cache_ttl=cache_ttl,
            model=self.model,
            max_tokens=self._max_tokens,
            extra_params=self._extra_params,
            thinking_config=self._thinking_config,
            thinking_effort=self._thinking_effort,
            supports_images=self.supports_images,
            max_image_bytes=self.max_image_bytes,
        )
        request_params = build_result["request_params"]
        use_tools = bool(build_result["use_tools"])

        if not request_params.get("messages"):
            logging.warning("[anthropic] No valid messages to send")
            return

        def _stream_call() -> List[Any]:
            get_llm_logger().debug(
                "[anthropic] Stream request: %s",
                json.dumps(request_params, indent=2, ensure_ascii=False, default=str),
            )
            output_chunks: List[Any] = []
            with self.client.messages.stream(**request_params) as stream:
                final_message = None

                for event in stream:
                    if hasattr(event, "type"):
                        if event.type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if delta:
                                if hasattr(delta, "thinking"):
                                    output_chunks.append({"type": "thinking", "content": delta.thinking})
                                elif hasattr(delta, "text"):
                                    output_chunks.append(delta.text)
                        elif event.type == "message_stop":
                            final_message = stream.get_final_message()

                if final_message:
                    if final_message.usage:
                        self._store_usage_from_response(final_message.usage, cache_ttl=cache_ttl)
                    self._store_reasoning(_extract_thinking_from_response(final_message))

                if use_tools and final_message:
                    tool_use = _extract_tool_use_from_response(final_message)
                    if tool_use:
                        self._store_tool_detection({
                            "type": "tool_call",
                            "tool_name": tool_use["name"],
                            "tool_args": tool_use["arguments"],
                        })
            return output_chunks

        for chunk in self._execute_with_retry(_stream_call, "streaming API call", is_stream=True):
            yield chunk

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]] = None,
        *,
        temperature: float | None = None,
        enable_cache: bool = True,
        cache_ttl: str = "5m",
        **_: Any,
    ) -> Dict[str, Any]:
        """Generate response with tool call detection.

        Returns:
            {"type": "text", "content": str} if no tool call
            {"type": "tool_call", "tool_name": str, "tool_args": dict} if tool call detected
        """
        result = self.generate(
            messages, tools,
            temperature=temperature, enable_cache=enable_cache, cache_ttl=cache_ttl
        )

        if isinstance(result, dict):
            return result

        return {
            "type": "text",
            "content": result,
        }


__all__ = ["AnthropicClient"]
