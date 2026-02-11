"""Native Anthropic Claude client with prompt caching support."""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import anthropic
from anthropic import Anthropic
from anthropic.types import Message, TextBlock, ToolUseBlock

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm

from .base import EmptyResponseError, LLMClient, get_llm_logger
from .exceptions import (
    AuthenticationError,
    EmptyResponseError as LLMEmptyResponseError,
    InvalidRequestError,
    LLMError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    ServerError,
)
from .utils import content_to_text


# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds


def _is_rate_limit_error(err: Exception) -> bool:
    """Check if the error is a rate limit that should be retried."""
    if isinstance(err, anthropic.RateLimitError):
        return True
    msg = str(err).lower()
    return (
        "rate" in msg
        or "429" in msg
        or "quota" in msg
        or "overload" in msg
    )


def _is_server_error(err: Exception) -> bool:
    """Check if the error is a server error (5xx) that should be retried."""
    if isinstance(err, anthropic.APIStatusError):
        return err.status_code >= 500
    msg = str(err).lower()
    return "503" in msg or "502" in msg or "504" in msg or "unavailable" in msg


def _is_timeout_error(err: Exception) -> bool:
    """Check if the error is a timeout that should be retried."""
    if isinstance(err, anthropic.APITimeoutError):
        return True
    if isinstance(err, anthropic.APIConnectionError):
        return True
    msg = str(err).lower()
    return "timeout" in msg or "timed out" in msg


def _should_retry(err: Exception) -> bool:
    """Check if the error should trigger a retry."""
    return _is_rate_limit_error(err) or _is_server_error(err) or _is_timeout_error(err)


def _is_authentication_error(err: Exception) -> bool:
    """Check if the error is an authentication error."""
    if isinstance(err, anthropic.AuthenticationError):
        return True
    msg = str(err).lower()
    return "401" in msg or "403" in msg or "authentication" in msg or "invalid api key" in msg


def _is_payment_error(err: Exception) -> bool:
    """Check if the error is a payment/billing error (402)."""
    msg = str(err).lower()
    return "402" in msg or "payment required" in msg or "spend limit" in msg or "billing" in msg


def _convert_to_llm_error(err: Exception, context: str = "API call") -> LLMError:
    """Convert a generic exception to an appropriate LLMError subclass."""
    if _is_payment_error(err):
        return PaymentError(f"Anthropic {context} failed: payment required", err)
    elif _is_rate_limit_error(err):
        return RateLimitError(f"Anthropic {context} failed: rate limit exceeded", err)
    elif _is_timeout_error(err):
        return LLMTimeoutError(f"Anthropic {context} failed: timeout", err)
    elif _is_server_error(err):
        return ServerError(f"Anthropic {context} failed: server error", err)
    elif _is_authentication_error(err):
        return AuthenticationError(f"Anthropic {context} failed: authentication error", err)
    else:
        return LLMError(f"Anthropic {context} failed: {err}", err)


def _make_cache_control(cache_ttl: str = "5m") -> Dict[str, Any]:
    """Create cache_control dict with TTL."""
    return {"type": "ephemeral", "ttl": cache_ttl}


