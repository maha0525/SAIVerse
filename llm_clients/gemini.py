"""Google Gemini client implementation."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import httpx
from google import genai
from google.genai import types

from .exceptions import (
    EmptyResponseError as LLMEmptyResponseError,
    LLMError,
    LLMTimeoutError,
    RateLimitError,
    SafetyFilterError,
    ServerError,
)

try:  # pragma: no cover - optional defensive patching
    from google.genai import _api_client as _genai_api_client
except Exception:  # pragma: no cover - absence is fine
    _genai_api_client = None


def _install_gemini_stream_patch() -> None:
    disable_flag = os.getenv("SAIVERSE_DISABLE_GEMINI_SSE_PATCH")
    if disable_flag and disable_flag.lower() not in {"0", "false", "off"}:
        logging.warning(
            "Skipping Gemini SSE patch because SAIVERSE_DISABLE_GEMINI_SSE_PATCH=%s",
            disable_flag,
        )
        return
    """Patch google.genai HttpResponse streaming to respect SSE framing."""

    if _genai_api_client is None:
        return

    HttpResponse = getattr(_genai_api_client, "HttpResponse", None)
    if HttpResponse is None:
        return
    if getattr(HttpResponse, "_saiverse_sse_patch", False):
        return

    def _clean_line(raw: Any) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raw = raw.decode("utf-8", errors="ignore")
        return raw.rstrip("\r\n")

    class _SSEAccumulator:
        __slots__ = ("_buffer",)

        def __init__(self) -> None:
            self._buffer: List[str] = []

        def feed(self, line: str) -> Optional[str]:
            if not line:
                if self._buffer:
                    data = "".join(self._buffer)
                    self._buffer.clear()
                    return data
                return None
            if line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                self._buffer.append(value)
            elif line.startswith(":"):
                return None
            else:
                # Skip other SSE fields (event:, id:, retry:, etc.)
                return None
            return None

        def flush(self) -> Optional[str]:
            if self._buffer:
                data = "".join(self._buffer)
                self._buffer.clear()
                return data
            return None

    original_segments = HttpResponse.segments
    original_async_segments = HttpResponse.async_segments

    def _yield_json_chunks(iterator: Iterator[str]) -> Iterator[Any]:
        _sse_logger = logging.getLogger("saiverse.llm")
        acc = _SSEAccumulator()
        for raw_line in iterator:
            cleaned = _clean_line(raw_line)
            _sse_logger.debug("Gemini SSE raw line: %s", cleaned)
            combined = acc.feed(cleaned)
            if combined:
                _sse_logger.debug("Gemini SSE payload: %s", combined)
                yield json.loads(combined)
        final = acc.flush()
        if final:
            _sse_logger.debug("Gemini SSE final payload: %s", final)
            yield json.loads(final)

    async def _yield_json_chunks_async(iterator: Any) -> Any:
        _sse_logger = logging.getLogger("saiverse.llm")
        acc = _SSEAccumulator()
        async for raw_line in iterator:
            cleaned = _clean_line(raw_line)
            _sse_logger.debug("Gemini SSE raw line (async): %s", cleaned)
            combined = acc.feed(cleaned)
            if combined:
                _sse_logger.debug("Gemini SSE payload (async): %s", combined)
                yield json.loads(combined)
        final = acc.flush()
        if final:
            _sse_logger.debug("Gemini SSE final payload (async): %s", final)

    def _patched_segments(self) -> Iterator[Any]:
        if isinstance(self.response_stream, list) or self.response_stream is None:
            yield from original_segments(self)  # type: ignore[misc]
            return
        if hasattr(self.response_stream, "iter_lines"):
            iterator = self.response_stream.iter_lines()  # type: ignore[union-attr]
            yield from _yield_json_chunks(iterator)
        else:
            yield from original_segments(self)  # type: ignore[misc]

    async def _patched_async_segments(self) -> Any:
        if isinstance(self.response_stream, list) or self.response_stream is None:
            async for chunk in original_async_segments(self):  # type: ignore[misc]
                yield chunk
            return
        if hasattr(self.response_stream, "aiter_lines"):
            async for item in _yield_json_chunks_async(self.response_stream.aiter_lines()):
                yield item
            return
        if hasattr(self.response_stream, "content"):
            async def _content_line_iter():
                while True:
                    chunk = await self.response_stream.content.readline()
                    if not chunk:
                        break
                    yield chunk

            try:
                async for item in _yield_json_chunks_async(_content_line_iter()):
                    yield item
            finally:
                if hasattr(self, "_session") and self._session:
                    await self._session.close()
            return
        async for chunk in original_async_segments(self):  # type: ignore[misc]
            yield chunk

    HttpResponse.segments = _patched_segments  # type: ignore[assignment]
    HttpResponse.async_segments = _patched_async_segments  # type: ignore[assignment]
    HttpResponse._saiverse_sse_patch = True  # type: ignore[attr-defined]


_install_gemini_stream_patch()

from .gemini_utils import build_gemini_clients

from media_utils import iter_image_media, load_image_bytes_for_llm
from media_summary import ensure_image_summary
from tools import GEMINI_TOOLS_SPEC
from llm_router import route

from .base import EmptyResponseError, IncompleteStreamError, LLMClient, get_llm_logger
from logging_config import log_timeout_event
from .utils import content_to_text, is_truthy_flag, merge_reasoning_strings


class ChunkTimeoutError(RuntimeError):
    """Raised when no chunks are received within the timeout period (socket stall)."""


# Timeout for receiving chunks during streaming (detects socket stalls)
CHUNK_TIMEOUT_SECONDS = int(os.getenv("GEMINI_CHUNK_TIMEOUT_SECONDS", "60"))

# Default safety settings (can be overridden via configure_parameters)
GEMINI_DEFAULT_SAFETY_SETTINGS = {
    "HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH",
    "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_ONLY_HIGH",
}

# Legacy constant for backward compatibility
GEMINI_SAFETY_CONFIG = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
    ),
]

# Allowed request parameters for Gemini API
GEMINI_ALLOWED_REQUEST_PARAMS = {
    "temperature", "top_p", "top_k", "max_output_tokens", "stop_sequences",
}

# Special parameters that need custom handling (not passed directly to API)
GEMINI_SPECIAL_PARAMS = {
    "thinking_level",
    "safety_harassment", "safety_hate_speech",
    "safety_sexually_explicit", "safety_dangerous_content",
}

# Mapping from parameter names to Gemini HarmCategory names
SAFETY_PARAM_TO_CATEGORY = {
    "safety_harassment": "HARM_CATEGORY_HARASSMENT",
    "safety_hate_speech": "HARM_CATEGORY_HATE_SPEECH",
    "safety_sexually_explicit": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "safety_dangerous_content": "HARM_CATEGORY_DANGEROUS_CONTENT",
}

GROUNDING_TOOL = types.Tool(google_search=types.GoogleSearch())


def merge_tools_for_gemini(request_tools: Optional[List[types.Tool]]) -> List[types.Tool]:
    """Ensure grounding tool is included when no custom functions are declared."""
    request_tools = request_tools or GEMINI_TOOLS_SPEC
    has_functions = any(tool.function_declarations for tool in request_tools)
    if has_functions:
        return request_tools
    return [GROUNDING_TOOL] + request_tools


class GeminiClient(LLMClient):
    """Client for Google Gemini API."""

    def __init__(
        self,
        model: str,
        config: Optional[Dict[str, Any]] | None = None,
        supports_images: bool = True,
    ) -> None:
        super().__init__(supports_images=supports_images)
        cfg = config or {}
        prefer_paid = cfg.get("prefer_paid", False)
        self.free_client, self.paid_client, self.client = build_gemini_clients(prefer_paid=prefer_paid)
        self.model = model

        # Request parameters (temperature, top_p, etc.)
        self._request_params: Dict[str, Any] = {}

        # Thinking configuration
        self._thinking_level: Optional[str] = None
        self._include_thoughts: bool = cfg.get("include_thoughts", False)
        # Auto-enable for 2.5/3 series models
        if not self._include_thoughts:
            model_lower = (model or "").lower()
            self._include_thoughts = "2.5" in model_lower or "-3-" in model_lower or model_lower.startswith("gemini-3")

        # Safety settings overrides
        self._safety_overrides: Dict[str, str] = {}

    def _build_thinking_config(self) -> Optional[types.ThinkingConfig]:
        """Build ThinkingConfig from current settings."""
        if not self._include_thoughts and not self._thinking_level:
            return None

        kwargs: Dict[str, Any] = {}
        if self._include_thoughts:
            kwargs["include_thoughts"] = True
        if self._thinking_level:
            kwargs["thinking_level"] = self._thinking_level

        try:
            return types.ThinkingConfig(**kwargs)
        except Exception as e:
            logging.warning("Failed to create ThinkingConfig: %s", e)
            return None

    def _build_safety_settings(self) -> List[types.SafetySetting]:
        """Build safety settings from defaults and overrides."""
        # Start with defaults
        settings = dict(GEMINI_DEFAULT_SAFETY_SETTINGS)

        # Apply overrides from configure_parameters
        for param_key, category in SAFETY_PARAM_TO_CATEGORY.items():
            if param_key in self._safety_overrides:
                settings[category] = self._safety_overrides[param_key]

        result = []
        for category_name, threshold_name in settings.items():
            category = getattr(types.HarmCategory, category_name, None)
            threshold = getattr(types.HarmBlockThreshold, threshold_name, None)
            if category and threshold:
                result.append(types.SafetySetting(category=category, threshold=threshold))
        return result

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        """Configure model parameters from UI settings."""
        if not isinstance(parameters, dict):
            return
        for key, value in parameters.items():
            if key in GEMINI_ALLOWED_REQUEST_PARAMS:
                if value is None:
                    self._request_params.pop(key, None)
                else:
                    self._request_params[key] = value
            elif key == "thinking_level":
                self._thinking_level = value if value else None
            elif key in SAFETY_PARAM_TO_CATEGORY:
                if value:
                    self._safety_overrides[key] = value
                else:
                    self._safety_overrides.pop(key, None)

    @staticmethod
    def _schema_from_json(js: Optional[Dict[str, Any]]) -> Optional[types.Schema]:
        if js is None:
            return None

        def _to_schema(node: Any) -> types.Schema:
            if not isinstance(node, dict):
                return types.Schema(type=types.Type.OBJECT)

            kwargs: Dict[str, Any] = {}
            value_type = node.get("type")
            if isinstance(value_type, list):
                if len(value_type) == 1:
                    value_type = value_type[0]
                else:
                    kwargs["any_of"] = [_to_schema({**node, "type": t}) for t in value_type]
                    value_type = None
            if isinstance(value_type, str):
                kwargs["type"] = {
                    "string": types.Type.STRING,
                    "number": types.Type.NUMBER,
                    "integer": types.Type.INTEGER,
                    "boolean": types.Type.BOOLEAN,
                    "array": types.Type.ARRAY,
                    "object": types.Type.OBJECT,
                    "null": types.Type.NULL,
                }.get(value_type, types.Type.TYPE_UNSPECIFIED)

            if "description" in node:
                kwargs["description"] = node["description"]
            if "enum" in node and isinstance(node["enum"], list):
                kwargs["enum"] = node["enum"]
            if "const" in node:
                kwargs["enum"] = [node["const"]]

            if "properties" in node and isinstance(node["properties"], dict):
                props = {k: _to_schema(v) for k, v in node["properties"].items()}
                kwargs["properties"] = props
                kwargs["property_ordering"] = list(node["properties"].keys())
            if "additionalProperties" in node:
                ap = node["additionalProperties"]
                if isinstance(ap, dict):
                    kwargs["additional_properties"] = _to_schema(ap)
                elif isinstance(ap, bool):
                    kwargs["additional_properties"] = ap
            if "required" in node and isinstance(node["required"], list):
                kwargs["required"] = node["required"]

            if "items" in node:
                kwargs["items"] = _to_schema(node["items"])

            if "anyOf" in node and isinstance(node["anyOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["anyOf"]]

            if "oneOf" in node and isinstance(node["oneOf"], list):
                kwargs["any_of"] = [_to_schema(sub) for sub in node["oneOf"]]

            if "allOf" in node and isinstance(node["allOf"], list):
                kwargs["all_of"] = [_to_schema(sub) for sub in node["allOf"]]

            return types.Schema(**kwargs)

        return _to_schema(js)

    @staticmethod
    def _requires_json_schema(node: Any) -> bool:
        if isinstance(node, dict):
            if "additionalProperties" in node:
                ap_val = node.get("additionalProperties")
                if ap_val not in (None, False):
                    return True
            for key in ("properties", "patternProperties"):
                props = node.get(key)
                if isinstance(props, dict) and any(
                    GeminiClient._requires_json_schema(value) for value in props.values()
                ):
                    return True
            for key in ("items", ):
                child = node.get(key)
                if child and GeminiClient._requires_json_schema(child):
                    return True
            for key in ("anyOf", "oneOf", "allOf"):
                child_list = node.get(key)
                if isinstance(child_list, list) and any(GeminiClient._requires_json_schema(item) for item in child_list):
                    return True
        elif isinstance(node, list):
            return any(GeminiClient._requires_json_schema(item) for item in node)
        return False

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        msg = str(err).lower()
        return (
            "rate" in msg
            or "429" in msg
            or "quota" in msg
            or "503" in msg
            or "unavailable" in msg
            or "overload" in msg
        )

    @staticmethod
    def _is_timeout_error(err: Exception) -> bool:
        """Check if the error is a timeout that should be retried."""
        return isinstance(err, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, ChunkTimeoutError))

    @staticmethod
    def _is_server_error(err: Exception) -> bool:
        """Check if the error is a server error (5xx)."""
        msg = str(err).lower()
        return "500" in msg or "502" in msg or "504" in msg or "internal" in msg

    @staticmethod
    def _is_authentication_error(err: Exception) -> bool:
        """Check if the error is an authentication error."""
        msg = str(err).lower()
        return "401" in msg or "403" in msg or "invalid" in msg and "key" in msg or "authentication" in msg

    def _convert_to_llm_error(self, err: Exception, context: str = "API call") -> LLMError:
        """Convert a generic exception to an appropriate LLMError subclass."""
        if self._is_rate_limit_error(err):
            return RateLimitError(f"Gemini {context} failed: rate limit exceeded", err)
        elif self._is_timeout_error(err):
            return LLMTimeoutError(f"Gemini {context} failed: timeout", err)
        elif self._is_server_error(err):
            return ServerError(f"Gemini {context} failed: server error", err)
        elif self._is_authentication_error(err):
            from .exceptions import AuthenticationError
            return AuthenticationError(f"Gemini {context} failed: authentication error", err)
        else:
            return LLMError(f"Gemini {context} failed: {err}", err)

    def _convert_messages(
        self,
        msgs: List[Dict[str, str] | types.Content],
    ) -> Tuple[str, List[types.Content]]:
        system_lines: List[str] = []
        contents: List[types.Content] = []
        attachment_limit_env = os.getenv("SAIVERSE_GEMINI_ATTACHMENT_LIMIT")
        max_image_embeds: Optional[int] = None
        if attachment_limit_env is not None:
            try:
                max_image_embeds = int(attachment_limit_env.strip())
            except ValueError:
                logging.warning(
                    "Invalid SAIVERSE_GEMINI_ATTACHMENT_LIMIT=%s; ignoring",
                    attachment_limit_env,
                )
                max_image_embeds = None
            else:
                if max_image_embeds < 0:
                    max_image_embeds = 0
        logging.debug(
            "[gemini] attachment limit=%s",
            "∞" if max_image_embeds is None else max_image_embeds,
        )

        attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
        # Track messages that are exempt from attachment limits (visual context)
        exempt_message_indices: Set[int] = set()
        if self.supports_images:
            for idx, message in enumerate(msgs):
                if isinstance(message, dict):
                    metadata = message.get("metadata")
                    # Check for visual context marker - these are exempt from limits
                    if isinstance(metadata, dict) and metadata.get("__visual_context__"):
                        exempt_message_indices.add(idx)
                    media_items = iter_image_media(metadata)
                    if media_items:
                        attachment_cache[idx] = media_items
        allowed_attachment_keys: Optional[Set[Tuple[int, int]]] = None
        if max_image_embeds is not None and attachment_cache:
            ordered: List[Tuple[int, int]] = []
            for msg_idx in sorted(attachment_cache.keys(), reverse=True):
                # Skip exempt (visual context) messages when counting towards limit
                if msg_idx in exempt_message_indices:
                    continue
                items = attachment_cache[msg_idx]
                for att_idx in range(len(items)):
                    ordered.append((msg_idx, att_idx))
            if max_image_embeds == 0:
                allowed_attachment_keys = set()
            else:
                allowed_attachment_keys = set(ordered[:max_image_embeds])
            # Always allow visual context attachments (exempt from limit)
            for msg_idx in exempt_message_indices:
                if msg_idx in attachment_cache:
                    for att_idx in range(len(attachment_cache[msg_idx])):
                        allowed_attachment_keys.add((msg_idx, att_idx))
                    logging.debug(
                        "[gemini] visual context at idx=%d exempted from attachment limit (%d images)",
                        msg_idx,
                        len(attachment_cache[msg_idx]),
                    )
        elif max_image_embeds is not None:
            allowed_attachment_keys = set()

        for idx, message in enumerate(msgs):
            if isinstance(message, types.Content):
                contents.append(message)
                continue

            role = message.get("role", "")
            if role == "system":
                system_lines.append(message.get("content", "") or "")
                continue

            if "tool_calls" in message:
                for fn_call in message["tool_calls"]:
                    contents.append(
                        types.Content(role="model", parts=[types.Part(function_call=fn_call)])
                    )
                continue

            text = content_to_text(message.get("content", "")) or ""
            text_content = text
            g_role = "user" if role == "user" else "model"
            attachments = attachment_cache.get(idx, []) if self.supports_images else []
            if attachments and self.supports_images:
                selected_attachments: List[Dict[str, Any]] = []
                skipped_attachments: List[Dict[str, Any]] = []
                attachment_records: List[Tuple[int, Dict[str, Any], Optional[str]]] = []
                for att_idx, attachment in enumerate(attachments):
                    summary_text = ensure_image_summary(attachment["path"], attachment["mime_type"])
                    key = (idx, att_idx)
                    if allowed_attachment_keys is not None and key not in allowed_attachment_keys:
                        attachment_records.append((att_idx, attachment, summary_text))
                        skipped_attachments.append(attachment)
                        continue
                    attachment_records.append((att_idx, attachment, summary_text))
                    selected_attachments.append(attachment)
                logging.debug(
                    "[gemini] embedding %d image attachment(s) for role=%s (skipped=%d)",
                    len(selected_attachments),
                    role,
                    len(skipped_attachments),
                )
                parts: List[types.Part] = []
                if skipped_attachments:
                    for att_idx, attachment, summary_text in attachment_records:
                        if attachment not in skipped_attachments:
                            continue
                        summary = summary_text or "(要約を取得できませんでした)"
                        note = f"[画像参照のみ: {attachment['uri']}] {summary}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                if text_content:
                    parts.append(types.Part(text=text_content))
                for attachment in selected_attachments:
                    data, effective_mime = load_image_bytes_for_llm(
                        attachment["path"], attachment["mime_type"]
                    )
                    if not data or not effective_mime:
                        logging.warning(
                            "Failed to load image for Gemini payload: %s", attachment["uri"]
                        )
                        continue
                    parts.append(types.Part.from_bytes(data=data, mime_type=effective_mime))
                if not parts:
                    parts.append(types.Part(text=""))
                contents.append(types.Content(parts=parts, role=g_role))
            else:
                if attachments:
                    logging.debug(
                        "[gemini] image attachments present but not embedded (supports_images=%s)",
                        self.supports_images,
                    )
                    for attachment in attachments:
                        summary = ensure_image_summary(attachment["path"], attachment["mime_type"])
                        summary_note = summary or "(要約を取得できませんでした)"
                        note = f"[画像: {attachment['uri']}] {summary_note}"
                        text_content = f"{text_content}\n{note}" if text_content else note
                contents.append(types.Content(parts=[types.Part(text=text_content)], role=g_role))

        return "\n".join(system_lines), contents

    def _last_user(self, messages: List[Any]) -> str:
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                return content_to_text(message.get("content", ""))
            if isinstance(message, types.Content) and message.role == "user":
                if message.parts and message.parts[0].text:
                    return message.parts[0].text
        return ""

    def _call_with_client(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
    ):
        sys_msg, contents = self._convert_messages(messages)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }
        if tools_spec:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        # Always disable AFC - SAIVerse handles function calls manually
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config
        return client.models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    def generate(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] = None,
        history_snippets: Optional[List[str]] = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> str | Dict[str, Any]:
        """Unified generate method.
        
        Args:
            messages: Conversation messages
            tools: Tool specifications. If provided, returns Dict with tool detection.
                   If None or empty, returns str with text response.
            history_snippets: Optional history context
            response_schema: Optional JSON schema for structured output
            temperature: Optional temperature override
            
        Returns:
            str: Text response when tools is None or empty
            Dict: Tool detection result when tools is provided, with keys:
                  - type: "text" | "tool_call" | "both"
                  - content: Generated text (if any)
                  - tool_name: Tool name (if tool_call or both)
                  - tool_args: Tool arguments dict (if tool_call or both)
        """
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])

        active_client = self.client
        sys_msg, contents = self._convert_messages(messages)
        
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }

        # Temperature: argument > _request_params
        effective_temp = temperature if temperature is not None else self._request_params.get("temperature")
        if effective_temp is not None:
            cfg_kwargs["temperature"] = effective_temp

        # Apply other request params (top_p, top_k, max_output_tokens, etc.)
        for param in ("top_p", "top_k", "max_output_tokens", "stop_sequences"):
            if param in self._request_params:
                cfg_kwargs[param] = self._request_params[param]

        # Tool configuration
        if use_tools:
            merged_tools = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tools"] = merged_tools
            cfg_kwargs["tool_config"] = types.ToolConfig(
                functionCallingConfig=types.FunctionCallingConfig(mode="AUTO")
            )
            logging.info("[gemini] Sending %d Tool objects to API", len(merged_tools))

        # Response schema configuration
        if response_schema:
            cfg_kwargs["response_mime_type"] = "application/json"
            if isinstance(response_schema, dict) and self._requires_json_schema(response_schema):
                cfg_kwargs["response_json_schema"] = response_schema
            else:
                schema_obj = self._schema_from_json(response_schema)
                if schema_obj is not None:
                    cfg_kwargs["response_schema"] = schema_obj

        # Always disable AFC - SAIVerse handles function calls manually
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)

        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config

        get_llm_logger().debug(
            "Gemini generate config model=%s use_tools=%s cfg=%s",
            self.model, use_tools, cfg_kwargs,
        )

        max_retries = 3
        model_id = self.model
        
        for attempt in range(max_retries):
            try:
                if use_tools:
                    # Non-streaming for tool detection
                    resp = active_client.models.generate_content(
                        model=model_id,
                        contents=contents,
                        config=types.GenerateContentConfig(**cfg_kwargs),
                    )
                else:
                    # Streaming with chunk timeout for text-only
                    stream = active_client.models.generate_content_stream(
                        model=model_id,
                        contents=contents,
                        config=types.GenerateContentConfig(**cfg_kwargs),
                    )
                    all_parts: List[Any] = []
                    last_chunk_time = time.time()
                    saw_any_chunk = False
                    last_chunk = None

                    for chunk in stream:
                        last_chunk = chunk
                        now = time.time()
                        if now - last_chunk_time > CHUNK_TIMEOUT_SECONDS and not saw_any_chunk:
                            raise ChunkTimeoutError(
                                f"No response received within {CHUNK_TIMEOUT_SECONDS} seconds"
                            )
                        last_chunk_time = now
                        saw_any_chunk = True
                        get_llm_logger().debug("Gemini stream chunk:\n%s", chunk)
                        
                        if chunk.candidates:
                            candidate = chunk.candidates[0]
                            if candidate.content and candidate.content.parts:
                                all_parts.extend(candidate.content.parts)
                    
                    if not saw_any_chunk:
                        raise EmptyResponseError("No chunks received from stream")
                    if not all_parts:
                        raise EmptyResponseError("No parts in stream response")

                    # Store usage from last chunk (uses self.config_key for pricing)
                    if last_chunk:
                        usage = getattr(last_chunk, "usage_metadata", None)
                        if usage:
                            # Debug: log available fields in usage_metadata
                            logging.info("[DEBUG] Gemini usage_metadata attrs: %s",
                                        [a for a in dir(usage) if not a.startswith('_')])
                            logging.info("[DEBUG] Gemini usage_metadata values: prompt=%s, candidates=%s, cached=%s, total=%s",
                                        getattr(usage, "prompt_token_count", None),
                                        getattr(usage, "candidates_token_count", None),
                                        getattr(usage, "cached_content_token_count", None),
                                        getattr(usage, "total_token_count", None))
                            cached = getattr(usage, "cached_content_token_count", 0) or 0
                            self._store_usage(
                                input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                                output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                                cached_tokens=cached,
                            )

                    # Debug: log what's in all_parts before processing
                    for i, part in enumerate(all_parts):
                        logging.info(
                            "[gemini] all_parts[%d]: type=%s, thought=%s, text=%r, attrs=%s",
                            i,
                            type(part).__name__,
                            getattr(part, "thought", "N/A"),
                            (getattr(part, "text", None) or "")[:100] if getattr(part, "text", None) else None,
                            [a for a in dir(part) if not a.startswith("_")]
                        )

                    text, reasoning_entries = self._separate_parts(all_parts)
                    self._store_reasoning(reasoning_entries)

                    if response_schema:
                        logging.info("[gemini] Structured output: text=%r, len=%d", text[:200] if text else "(empty)", len(text))
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict):
                                return parsed
                        except json.JSONDecodeError as e:
                            logging.warning("[gemini] Failed to parse structured output: %s", e)
                            from .exceptions import InvalidRequestError
                            raise InvalidRequestError(
                                "Failed to parse JSON response from structured output",
                                e,
                                user_message="LLMからの応答を解析できませんでした。再度お試しください。"
                            ) from e

                    # Check for empty streaming response (no response_schema and empty/whitespace-only text)
                    if not text.strip():
                        logging.error(
                            "[gemini] Empty streaming response (attempt %d/%d). "
                            "Received parts but text is empty/whitespace-only.",
                            attempt + 1, max_retries
                        )
                        continue

                    prefix = "\n".join(history_snippets)
                    return prefix + ("\n" if prefix and text else "") + text

                # Process non-streaming response (tool mode)
                get_llm_logger().debug("Gemini raw:\n%s", resp)

                # Store usage information (uses self.config_key for pricing)
                usage = getattr(resp, "usage_metadata", None)
                logging.info("[DEBUG] Gemini non-stream usage_metadata: %s", usage)
                if usage:
                    logging.info("[DEBUG] Gemini non-stream usage attrs: %s",
                                [a for a in dir(usage) if not a.startswith('_')])
                    logging.info("[DEBUG] Gemini non-stream usage values: prompt=%s, candidates=%s, cached=%s",
                                getattr(usage, "prompt_token_count", None),
                                getattr(usage, "candidates_token_count", None),
                                getattr(usage, "cached_content_token_count", None))
                    cached = getattr(usage, "cached_content_token_count", 0) or 0
                    self._store_usage(
                        input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                        output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                        cached_tokens=cached,
                    )

                if not resp.candidates:
                    logging.warning("[gemini] No candidates (attempt %d/%d)", attempt + 1, max_retries)
                    continue
                    
                candidate = resp.candidates[0]

                # Check for safety filter block
                finish_reason = getattr(candidate, "finish_reason", None)
                if finish_reason is not None:
                    finish_reason_str = str(finish_reason).upper()
                    if "SAFETY" in finish_reason_str:
                        safety_ratings = getattr(candidate, "safety_ratings", [])
                        blocked_categories = [
                            str(getattr(r, "category", "unknown"))
                            for r in (safety_ratings or [])
                            if str(getattr(r, "probability", "")).upper() in ("HIGH", "MEDIUM")
                        ]
                        detail = f"Blocked categories: {blocked_categories}" if blocked_categories else ""
                        logging.warning("[gemini] Response blocked by safety filter: %s", detail)
                        raise SafetyFilterError(
                            f"Content blocked by safety filter. {detail}",
                            user_message="コンテンツが安全性フィルターによりブロックされました。入力内容を変更してお試しください。"
                        )

                if not candidate.content or not candidate.content.parts:
                    logging.warning("[gemini] No content/parts (attempt %d/%d)", attempt + 1, max_retries)
                    continue

                # Extract text, reasoning, and function calls
                text_parts = []
                reasoning_entries = []
                function_call_part = None

                for part in candidate.content.parts:
                    part_fcall = getattr(part, "function_call", None)
                    if part_fcall:
                        function_call_part = part_fcall
                        continue
                    
                    is_thought = is_truthy_flag(getattr(part, "thought", None))
                    part_text = getattr(part, "text", None)
                    if not part_text:
                        continue
                    if is_thought:
                        reasoning_entries.append({"title": "Thought", "text": part_text.strip()})
                    else:
                        text_parts.append(part_text)

                text = "".join(text_parts)
                self._store_reasoning(reasoning_entries)

                # Check candidate-level function_call for backwards compatibility
                if not function_call_part:
                    function_call_part = getattr(candidate, "function_call", None)

                # Return appropriate type based on what was found
                if function_call_part:
                    fcall_name = getattr(function_call_part, "name", None)
                    fcall_args = getattr(function_call_part, "args", {}) or {}
                    
                    if fcall_name and isinstance(fcall_name, str):
                        if text:
                            logging.info("[gemini] Returning both: text + tool_call (%s)", fcall_name)
                            return {
                                "type": "both",
                                "content": text,
                                "tool_name": fcall_name,
                                "tool_args": dict(fcall_args),
                                "raw_function_call": function_call_part,
                            }
                        else:
                            logging.info("[gemini] Returning tool_call: %s", fcall_name)
                            return {
                                "type": "tool_call",
                                "tool_name": fcall_name,
                                "tool_args": dict(fcall_args),
                                "raw_function_call": function_call_part,
                            }

                # Text-only response in tool mode
                # Check for empty text response (no tool call and empty/whitespace-only text)
                if not text.strip():
                    logging.error(
                        "[gemini] Empty text response without tool call (attempt %d/%d). "
                        "Model returned Part(text='') with finish_reason=STOP. "
                        "prompt_tokens=%s, response_id=%s",
                        attempt + 1, max_retries,
                        getattr(usage, "prompt_token_count", None) if usage else None,
                        getattr(resp, "response_id", None)
                    )
                    continue

                logging.info("[gemini] Returning text response")
                return {"type": "text", "content": text}

            except EmptyResponseError as e:
                logging.warning("Gemini empty response (attempt %d/%d): %s", attempt + 1, max_retries, e)
                continue
            except Exception as exc:
                if self._is_timeout_error(exc):
                    logging.warning(
                        "Gemini timeout (attempt %d/%d): %s",
                        attempt + 1, max_retries, type(exc).__name__
                    )
                    try:
                        total_chars = sum(
                            len(m.get("content", "") or "") if isinstance(m, dict) else 0
                            for m in messages
                        )
                        image_count = sum(
                            len(iter_image_media(m.get("metadata")) or [])
                            if isinstance(m, dict) else 0
                            for m in messages
                        )
                        client_type = "paid" if active_client is self.paid_client else "free"
                        log_timeout_event(
                            timeout_type=type(exc).__name__,
                            model=model_id,
                            wait_duration_sec=600.0,
                            message_count=len(messages),
                            total_chars=total_chars,
                            image_count=image_count,
                            has_tools=use_tools,
                            use_stream=not use_tools,
                            client_type=client_type,
                            retry_attempt=attempt + 1,
                        )
                    except Exception as log_exc:
                        logging.debug("Failed to log timeout diagnostics: %s", log_exc)
                    if active_client is self.free_client and self.paid_client:
                        logging.info("Switching to paid Gemini API key after timeout")
                        active_client = self.paid_client
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                    continue
                if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(exc):
                    logging.info("Retrying with paid Gemini API key due to rate limit")
                    active_client = self.paid_client
                    continue
                logging.exception("Gemini call failed")
                raise self._convert_to_llm_error(exc, "API call") from exc

        logging.error("Gemini API call failed after %d retries", max_retries)
        if use_tools:
            return {"type": "text", "content": ""}
        raise LLMEmptyResponseError(
            f"Gemini API call failed after {max_retries} retries with empty responses",
            user_message="何度も空の応答が返されました。しばらく待ってから再度お試しください。"
        )

    def _separate_parts(self, parts: List[Any]) -> Tuple[str, List[Dict[str, str]]]:
        reasoning_entries: List[Dict[str, str]] = []
        text_segments: List[str] = []
        counter = 1
        for part in parts or []:
            if part is None:
                continue
            is_thought = is_truthy_flag(getattr(part, "thought", None))
            text = getattr(part, "text", None)
            if not text:
                continue
            if is_thought:
                reasoning_entries.append({"title": f"Thought {counter}", "text": text.strip()})
                counter += 1
            else:
                text_segments.append(text)
        return "".join(text_segments), reasoning_entries

    def generate_stream(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[list] | None = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        disable_stream = os.getenv("SAIVERSE_DISABLE_GEMINI_STREAMING")
        if disable_stream and disable_stream.lower() not in {"0", "false", "off"}:
            logging.info("SAIVERSE_DISABLE_GEMINI_STREAMING set; using non-streaming Gemini call")
            result = self.generate(
                messages,
                tools=tools or [],
                history_snippets=history_snippets,
                response_schema=response_schema,
                temperature=temperature,
            )
            yield result
            return

        tools_spec = GEMINI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = history_snippets or []
        self._store_reasoning([])
        reasoning_chunks: List[str] = []

        if use_tools:
            decision = route(self._last_user(messages), tools_spec)
            logging.info(
                "Router decision:\n%s",
                json.dumps(decision, indent=2, ensure_ascii=False),
            )
            tool_cfg = types.ToolConfig(
                functionCallingConfig=(
                    types.FunctionCallingConfig(
                        mode="ANY",
                        allowedFunctionNames=[decision["tool"]],
                    )
                    if decision["call"] == "yes"
                    else types.FunctionCallingConfig(mode="AUTO")
                )
            )
        else:
            tool_cfg = None

        active_client = self.client
        try:
            stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature, response_schema)
        except Exception as exc:
            if active_client is self.free_client and self.paid_client and self._is_rate_limit_error(exc):
                logging.info("Retrying with paid Gemini API key due to rate limit")
                active_client = self.paid_client
                stream = self._start_stream(active_client, messages, tools_spec, tool_cfg, use_tools, temperature, response_schema)
            else:
                logging.exception("Gemini call failed")
                raise self._convert_to_llm_error(exc, "streaming")

        fcall: Optional[types.FunctionCall] = None
        prefix_yielded = False
        seen_stream_texts: Dict[int, str] = {}
        thought_seen: Dict[int, str] = {}

        saw_chunks = False
        stream_completed = False
        last_chunk = None

        for chunk in stream:
            last_chunk = chunk
            get_llm_logger().debug("Gemini stream chunk:\n%s", chunk)
            if not chunk.candidates:
                continue
            candidate = chunk.candidates[0]
            get_llm_logger().debug("Gemini stream candidate:\n%s", candidate)
            saw_chunks = True

            # Check for safety filter block in streaming
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason is not None:
                finish_reason_str = str(finish_reason).upper()
                if "SAFETY" in finish_reason_str:
                    safety_ratings = getattr(candidate, "safety_ratings", [])
                    blocked_categories = [
                        str(getattr(r, "category", "unknown"))
                        for r in (safety_ratings or [])
                        if str(getattr(r, "probability", "")).upper() in ("HIGH", "MEDIUM")
                    ]
                    detail = f"Blocked categories: {blocked_categories}" if blocked_categories else ""
                    logging.warning("[gemini] Stream blocked by safety filter: %s", detail)
                    raise SafetyFilterError(
                        f"Content blocked by safety filter. {detail}",
                        user_message="コンテンツが安全性フィルターによりブロックされました。入力内容を変更してお試しください。"
                    )

            if not candidate.content or not candidate.content.parts:
                continue
            candidate_index = getattr(candidate, "index", 0)
            for part_idx, part in enumerate(candidate.content.parts):
                if getattr(part, "function_call", None) and fcall is None:
                    get_llm_logger().debug("Gemini function_call (part %s): %s", part_idx, part.function_call)
                    fcall = part.function_call
                elif is_truthy_flag(getattr(part, "thought", None)):
                    text_val = getattr(part, "text", None) or ""
                    if text_val:
                        previous = thought_seen.get(candidate_index, "")
                        if text_val.startswith(previous):
                            delta = text_val[len(previous) :]
                            if delta:
                                reasoning_chunks.append(delta)
                                thought_seen[candidate_index] = previous + delta
                        else:
                            reasoning_chunks.append(text_val)
                            thought_seen[candidate_index] = previous + text_val

            combined_text = "".join(
                getattr(part, "text", None) or ""
                for part in candidate.content.parts
                if getattr(part, "text", None) and not is_truthy_flag(getattr(part, "thought", None))
            )
            if not combined_text:
                continue

            previous_text = seen_stream_texts.get(candidate_index, "")
            new_text = (
                combined_text[len(previous_text) :]
                if combined_text.startswith(previous_text)
                else combined_text
            )
            if not new_text:
                continue
            get_llm_logger().debug("Gemini text delta: %s", new_text)
            if not prefix_yielded and history_snippets:
                yield "\n".join(history_snippets) + "\n"
                prefix_yielded = True
            yield new_text
            seen_stream_texts[candidate_index] = combined_text
            finish_reason = getattr(candidate, "finish_reason", None)
            if finish_reason:
                stream_completed = True

        if fcall is None and saw_chunks and not stream_completed:
            # Log as warning but continue with received content
            logging.warning("Gemini stream ended without completion signal, but content was received. Continuing with partial response.")

        # Store usage from last chunk (uses self.config_key for pricing)
        logging.info("[DEBUG] generate_stream last_chunk exists: %s", last_chunk is not None)
        if last_chunk:
            usage = getattr(last_chunk, "usage_metadata", None)
            logging.info("[DEBUG] generate_stream usage_metadata: %s", usage)
            if usage:
                logging.info("[DEBUG] generate_stream usage attrs: %s",
                            [a for a in dir(usage) if not a.startswith('_')])
                logging.info("[DEBUG] generate_stream usage values: prompt=%s, candidates=%s, cached=%s",
                            getattr(usage, "prompt_token_count", None),
                            getattr(usage, "candidates_token_count", None),
                            getattr(usage, "cached_content_token_count", None))
                cached = getattr(usage, "cached_content_token_count", 0) or 0
                self._store_usage(
                    input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                    output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
                    cached_tokens=cached,
                )

        # Store reasoning
        self._store_reasoning(merge_reasoning_strings(reasoning_chunks))

        # Store tool detection result if function call was detected
        if fcall is not None:
            # Collect all text that was yielded
            all_text = "".join(seen_stream_texts.values())
            fcall_name = getattr(fcall, "name", None)
            fcall_args = getattr(fcall, "args", {}) or {}

            if all_text.strip():
                self._store_tool_detection({
                    "type": "both",
                    "content": all_text,
                    "tool_name": fcall_name,
                    "tool_args": dict(fcall_args),
                    "raw_function_call": fcall,
                })
            else:
                self._store_tool_detection({
                    "type": "tool_call",
                    "tool_name": fcall_name,
                    "tool_args": dict(fcall_args),
                    "raw_function_call": fcall,
                })
            logging.info("[gemini] Tool detection stored: %s", fcall_name)
        else:
            # No tool call - store text-only result
            all_text = "".join(seen_stream_texts.values())
            self._store_tool_detection({
                "type": "text",
                "content": all_text,
            })

    def _start_stream(
        self,
        client: genai.Client,
        messages: List[Any],
        tools_spec: Optional[list],
        tool_cfg: Optional[types.ToolConfig],
        use_tools: bool,
        temperature: float | None,
        response_schema: Optional[Dict[str, Any]] = None,
    ):
        sys_msg, contents = self._convert_messages(messages)
        cfg_kwargs: Dict[str, Any] = {
            "system_instruction": sys_msg,
            "safety_settings": self._build_safety_settings(),
        }
        if use_tools:
            cfg_kwargs["tools"] = merge_tools_for_gemini(tools_spec)
            cfg_kwargs["tool_config"] = tool_cfg
        # Response schema configuration
        if response_schema and not use_tools:
            cfg_kwargs["response_mime_type"] = "application/json"
            if isinstance(response_schema, dict) and self._requires_json_schema(response_schema):
                cfg_kwargs["response_json_schema"] = response_schema
            else:
                schema_obj = self._schema_from_json(response_schema)
                if schema_obj is not None:
                    cfg_kwargs["response_schema"] = schema_obj
        # Always disable AFC - SAIVerse handles function calls manually via Playbook
        cfg_kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            cfg_kwargs["thinking_config"] = thinking_config

        # Temperature: argument > _request_params
        effective_temp = temperature if temperature is not None else self._request_params.get("temperature")
        if effective_temp is not None:
            cfg_kwargs["temperature"] = effective_temp

        # Apply other request params (top_p, top_k, max_output_tokens, etc.)
        for param in ("top_p", "top_k", "max_output_tokens", "stop_sequences"):
            if param in self._request_params:
                cfg_kwargs[param] = self._request_params[param]
        get_llm_logger().debug(
            "Gemini stream config model=%s use_tools=%s cfg=%s",
            self.model,
            use_tools,
            cfg_kwargs,
        )
        if use_tools:
            cfg_kwargs.setdefault("tool_config", tool_cfg)
        return client.models.generate_content_stream(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(**cfg_kwargs),
        )

    def generate_with_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Any] | None = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Dict[str, Any]:
        """DEPRECATED: Use generate(messages, tools=[...]) instead.
        
        This method is kept for backward compatibility with existing code.
        It simply delegates to generate() with tools specified.
        """
        import warnings
        warnings.warn(
            "generate_with_tool_detection() is deprecated. Use generate(messages, tools=[...]) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        tools_spec = tools or []
        if not tools_spec:
            # If no tools, return text-only format for compatibility
            result = self.generate(messages, temperature=temperature)
            if isinstance(result, str):
                return {"type": "text", "content": result}
            return result
        return self.generate(messages, tools=tools_spec, temperature=temperature)

__all__ = [
    "GeminiClient",
    "GEMINI_SAFETY_CONFIG",
    "merge_tools_for_gemini",
    "GROUNDING_TOOL",
    "genai",
]
