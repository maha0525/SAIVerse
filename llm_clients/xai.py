"""xAI native SDK client for Grok models.

Uses ``xai_sdk`` (gRPC) instead of the OpenAI-compatible REST endpoint.
Supports text generation, streaming, tool calling, structured output via
``chat.parse()``, and multimodal vision messages.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm
from tools import OPENAI_TOOLS_SPEC
from saiverse.llm_router import route

from .base import LLMClient, get_llm_logger
from .exceptions import (
    AuthenticationError,
    EmptyResponseError,
    InvalidRequestError,
    LLMError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    ServerError,
)
from .utils import (
    compute_allowed_attachment_keys,
    content_to_text,
    image_summary_note,
    merge_reasoning_strings,
    parse_attachment_limit,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error classification helpers (gRPC / xai_sdk exceptions)
# ---------------------------------------------------------------------------

def _convert_to_llm_error(err: Exception, context: str = "API call") -> LLMError:
    """Convert xai_sdk / gRPC exceptions to SAIVerse LLMError subclasses."""
    msg = str(err).lower()

    # Try gRPC status codes first
    try:
        import grpc
        if isinstance(err, grpc.RpcError):
            code = err.code()
            if code == grpc.StatusCode.UNAUTHENTICATED:
                return AuthenticationError(f"xAI {context}: authentication failed", err)
            if code == grpc.StatusCode.PERMISSION_DENIED:
                return AuthenticationError(f"xAI {context}: permission denied", err)
            if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                return RateLimitError(f"xAI {context}: rate limit exceeded", err)
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                return LLMTimeoutError(f"xAI {context}: timeout", err)
            if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL):
                return ServerError(f"xAI {context}: server error", err)
    except ImportError:
        pass

    # Fallback: inspect error message strings
    if "401" in msg or "unauthenticated" in msg or "invalid api key" in msg:
        return AuthenticationError(f"xAI {context}: authentication error", err)
    if "402" in msg or "payment" in msg or "billing" in msg:
        return PaymentError(f"xAI {context}: payment required", err)
    if "429" in msg or "rate" in msg or "quota" in msg or "exhausted" in msg:
        return RateLimitError(f"xAI {context}: rate limit", err)
    if "timeout" in msg or "deadline" in msg:
        return LLMTimeoutError(f"xAI {context}: timeout", err)
    if "503" in msg or "502" in msg or "unavailable" in msg:
        return ServerError(f"xAI {context}: server error", err)

    return LLMError(f"xAI {context} failed: {err}", err)


# ---------------------------------------------------------------------------
# Message / tool conversion helpers
# ---------------------------------------------------------------------------

def _convert_tools(tools_spec: List[Dict[str, Any]]) -> list:
    """Convert OpenAI-format tool specs to xai_sdk ``tool()`` objects."""
    from xai_sdk.chat import tool as xai_tool

    xai_tools = []
    for t in tools_spec:
        func = t.get("function", t)
        xai_tools.append(xai_tool(
            name=func["name"],
            description=func.get("description", ""),
            parameters=func.get("parameters", {}),
        ))
    return xai_tools


def _build_xai_messages(
    messages: List[Dict[str, Any]],
    supports_images: bool,
) -> list:
    """Convert SAIVerse message dicts to xai_sdk message objects.

    Handles system/user/assistant roles and optional image attachments.
    Skips empty messages and SAIMemory metadata fields.
    """
    from xai_sdk.chat import assistant, image, system, user

    max_image_embeds = parse_attachment_limit("XAI")

    # Pre-scan for image attachments
    attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
    exempt_indices: set = set()
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata")
        if isinstance(metadata, dict) and metadata.get("__visual_context__"):
            exempt_indices.add(idx)
        media_items = iter_image_media(metadata)
        if media_items:
            attachment_cache[idx] = media_items

    allowed_attachment_keys = compute_allowed_attachment_keys(
        attachment_cache, max_image_embeds, exempt_indices,
    )

    result: list = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue

        role = (msg.get("role") or "").lower()
        if role == "host":
            role = "system"

        raw_content = msg.get("content", "")
        text = content_to_text(raw_content)

        # Skip empty messages (except tool role)
        if not text and role in ("system", "user", "assistant"):
            continue

        if role == "system":
            result.append(system(text))
        elif role == "user":
            # Build image parts if supported
            image_parts: List[Any] = []
            attachments = attachment_cache.get(idx, [])
            if supports_images and attachments:
                for att_idx, att in enumerate(attachments):
                    should_embed = (
                        allowed_attachment_keys is None
                        or (idx, att_idx) in allowed_attachment_keys
                    )
                    if should_embed:
                        data, effective_mime = load_image_bytes_for_llm(
                            att["path"], att["mime_type"],
                        )
                        if data and effective_mime:
                            b64 = base64.b64encode(data).decode("ascii")
                            image_parts.append(
                                image(image_url=f"data:{effective_mime};base64,{b64}")
                            )
                            continue
                    # Not embedding — add text summary
                    note = image_summary_note(
                        att["path"], att["mime_type"],
                        att.get("uri", att.get("path", "unknown")),
                    )
                    text = f"{text}\n{note}" if text else note

            if image_parts:
                result.append(user(text, *image_parts))
            else:
                result.append(user(text))
        elif role == "assistant":
            result.append(assistant(text))
        # tool / tool_result messages are not part of the initial history

    return result


# ---------------------------------------------------------------------------
# Allowed request parameters
# ---------------------------------------------------------------------------

XAI_ALLOWED_REQUEST_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "n",
    "reasoning_effort",
    "seed",
}


# ---------------------------------------------------------------------------
# XAIClient
# ---------------------------------------------------------------------------

class XAIClient(LLMClient):
    """Client for xAI's native SDK (gRPC-based).

    Uses ``xai_sdk.Client`` for all API operations including text generation,
    streaming, tool calling, structured output, and image understanding.
    """

    def __init__(
        self,
        model: str,
        *,
        supports_images: bool = False,
        api_key: Optional[str] = None,
        api_key_env: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        super().__init__(supports_images=supports_images)
        import xai_sdk

        key_env = api_key_env or "XAI_API_KEY"
        resolved_key = api_key or os.getenv(key_env)
        if not resolved_key:
            raise AuthenticationError(
                f"{key_env} environment variable is not set.",
                user_message="xAI APIキーが設定されていません。管理者にお問い合わせください。",
            )

        self.client = xai_sdk.Client(api_key=resolved_key, timeout=3600)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._request_kwargs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        if not isinstance(parameters, dict):
            return
        for key, value in parameters.items():
            if key not in XAI_ALLOWED_REQUEST_PARAMS:
                continue
            if value is None:
                self._request_kwargs.pop(key, None)
            else:
                self._request_kwargs[key] = value

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_chat(
        self,
        tools_spec: Optional[List[Dict[str, Any]]] = None,
        response_format: Any = None,
    ):
        """Create an xai_sdk Chat instance with current settings."""
        kwargs: Dict[str, Any] = {"model": self.model}

        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort

        if tools_spec:
            kwargs["tools"] = _convert_tools(tools_spec)

        if response_format is not None:
            kwargs["response_format"] = response_format

        # Store messages flag — we don't need server-side storage
        kwargs["store_messages"] = False

        return self.client.chat.create(**kwargs)

    def _store_usage_from_response(self, response: Any) -> None:
        """Extract and store token usage from an xai_sdk response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        input_tokens = getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            cached = getattr(details, "cached_tokens", 0) or 0
        self._store_usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
        )

    def _store_reasoning_from_response(self, response: Any) -> None:
        """Extract and store reasoning content from an xai_sdk response."""
        reasoning_content = getattr(response, "reasoning_content", None)
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            self._store_reasoning([{"title": "", "text": reasoning_content.strip()}])
        else:
            self._store_reasoning([])

        # Also store reasoning token count for logging
        usage = getattr(response, "usage", None)
        if usage:
            reasoning_tokens = getattr(usage, "reasoning_tokens", None)
            if reasoning_tokens:
                _log.info("[xai] Reasoning tokens used: %d", reasoning_tokens)

    # ------------------------------------------------------------------
    # generate() — non-streaming
    # ------------------------------------------------------------------

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[list] = None,
        history_snippets: Optional[List[str]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str | Dict[str, Any]:
        """Generate a response using xai_sdk.

        Args:
            messages: Conversation messages
            tools: Tool specifications. If provided, returns Dict with tool detection.
            history_snippets: Optional history context
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override

        Returns:
            str: Text response when tools is None or empty
            Dict: Tool detection result when tools is provided
        """
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        snippets: List[str] = list(history_snippets or [])
        self._store_reasoning([])

        if response_schema and use_tools:
            _log.warning("response_schema specified alongside tools; structured output ignored for tool runs.")
            response_schema = None

        # Apply temperature override
        chat_kwargs: Dict[str, Any] = {}
        if temperature is not None:
            chat_kwargs["temperature"] = temperature

        xai_messages = _build_xai_messages(messages, self.supports_images)
        get_llm_logger().debug(
            "[xai] generate: model=%s, messages=%d, tools=%d, schema=%s",
            self.model, len(xai_messages), len(tools_spec), bool(response_schema),
        )

        try:
            if use_tools:
                return self._generate_with_tools(xai_messages, tools_spec)
            elif response_schema:
                return self._generate_with_schema(xai_messages, response_schema)
            else:
                return self._generate_text(xai_messages, snippets)
        except LLMError:
            raise
        except Exception as e:
            _log.exception("[xai] generate failed")
            raise _convert_to_llm_error(e, "generate")

    def _generate_text(
        self,
        xai_messages: list,
        snippets: List[str],
    ) -> str:
        """Plain text generation."""
        chat = self._create_chat()
        for msg in xai_messages:
            chat.append(msg)

        response = chat.sample()
        get_llm_logger().debug("[xai] response content length: %d", len(response.content or ""))

        self._store_usage_from_response(response)
        self._store_reasoning_from_response(response)

        text = response.content or ""
        if not text.strip():
            raise EmptyResponseError("xAI returned empty response")

        if snippets:
            prefix = "\n".join(snippets)
            return prefix + ("\n" if text and prefix else "") + text
        return text

    def _generate_with_tools(
        self,
        xai_messages: list,
        tools_spec: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Generation with tool detection (no execution)."""
        chat = self._create_chat(tools_spec=tools_spec)
        for msg in xai_messages:
            chat.append(msg)

        response = chat.sample()
        self._store_usage_from_response(response)
        self._store_reasoning_from_response(response)

        tool_calls = getattr(response, "tool_calls", None)
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, AttributeError):
                _log.warning("Tool call arguments invalid JSON: %s", getattr(tc.function, "arguments", ""))
                args = {}
            return {
                "type": "tool_call",
                "tool_name": tc.function.name,
                "tool_args": args,
            }

        content = response.content or ""
        if not content.strip():
            raise EmptyResponseError("xAI returned empty response without tool call")
        return {"type": "text", "content": content}

    def _generate_with_schema(
        self,
        xai_messages: list,
        response_schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Structured output using chat.parse() with dynamic Pydantic model."""
        from .xai_schema_utils import json_schema_to_pydantic

        model_name = response_schema.get("title", "DynamicOutput")
        DynamicModel = json_schema_to_pydantic(response_schema, model_name=model_name)

        chat = self._create_chat(response_format=DynamicModel)
        for msg in xai_messages:
            chat.append(msg)

        response, parsed = chat.parse(DynamicModel)
        self._store_usage_from_response(response)
        self._store_reasoning_from_response(response)

        return parsed.model_dump()

    # ------------------------------------------------------------------
    # generate_stream() — streaming
    # ------------------------------------------------------------------

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[list] = None,
        force_tool_choice: Optional[dict | str] = None,
        history_snippets: Optional[List[str]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        """Stream response tokens using xai_sdk.

        Yields text fragments and ``{"type": "thinking", ...}`` dicts for
        reasoning content.
        """
        tools_spec = OPENAI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = list(history_snippets or [])
        self._store_reasoning([])
        reasoning_chunks: List[str] = []

        xai_messages = _build_xai_messages(messages, self.supports_images)

        try:
            if not use_tools:
                # No-tools streaming (plain text or structured output)
                if response_schema:
                    # Structured output: use non-streaming parse, then yield result
                    result = self._generate_with_schema(xai_messages, response_schema)
                    prefix = "\n".join(history_snippets)
                    if prefix:
                        yield prefix + "\n"
                    yield json.dumps(result, ensure_ascii=False)
                    return

                chat = self._create_chat()
                for msg in xai_messages:
                    chat.append(msg)

                prefix = "\n".join(history_snippets)
                if prefix:
                    yield prefix + "\n"

                response = None
                for resp, chunk in chat.stream():
                    response = resp
                    chunk_content = getattr(chunk, "content", None)
                    if chunk_content:
                        yield chunk_content

                    # Check for reasoning content in chunk
                    reasoning_text = getattr(chunk, "reasoning_content", None)
                    if isinstance(reasoning_text, str) and reasoning_text:
                        reasoning_chunks.append(reasoning_text)
                        yield {"type": "thinking", "content": reasoning_text}

                if response:
                    self._store_usage_from_response(response)
                self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
                return

            # --- Tool-aware streaming ---
            if force_tool_choice is None:
                user_msg = next(
                    (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                    "",
                )
                decision = route(user_msg, tools_spec)
                _log.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))

            chat = self._create_chat(tools_spec=tools_spec)
            for msg in xai_messages:
                chat.append(msg)

            prefix_yielded = False
            response = None
            text_fragments: List[str] = []

            for resp, chunk in chat.stream():
                response = resp
                chunk_content = getattr(chunk, "content", None)

                # Check for reasoning in chunk
                reasoning_text = getattr(chunk, "reasoning_content", None)
                if isinstance(reasoning_text, str) and reasoning_text:
                    reasoning_chunks.append(reasoning_text)
                    yield {"type": "thinking", "content": reasoning_text}

                if chunk_content:
                    if not prefix_yielded and history_snippets:
                        yield "\n".join(history_snippets) + "\n"
                        prefix_yielded = True
                    text_fragments.append(chunk_content)
                    yield chunk_content

            if response:
                self._store_usage_from_response(response)

            self._store_reasoning(merge_reasoning_strings(reasoning_chunks))

            # Check for tool calls in the final response
            tool_calls = getattr(response, "tool_calls", None) if response else None
            if tool_calls and len(tool_calls) > 0:
                tc = tool_calls[0]
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                self._store_tool_detection({
                    "type": "tool_call",
                    "tool_name": tc.function.name,
                    "tool_args": args,
                })
                _log.info("[xai] Tool detection stored: %s", tc.function.name)
            else:
                self._store_tool_detection({"type": "text", "content": "".join(text_fragments)})

        except LLMError:
            raise
        except Exception as e:
            _log.exception("[xai] streaming failed")
            raise _convert_to_llm_error(e, "streaming")

    # ------------------------------------------------------------------
    # Deprecated: generate_with_tool_detection()
    # ------------------------------------------------------------------

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """DEPRECATED: Use generate(messages, tools=[...]) instead."""
        import warnings
        warnings.warn(
            "generate_with_tool_detection() is deprecated. Use generate(messages, tools=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        tools_spec = tools or []
        if not tools_spec:
            result = self.generate(messages, temperature=temperature)
            if isinstance(result, str):
                return {"type": "text", "content": result}
            return result
        return self.generate(messages, tools=tools_spec, temperature=temperature)


__all__ = ["XAIClient"]