def _prepare_schema_for_native_output(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Prepare a JSON schema for Anthropic native structured output.

    Native structured output requires additionalProperties: false on all object types.
    This function deep-copies the schema and adds the constraint where missing.
    """
    import copy
    schema = copy.deepcopy(schema)

    def _fix_object(obj: Dict[str, Any]) -> None:
        if not isinstance(obj, dict):
            return
        if obj.get("type") == "object":
            obj.setdefault("additionalProperties", False)
            for prop in (obj.get("properties") or {}).values():
                _fix_object(prop)
        elif obj.get("type") == "array" and isinstance(obj.get("items"), dict):
            _fix_object(obj["items"])
        # Handle anyOf / allOf
        for key in ("anyOf", "allOf"):
            if key in obj:
                for item in obj[key]:
                    _fix_object(item)

    _fix_object(schema)
    return schema


def _prepare_anthropic_system(
    messages: List[Dict[str, Any]],
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extract system messages and prepare them for Anthropic API format.

    Returns:
        Tuple of (system_blocks, remaining_messages)
        system_blocks: List of content blocks for system parameter
        remaining_messages: Messages without system messages
    """
    system_blocks: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            remaining.append(msg)
            continue

        role = msg.get("role", "")
        if isinstance(role, str) and role.lower() == "host":
            role = "system"

        if role == "system":
            content = content_to_text(msg.get("content", ""))
            if content:
                system_blocks.append({
                    "type": "text",
                    "text": content,
                })
        else:
            remaining.append(msg)

    # Add cache_control to the last system block for efficient caching.
    # Note: Anthropic has no read-only cache mode. Placing breakpoints enables
    # both reads and writes. Removing them disables both entirely.
    if enable_cache and system_blocks:
        system_blocks[-1]["cache_control"] = _make_cache_control(cache_ttl)

    return system_blocks, remaining


def _prepare_anthropic_messages(
    messages: List[Dict[str, Any]],
    supports_images: bool = False,
    max_image_bytes: Optional[int] = None,
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    """
    Prepare messages for Anthropic API format.

    Args:
        messages: Raw message list (should not contain system messages)
        supports_images: Whether the model supports images
        max_image_bytes: Optional max bytes for images
        enable_cache: Whether to add cache_control markers
        cache_ttl: Cache TTL ("5m" or "1h")
    """
    prepared: List[Dict[str, Any]] = []
    # Track index of first dynamic content (realtime context, etc.)
    first_dynamic_index: Optional[int] = None

    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        if role == "system":
            # System messages should be extracted separately
            continue
        if role == "host":
            role = "user"

        content = msg.get("content", "")
        metadata = msg.get("metadata")

        # Detect realtime context (dynamic content that changes each request)
        is_dynamic = False
        if isinstance(metadata, dict):
            if metadata.get("__realtime_context__"):
                is_dynamic = True

        attachments = list(iter_image_media(metadata)) if metadata else []

        # Skip empty messages
        if not content and not attachments:
            continue

        # Convert content to content blocks
        content_blocks: List[Dict[str, Any]] = []

        # Handle text content
        text = content_to_text(content)
        if text:
            content_blocks.append({
                "type": "text",
                "text": text,
            })

        # Handle images for user messages
        if supports_images and role == "user" and attachments:
            for att in attachments:
                data, effective_mime = load_image_bytes_for_llm(
                    att["path"], att["mime_type"], max_bytes=max_image_bytes
                )
                if data and effective_mime:
                    b64 = base64.b64encode(data).decode("ascii")
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": effective_mime,
                            "data": b64,
                        },
                    })
                else:
                    logging.warning("Image file not found or unreadable: %s", att.get("uri") or att.get("path"))
                    content_blocks.append({
                        "type": "text",
                        "text": f"[画像: {att['uri']}]",
                    })
        elif attachments:
            # Non-image-supporting case: add text placeholders
            for att in attachments:
                content_blocks.append({
                    "type": "text",
                    "text": f"[画像: {att['uri']}]",
                })

        if content_blocks:
            prepared_index = len(prepared)
            prepared.append({
                "role": role,
                "content": content_blocks,
            })
            # Track first dynamic content index
            if is_dynamic and first_dynamic_index is None:
                first_dynamic_index = prepared_index

    # Add cache_control BEFORE dynamic content (realtime context).
    # Note: Anthropic has no read-only cache mode. Placing breakpoints enables
    # both reads and writes. Removing them disables both entirely.
    if enable_cache and prepared:
        if first_dynamic_index is not None and first_dynamic_index > 0:
            # Place breakpoint on the message BEFORE dynamic content
            cache_target_index = first_dynamic_index - 1
            logging.debug(
                "[anthropic] Placing cache breakpoint before dynamic content at index %d",
                cache_target_index
            )
        else:
            # No dynamic content found, use second-to-last message if available
            # (last message is typically the new user input)
            cache_target_index = len(prepared) - 2 if len(prepared) >= 2 else len(prepared) - 1

        if cache_target_index >= 0:
            target_msg = prepared[cache_target_index]
            if target_msg.get("content") and isinstance(target_msg["content"], list):
                target_msg["content"][-1]["cache_control"] = _make_cache_control(cache_ttl)

    return prepared


def _prepare_anthropic_tools(
    tools: List[Dict[str, Any]],
    enable_cache: bool = True,
    cache_ttl: str = "5m",
) -> List[Dict[str, Any]]:
    """
    Prepare tools for Anthropic API format.

    Anthropic uses:
    - name: Tool name
    - description: Tool description
    - input_schema: JSON schema for parameters
    """
    anthropic_tools: List[Dict[str, Any]] = []

    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            anthropic_tool = {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            }
            anthropic_tools.append(anthropic_tool)
        else:
            # Already in Anthropic format or simple format
            anthropic_tools.append(tool)

    # Add cache_control to the last tool for caching.
    # Note: Anthropic has no read-only cache mode.
    if enable_cache and anthropic_tools:
        anthropic_tools[-1]["cache_control"] = _make_cache_control(cache_ttl)

    return anthropic_tools


def _extract_text_from_response(message: Message) -> str:
    """Extract text content from Anthropic response."""
    texts: List[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            texts.append(block.text)
        elif hasattr(block, "text"):
            texts.append(block.text)
    return "".join(texts)


def _extract_thinking_from_response(message: Message) -> List[Dict[str, str]]:
    """Extract thinking content from Anthropic response.

    Anthropic's extended thinking returns content blocks with type='thinking'
    and a 'thinking' attribute containing the thought text.
    """
    reasoning_entries: List[Dict[str, str]] = []
    thinking_idx = 0
    for block in message.content:
        if getattr(block, "type", None) == "thinking":
            thinking_text = getattr(block, "thinking", "")
            if thinking_text and thinking_text.strip():
                thinking_idx += 1
                reasoning_entries.append({
                    "title": f"Thought {thinking_idx}",
                    "text": thinking_text.strip(),
                })
    return reasoning_entries


def _extract_tool_use_from_response(message: Message) -> Optional[Dict[str, Any]]:
    """Extract tool use from Anthropic response."""
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            return {
                "id": block.id,
                "name": block.name,
                "arguments": block.input,
            }
        elif hasattr(block, "type") and getattr(block, "type", None) == "tool_use":
            return {
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "arguments": getattr(block, "input", {}),
            }
    return None


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

        # Extract system messages
        system_blocks, remaining_messages = _prepare_anthropic_system(
            messages, enable_cache=enable_cache, cache_ttl=cache_ttl
        )

        # Prepare messages
        prepared_messages = _prepare_anthropic_messages(
            remaining_messages,
            supports_images=self.supports_images,
            max_image_bytes=self.max_image_bytes,
            enable_cache=enable_cache,
            cache_ttl=cache_ttl,
        )

        if not prepared_messages:
            logging.warning("[anthropic] No valid messages to send")
            return ""

        # Build request parameters
        request_params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": prepared_messages,
        }

        if system_blocks:
            request_params["system"] = system_blocks

        if temperature is not None:
            request_params["temperature"] = temperature
        elif "temperature" in self._extra_params:
            request_params["temperature"] = self._extra_params["temperature"]

        # Apply other extra params (top_p, top_k)
        for param in ("top_p", "top_k"):
            if param in self._extra_params:
                request_params[param] = self._extra_params[param]

        # Add thinking configuration if set
        if self._thinking_config:
            request_params["thinking"] = self._thinking_config

        # Handle tools
        use_tools = bool(tools)
        if use_tools:
            request_params["tools"] = _prepare_anthropic_tools(
                tools, enable_cache=enable_cache, cache_ttl=cache_ttl
            )

        # Build output_config (may contain effort and/or structured output format)
        output_config: Dict[str, Any] = {}
        if self._thinking_effort:
            output_config["effort"] = self._thinking_effort

        # Handle structured output
        use_native_structured_output = False
        if response_schema and not use_tools:
            if self._thinking_config:
                # Thinking enabled: use native output_config (compatible with thinking)
                # Native structured output requires additionalProperties: false on all objects
                prepared_schema = _prepare_schema_for_native_output(response_schema)
                output_config["format"] = {
                    "type": "json_schema",
                    "schema": prepared_schema,
                }
                use_native_structured_output = True
            else:
                # No thinking: use tool_choice pattern (legacy, works on all models)
                schema_name = response_schema.get("title", "structured_output")
                request_params["tools"] = [{
                    "name": schema_name,
                    "description": "Generate structured output according to the schema",
                    "input_schema": response_schema,
                }]
                request_params["tool_choice"] = {"type": "tool", "name": schema_name}

        # Apply output_config if any fields were set (effort and/or format)
        if output_config:
            request_params["output_config"] = output_config

        response = None
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                get_llm_logger().debug("[anthropic] Request (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, json.dumps(request_params, indent=2, ensure_ascii=False, default=str))
                response = self.client.messages.create(**request_params)
                get_llm_logger().debug("[anthropic] Response: %s", response.model_dump_json(indent=2))
                break  # Success, exit retry loop
            except anthropic.BadRequestError as e:
                logging.error("[anthropic] Bad request: %s", e)
                raise InvalidRequestError(f"Anthropic API error: {e}", e)
            except Exception as e:
                last_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[anthropic] Retryable error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("[anthropic] API call failed")
                raise _convert_to_llm_error(e, "API call")

        if response is None:
            if last_error:
                raise _convert_to_llm_error(last_error, f"API call after {MAX_RETRIES} retries")
            raise LLMEmptyResponseError(f"Anthropic API call failed after {MAX_RETRIES} retries with no response")

        # Store usage
        self._store_usage_from_response(response.usage, cache_ttl=cache_ttl)

        # Extract and store thinking content
        self._store_reasoning(_extract_thinking_from_response(response))

        # Handle structured output response
        if response_schema and not use_tools:
            if use_native_structured_output:
                # Native structured output: response is JSON text in content[0].text
                text = _extract_text_from_response(response)
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    logging.warning("[anthropic] Failed to parse native structured output as JSON, returning raw text")
                    return text
            else:
                # Tool-choice pattern: extract from tool_use block
                tool_use = _extract_tool_use_from_response(response)
                if tool_use:
                    return tool_use["arguments"]
                # Fallback to text if no tool use
                return _extract_text_from_response(response)

        # Handle tool mode
        if use_tools:
            tool_use = _extract_tool_use_from_response(response)
            if tool_use:
                self._store_tool_detection({
                    "type": "tool_call",
                    "tool_name": tool_use["name"],
                    "tool_args": tool_use["arguments"],
                })
                return {
                    "type": "tool_call",
                    "tool_name": tool_use["name"],
                    "tool_args": tool_use["arguments"],
                }
            else:
                text = _extract_text_from_response(response)
                # Check for empty text response without tool call
                if not text.strip():
                    logging.error(
                        "[anthropic] Empty text response without tool call. "
                        "Model returned empty content. stop_reason=%s",
                        getattr(response, "stop_reason", None)
                    )
                    raise LLMEmptyResponseError("Anthropic returned empty response without tool call")
                self._store_tool_detection({
                    "type": "text",
                    "content": text,
                })
                return {
                    "type": "text",
                    "content": text,
                }

        # Normal text response
        text = _extract_text_from_response(response)
        # Check for empty response
        if not text.strip():
            logging.error(
                "[anthropic] Empty text response. "
                "Model returned empty content. stop_reason=%s",
                getattr(response, "stop_reason", None)
            )
            raise LLMEmptyResponseError("Anthropic returned empty response")
        return text

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

        # Extract system messages
        system_blocks, remaining_messages = _prepare_anthropic_system(
            messages, enable_cache=enable_cache, cache_ttl=cache_ttl
        )

        # Prepare messages
        prepared_messages = _prepare_anthropic_messages(
            remaining_messages,
            supports_images=self.supports_images,
            max_image_bytes=self.max_image_bytes,
            enable_cache=enable_cache,
            cache_ttl=cache_ttl,
        )

        if not prepared_messages:
            logging.warning("[anthropic] No valid messages to send")
            return

        # Build request parameters
        request_params: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "messages": prepared_messages,
        }

        if system_blocks:
            request_params["system"] = system_blocks

        if temperature is not None:
            request_params["temperature"] = temperature
        elif "temperature" in self._extra_params:
            request_params["temperature"] = self._extra_params["temperature"]

        # Apply other extra params (top_p, top_k)
        for param in ("top_p", "top_k"):
            if param in self._extra_params:
                request_params[param] = self._extra_params[param]

        # Add thinking configuration if set
        if self._thinking_config:
            request_params["thinking"] = self._thinking_config

        # Add effort parameter via output_config if configured
        if self._thinking_effort:
            request_params["output_config"] = {"effort": self._thinking_effort}

        # Handle tools
        use_tools = bool(tools)
        if use_tools:
            request_params["tools"] = _prepare_anthropic_tools(
                tools, enable_cache=enable_cache, cache_ttl=cache_ttl
            )

        last_error: Optional[Exception] = None
        stream_success = False

        for attempt in range(MAX_RETRIES):
            try:
                get_llm_logger().debug("[anthropic] Stream request (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, json.dumps(request_params, indent=2, ensure_ascii=False, default=str))

                with self.client.messages.stream(**request_params) as stream:
                    final_message = None

                    for event in stream:
                        # Handle text and thinking deltas
                        if hasattr(event, "type"):
                            if event.type == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if delta:
                                    if hasattr(delta, "thinking"):
                                        yield {"type": "thinking", "content": delta.thinking}
                                    elif hasattr(delta, "text"):
                                        yield delta.text
                            elif event.type == "message_stop":
                                final_message = stream.get_final_message()

                    # Store usage and thinking from final message
                    if final_message:
                        if final_message.usage:
                            self._store_usage_from_response(final_message.usage, cache_ttl=cache_ttl)
                        self._store_reasoning(
                            _extract_thinking_from_response(final_message)
                        )

                    # Handle tool calls in streaming mode
                    if use_tools and final_message:
                        tool_use = _extract_tool_use_from_response(final_message)
                        if tool_use:
                            self._store_tool_detection({
                                "type": "tool_call",
                                "tool_name": tool_use["name"],
                                "tool_args": tool_use["arguments"],
                            })

                stream_success = True
                break  # Success, exit retry loop

            except anthropic.BadRequestError as e:
                logging.error("[anthropic] Bad request: %s", e)
                raise InvalidRequestError(f"Anthropic streaming API error: {e}", e)
            except Exception as e:
                last_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[anthropic] Retryable streaming error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("[anthropic] Streaming API call failed")
                raise _convert_to_llm_error(e, "streaming API call")

        if not stream_success:
            if last_error:
                raise _convert_to_llm_error(last_error, f"streaming API call after {MAX_RETRIES} retries")
            raise LLMEmptyResponseError(f"Anthropic streaming API call failed after {MAX_RETRIES} retries with no response")

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
