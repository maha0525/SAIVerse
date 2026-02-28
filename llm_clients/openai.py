"""OpenAI (and compatible) chat completion client."""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import openai
from openai import OpenAI

from saiverse.media_utils import iter_image_media, load_image_bytes_for_llm
from tools import OPENAI_TOOLS_SPEC
from saiverse.llm_router import route

from . import openai_errors
from .base import LLMClient, get_llm_logger
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
from .utils import (
    compute_allowed_attachment_keys,
    content_to_text,
    image_summary_note,
    merge_reasoning_strings,
    obj_to_dict,
    parse_attachment_limit,
)


# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF = 1.0  # seconds


def _is_rate_limit_error(err: Exception) -> bool:
    """Check if the error is a rate limit that should be retried."""
    return openai_errors.is_rate_limit_error(err)


def _is_server_error(err: Exception) -> bool:
    """Check if the error is a server error (5xx) that should be retried."""
    return openai_errors.is_server_error(err)


def _is_timeout_error(err: Exception) -> bool:
    """Check if the error is a timeout that should be retried."""
    return openai_errors.is_timeout_error(err)


def _should_retry(err: Exception) -> bool:
    """Check if the error should trigger a retry."""
    return openai_errors.should_retry(err)


def _is_authentication_error(err: Exception) -> bool:
    """Check if the error is an authentication error."""
    return openai_errors.is_authentication_error(err)


def _is_payment_error(err: Exception) -> bool:
    """Check if the error is a payment/billing error (402) or quota exhaustion."""
    return openai_errors.is_payment_error(err)


def _is_content_policy_error(err: Exception) -> bool:
    """Check if the error is a content policy violation (prompt-level block)."""
    return openai_errors.is_content_policy_error(err)


def _convert_to_llm_error(err: Exception, context: str = "API call") -> LLMError:
    """Convert a generic exception to an appropriate LLMError subclass."""
    return openai_errors.convert_to_llm_error(err, context)


def _prepare_openai_messages(messages: List[Any], supports_images: bool, max_image_bytes: Optional[int] = None, convert_system_to_user: bool = False, reasoning_passback_field: Optional[str] = None) -> List[Any]:
    """
    Prepare messages for OpenAI API by extracting only allowed fields.
    Removes SAIMemory-specific fields (id, thread_id, created_at, metadata).

    Args:
        messages: Raw message list
        supports_images: Whether the model supports images
        max_image_bytes: Optional max bytes for images
        convert_system_to_user: If True, converts system messages (except the first one)
                                to user messages wrapped in <system></system> tags
    """
    # OpenAI API standard fields
    ALLOWED_FIELDS = {"role", "content", "name", "tool_calls", "tool_call_id"}

    def _is_empty_message(msg: Dict[str, Any]) -> bool:
        """Check if a message is empty (no content and no tool_calls)."""
        role = msg.get("role")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")

        # System and user messages with empty content are invalid
        if role in ("assistant", "system", "user"):
            # Content is empty if it's None, empty string, or empty list
            content_empty = not content or (isinstance(content, (list, str)) and len(content) == 0)
            # Assistant messages must have content OR tool_calls
            if role == "assistant":
                return content_empty and not tool_calls
            # System/user messages must have content
            return content_empty
        return False

    # ------------------------------------------------------------------
    # Pre-scan: build attachment cache and metadata flags
    # ------------------------------------------------------------------
    max_image_embeds = parse_attachment_limit("OPENAI")
    attachment_cache: Dict[int, List[Dict[str, Any]]] = {}
    skip_summary_indices: set = set()
    exempt_indices: set = set()

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        metadata = msg.get("metadata")
        if isinstance(metadata, dict):
            if metadata.get("__skip_image_summary__"):
                skip_summary_indices.add(idx)
            if metadata.get("__visual_context__"):
                exempt_indices.add(idx)
        media_items = iter_image_media(metadata)
        if media_items:
            attachment_cache[idx] = media_items

    allowed_attachment_keys = compute_allowed_attachment_keys(
        attachment_cache, max_image_embeds, exempt_indices,
    )
    logging.debug(
        "[openai] attachment limit=%s, cached=%d msgs with images",
        "∞" if max_image_embeds is None else max_image_embeds,
        len(attachment_cache),
    )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    prepared: List[Any] = []
    seen_non_system = False  # Track if we've seen any non-system messages

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            prepared.append(msg)
            continue

        role = msg.get("role")
        if isinstance(role, str) and role.lower() == "host":
            role = "system"

        # Skip empty messages early
        if _is_empty_message(msg):
            logging.debug("Skipping empty message with role=%s", role)
            continue

        metadata = msg.get("metadata")
        attachments = attachment_cache.get(idx, [])
        skip_summary = idx in skip_summary_indices

        # Extract only allowed fields
        clean_msg: Dict[str, Any] = {}
        for field in ALLOWED_FIELDS:
            if field in msg:
                clean_msg[field] = msg[field]

        # Override role if it was "host"
        if role:
            clean_msg["role"] = role

        # Preserve reasoning_details for multi-turn reasoning pass-back
        # Only pass back structured data (list of objects); skip plain strings
        # which were stored by older code and would cause provider errors
        if reasoning_passback_field and role == "assistant":
            rd = (metadata or {}).get("reasoning_details") if isinstance(metadata, dict) else None
            if isinstance(rd, list):
                clean_msg[reasoning_passback_field] = rd

        # Skip if the cleaned message is also empty
        if _is_empty_message(clean_msg):
            logging.debug("Skipping empty cleaned message with role=%s", role)
            continue

        # Convert system messages to user messages with <system> tags if needed
        # Only convert system messages that appear after non-system messages
        if convert_system_to_user and role == "system" and seen_non_system:
            content = content_to_text(msg.get("content", ""))
            clean_msg["role"] = "user"
            clean_msg["content"] = f"<system>\n{content}\n</system>"
            prepared.append(clean_msg)
            continue

        # Track if we've seen non-system messages
        if role != "system":
            seen_non_system = True

        if not attachments:
            prepared.append(clean_msg)
            continue

        text = content_to_text(msg.get("content"))
        if supports_images and role == "user":
            parts: List[Dict[str, Any]] = []
            if text:
                parts.append({"type": "text", "text": text})
            for att_idx, att in enumerate(attachments):
                # Determine whether to embed this attachment as binary
                should_embed = (
                    allowed_attachment_keys is None
                    or (idx, att_idx) in allowed_attachment_keys
                )
                if should_embed:
                    data, effective_mime = load_image_bytes_for_llm(
                        att["path"], att["mime_type"], max_bytes=max_image_bytes,
                    )
                    if data and effective_mime:
                        b64 = base64.b64encode(data).decode("ascii")
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{effective_mime};base64,{b64}"},
                            }
                        )
                        continue
                    else:
                        logging.warning(
                            "Image file not found or unreadable, skipping attachment: %s",
                            att.get("uri") or att.get("path"),
                        )
                # Not embedding — produce a text summary instead
                note = image_summary_note(
                    att["path"], att["mime_type"],
                    att.get("uri", att.get("path", "unknown")),
                    skip_summary=skip_summary,
                )
                parts.append({"type": "text", "text": note})
            clean_msg["content"] = parts if parts else text
            prepared.append(clean_msg)
        else:
            # Model doesn't support images or non-user role — use text summaries
            note_lines: List[str] = []
            if text:
                note_lines.append(text)
            for att in attachments:
                note = image_summary_note(
                    att["path"], att["mime_type"],
                    att.get("uri", att.get("path", "unknown")),
                    skip_summary=skip_summary,
                )
                note_lines.append(note)
            clean_msg["content"] = "\n".join(note_lines)
            prepared.append(clean_msg)
    return prepared


def _extract_reasoning_from_openai_message(message: Any) -> Tuple[str, List[Dict[str, str]], Any]:
    """Extract text, reasoning entries, and raw reasoning_details from an OpenAI message.

    Returns:
        (final_text, reasoning_entries, reasoning_details)
        reasoning_details is the raw provider data for multi-turn pass-back (or None).
    """
    msg_dict = obj_to_dict(message) or {}
    content = msg_dict.get("content")
    reasoning_entries: List[Dict[str, str]] = []
    text_segments: List[str] = []

    def _append_reasoning(text: str, title: Optional[str] = None) -> None:
        text = (text or "").strip()
        if text:
            reasoning_entries.append({"title": title or "", "text": text})

    if isinstance(content, list):
        for part in content:
            part_dict = obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if not text:
                continue
            if ptype in {"reasoning", "thinking", "analysis"}:
                _append_reasoning(text, part_dict.get("title"))
            elif ptype in {"output_text", "text", None}:
                text_segments.append(text)
    elif isinstance(content, str):
        text_segments.append(content)

    reasoning_content = msg_dict.get("reasoning_content")
    if isinstance(reasoning_content, str):
        _append_reasoning(reasoning_content)

    if msg_dict.get("reasoning") and isinstance(msg_dict["reasoning"], dict):
        rc = msg_dict["reasoning"].get("content")
        if isinstance(rc, str):
            _append_reasoning(rc)

    # Capture raw reasoning_details for multi-turn pass-back (e.g., OpenRouter)
    reasoning_details = msg_dict.get("reasoning_details")

    final_text = "".join(text_segments)
    return final_text, reasoning_entries, reasoning_details


def _extract_reasoning_from_delta(delta: Any) -> List[str]:
    reasoning_chunks: List[str] = []
    delta_dict = obj_to_dict(delta)
    if not isinstance(delta_dict, dict):
        return reasoning_chunks

    # Check "reasoning" field (list of items or string)
    raw_reasoning = delta_dict.get("reasoning")
    if isinstance(raw_reasoning, list):
        for item in raw_reasoning:
            item_dict = obj_to_dict(item) or {}
            text = item_dict.get("text") or item_dict.get("content") or ""
            if text:
                reasoning_chunks.append(text)
    elif isinstance(raw_reasoning, str):
        reasoning_chunks.append(raw_reasoning)

    # Check "reasoning_content" field (used by o-series models, NIM)
    reasoning_content = delta_dict.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content:
        reasoning_chunks.append(reasoning_content)

    # Check "reasoning_details" field (used by OpenRouter)
    # Extract text for display; raw objects are collected separately for passback
    raw_rd = delta_dict.get("reasoning_details")
    if isinstance(raw_rd, list) and not reasoning_chunks:
        # Only extract text if no other reasoning source was found (avoid duplicates)
        for item in raw_rd:
            item_dict = obj_to_dict(item) or item if isinstance(item, dict) else {}
            text = item_dict.get("text") or item_dict.get("summary") or ""
            if text:
                reasoning_chunks.append(text)

    return reasoning_chunks


def _extract_raw_reasoning_details_from_delta(delta: Any) -> List[Dict[str, Any]]:
    """Extract raw reasoning_details objects from a streaming delta for multi-turn passback."""
    delta_dict = obj_to_dict(delta)
    if not isinstance(delta_dict, dict):
        return []
    raw_rd = delta_dict.get("reasoning_details")
    if not isinstance(raw_rd, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in raw_rd:
        d = obj_to_dict(item) if not isinstance(item, dict) else item
        if isinstance(d, dict):
            result.append(d)
    return result


def _merge_streaming_reasoning_details(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge incremental reasoning_details chunks from streaming into consolidated entries.

    Groups by 'index' field and concatenates 'text'/'summary' values.
    """
    by_index: Dict[int, Dict[str, Any]] = {}
    for item in items:
        idx = item.get("index", 0)
        if idx not in by_index:
            by_index[idx] = dict(item)
        else:
            existing = by_index[idx]
            for text_key in ("text", "summary"):
                chunk_text = item.get(text_key, "")
                if chunk_text:
                    existing[text_key] = existing.get(text_key, "") + chunk_text
    return [by_index[k] for k in sorted(by_index.keys())]


def _process_openai_stream_content(content: Any) -> Tuple[str, List[str]]:
    reasoning_chunks: List[str] = []
    text_fragments: List[str] = []

    if isinstance(content, list):
        for part in content:
            part_dict = obj_to_dict(part) or {}
            ptype = part_dict.get("type")
            text = part_dict.get("text") or part_dict.get("content") or ""
            if not text:
                continue
            if ptype in {"reasoning", "thinking", "analysis"}:
                reasoning_chunks.append(text)
            else:
                text_fragments.append(text)
    elif isinstance(content, str):
        text_fragments.append(content)

    return "".join(text_fragments), reasoning_chunks


class OpenAIClient(LLMClient):
    """Client for OpenAI-compatible chat completions API."""

    def __init__(
        self,
        model: str = "gpt-4.1",
        *,
        supports_images: bool = False,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key_env: Optional[str] = None,
        request_kwargs: Optional[Dict[str, Any]] = None,
        max_image_bytes: Optional[int] = None,
        convert_system_to_user: bool = False,
        structured_output_backend: Optional[str] = None,
        structured_output_mode: Optional[str] = None,
        reasoning_passback_field: Optional[str] = None,
    ) -> None:
        super().__init__(supports_images=supports_images)
        key_env = api_key_env or "OPENAI_API_KEY"
        api_key = api_key or os.getenv(key_env)
        if not api_key:
            raise AuthenticationError(
                f"{key_env} environment variable is not set.",
                user_message="OpenAI APIキーが設定されていません。管理者にお問い合わせください。"
            )

        client_kwargs: Dict[str, str] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = OpenAI(**client_kwargs)
        self.model = model
        self._request_kwargs: Dict[str, Any] = dict(request_kwargs or {})
        self.max_image_bytes = max_image_bytes
        self.convert_system_to_user = convert_system_to_user
        self.structured_output_backend = structured_output_backend
        self.structured_output_mode = structured_output_mode or "native"
        self.reasoning_passback_field = reasoning_passback_field

    def _create_completion(self, **kwargs: Any):
        return self.client.chat.completions.create(**kwargs)

    def _add_additional_properties(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively add additionalProperties: false and normalize schema for OpenAI strict mode."""
        import copy
        schema = copy.deepcopy(schema)

        def _process(node: Any) -> Any:
            if isinstance(node, dict):
                # Normalize type names (int -> integer, bool -> boolean, float -> number)
                if "type" in node:
                    type_value = node["type"]
                    if type_value == "int":
                        node["type"] = "integer"
                    elif type_value == "bool":
                        node["type"] = "boolean"
                    elif type_value == "float":
                        node["type"] = "number"

                # Add additionalProperties: false to objects
                if node.get("type") == "object" and "additionalProperties" not in node:
                    node["additionalProperties"] = False

                # Complete required array to include all properties (OpenAI strict mode requirement)
                if node.get("type") == "object" and "properties" in node:
                    all_keys = list(node["properties"].keys())
                    existing_required = node.get("required", [])
                    # Add all properties to required if not already present
                    node["required"] = list(set(existing_required + all_keys))

                # Recursively process all values
                return {k: _process(v) for k, v in node.items()}
            elif isinstance(node, list):
                return [_process(item) for item in node]
            else:
                return node

        return _process(schema)

    @staticmethod
    def _inject_schema_prompt(
        messages: List[Dict[str, Any]],
        response_schema: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Inject response schema into messages as a system instruction.

        Used when structured_output_mode is 'json_object': the server only
        guarantees valid JSON, so we describe the expected schema in the
        prompt so the model knows which fields and types to produce.
        """
        schema_text = json.dumps(response_schema, ensure_ascii=False, indent=2)
        instruction = (
            "You must respond with a JSON object that conforms to the following JSON schema.\n"
            "Output ONLY the JSON object with no additional text.\n\n"
            f"```json\n{schema_text}\n```"
        )
        # Prepend as a system message so it takes priority
        return [{"role": "system", "content": instruction}] + list(messages)

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
                  - type: "text" | "tool_call"
                  - content: Generated text (if type is "text")
                  - tool_name: Tool name (if type is "tool_call")
                  - tool_args: Tool arguments dict (if type is "tool_call")
        """
        tools_spec = tools or []
        use_tools = bool(tools_spec)
        snippets: List[str] = list(history_snippets or [])
        self._store_reasoning([])

        if response_schema and use_tools:
            logging.warning("response_schema specified alongside tools; structured output is ignored for tool runs.")
            response_schema = None

        def _build_request_kwargs() -> Dict[str, Any]:
            req = dict(self._request_kwargs)
            if temperature is not None:
                req["temperature"] = temperature
            if response_schema:
                if self.structured_output_mode == "json_object":
                    # json_object mode: only guarantee valid JSON output;
                    # schema adherence is handled by prompt injection
                    req["response_format"] = {"type": "json_object"}
                    logging.debug("[openai] Using json_object mode with prompt-based schema")
                else:
                    # native mode: strict json_schema enforcement
                    schema_name = response_schema.get("title") if isinstance(response_schema, dict) else None
                    openai_schema = self._add_additional_properties(response_schema)
                    json_schema_config: Dict[str, Any] = {
                        "name": schema_name or "saiverse_structured_output",
                        "schema": openai_schema,
                        "strict": True,
                    }
                    response_format_config: Dict[str, Any] = {
                        "type": "json_schema",
                        "json_schema": json_schema_config,
                    }
                    if self.structured_output_backend:
                        json_schema_config["backend"] = self.structured_output_backend
                        response_format_config["backend"] = self.structured_output_backend
                        logging.info("Applying structured_output_backend='%s' to request (both locations)", self.structured_output_backend)
                    req["response_format"] = response_format_config
                    logging.debug("response_format: %s", req["response_format"])
            return req

        # If json_object mode, inject schema into messages as a system instruction
        effective_messages = messages
        if response_schema and self.structured_output_mode == "json_object":
            effective_messages = self._inject_schema_prompt(messages, response_schema)

        # Non-tool mode: return str or dict (if response_schema)
        if not use_tools:
            resp = None
            last_error: Optional[Exception] = None

            for attempt in range(MAX_RETRIES):
                try:
                    resp = self._create_completion(
                        model=self.model,
                        messages=_prepare_openai_messages(effective_messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                        n=1,
                        **_build_request_kwargs(),
                    )
                    break  # Success, exit retry loop
                except Exception as e:
                    last_error = e
                    if _should_retry(e) and attempt < MAX_RETRIES - 1:
                        backoff = INITIAL_BACKOFF * (2 ** attempt)
                        logging.warning(
                            "[openai] Retryable error (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                        )
                        time.sleep(backoff)
                        continue
                    logging.exception("OpenAI call failed")
                    raise _convert_to_llm_error(e, "API call")

            if resp is None:
                if last_error:
                    raise _convert_to_llm_error(last_error, f"API call after {MAX_RETRIES} retries")
                raise LLMEmptyResponseError(f"OpenAI API call failed after {MAX_RETRIES} retries with no response")

            get_llm_logger().debug("OpenAI raw:\n%s", resp.model_dump_json(indent=2))

            # Store usage information
            if resp.usage:
                # Check for cached tokens (OpenAI Prompt Caching)
                cached = 0
                if hasattr(resp.usage, "prompt_tokens_details") and resp.usage.prompt_tokens_details:
                    cached = getattr(resp.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                self._store_usage(
                    input_tokens=resp.usage.prompt_tokens or 0,
                    output_tokens=resp.usage.completion_tokens or 0,
                    cached_tokens=cached,
                )

            choice = resp.choices[0]

            # Check for content filter (output-level block)
            if choice.finish_reason == "content_filter":
                logging.warning(
                    "[openai] Output blocked by content filter. finish_reason=%s",
                    choice.finish_reason,
                )
                raise SafetyFilterError(
                    "OpenAI output blocked by content filter",
                    user_message="生成された内容がOpenAIのコンテンツフィルターによりブロックされました。入力内容を変更してお試しください。",
                )

            text_body, reasoning_entries, reasoning_details = _extract_reasoning_from_openai_message(choice.message)
            if not text_body:
                text_body = choice.message.content or ""
            self._store_reasoning(reasoning_entries)
            self._store_reasoning_details(reasoning_details)

            if response_schema:
                try:
                    parsed = json.loads(text_body)
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError as e:
                    logging.warning("[openai] Failed to parse structured output: %s", e)
                    raise InvalidRequestError(
                        "Failed to parse JSON response from structured output",
                        e,
                        user_message="LLMからの応答を解析できませんでした。再度お試しください。"
                    ) from e
            else:
                # Check for empty response
                if not text_body.strip():
                    logging.error(
                        "[openai] Empty text response. "
                        "Model returned empty content. finish_reason=%s",
                        choice.finish_reason
                    )
                    raise LLMEmptyResponseError("OpenAI returned empty response")
                if snippets:
                    prefix = "\n".join(snippets)
                    return prefix + ("\n" if text_body and prefix else "") + text_body
                return text_body

        # Tool mode: return Dict with tool detection (no execution)
        logging.info("[openai] Sending %d tools to API", len(tools_spec))
        for i, tool in enumerate(tools_spec):
            logging.info("[openai] Tool[%d]: %s", i, tool)

        resp = None
        last_error_tool: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                    tools=tools_spec,
                    tool_choice="auto",
                    n=1,
                    **_build_request_kwargs(),
                )
                break  # Success, exit retry loop
            except Exception as e:
                last_error_tool = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[openai] Retryable error in tool mode (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("OpenAI call failed")
                raise _convert_to_llm_error(e, "API call (tool mode)")

        if resp is None:
            if last_error_tool:
                raise _convert_to_llm_error(last_error_tool, f"API call after {MAX_RETRIES} retries (tool mode)")
            raise LLMEmptyResponseError(f"OpenAI API call failed after {MAX_RETRIES} retries with no response")

        get_llm_logger().debug("OpenAI raw (tool detection):\n%s", resp.model_dump_json(indent=2))

        # Store usage information
        if resp.usage:
            # Check for cached tokens (OpenAI Prompt Caching)
            cached = 0
            if hasattr(resp.usage, "prompt_tokens_details") and resp.usage.prompt_tokens_details:
                cached = getattr(resp.usage.prompt_tokens_details, "cached_tokens", 0) or 0
            self._store_usage(
                input_tokens=resp.usage.prompt_tokens or 0,
                output_tokens=resp.usage.completion_tokens or 0,
                cached_tokens=cached,
            )

        choice = resp.choices[0]

        # Check for content filter (output-level block)
        if choice.finish_reason == "content_filter":
            logging.warning(
                "[openai] Output blocked by content filter (tool mode). finish_reason=%s",
                choice.finish_reason,
            )
            raise SafetyFilterError(
                "OpenAI output blocked by content filter",
                user_message="生成された内容がOpenAIのコンテンツフィルターによりブロックされました。入力内容を変更してお試しください。",
            )

        tool_calls = getattr(choice.message, "tool_calls", [])

        # Extract reasoning if present
        text_body, reasoning_entries, reasoning_details = _extract_reasoning_from_openai_message(choice.message)
        self._store_reasoning(reasoning_entries)
        self._store_reasoning_details(reasoning_details)

        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                logging.warning("Tool call arguments invalid JSON: %s", tc.function.arguments)
                args = {}
            return {
                "type": "tool_call",
                "tool_name": tc.function.name,
                "tool_args": args,
                "raw_message": choice.message,
            }
        else:
            content = text_body or choice.message.content or ""
            # Check for empty text response without tool call
            if not content.strip():
                logging.error(
                    "[openai] Empty text response without tool call. "
                    "Model returned empty content. finish_reason=%s",
                    choice.finish_reason
                )
                raise LLMEmptyResponseError("OpenAI returned empty response without tool call")
            return {"type": "text", "content": content}

    def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[list] | None = None,
        force_tool_choice: Optional[dict | str] = None,
        history_snippets: Optional[List[str]] | None = None,
        response_schema: Optional[Dict[str, Any]] = None,
        *,
        temperature: float | None = None,
        **_: Any,
    ) -> Iterator[str]:
        """
        ユーザ向けに逐次テキストを yield するストリーム版。
        - force_tool_choice: 初回のみ {"type":"function","function":{"name":..}} か "auto"
        - 再帰呼び出し時はデフォルト None → 自動で "auto"
        """
        tools_spec = OPENAI_TOOLS_SPEC if tools is None else tools
        use_tools = bool(tools_spec)
        history_snippets = list(history_snippets or [])
        self._store_reasoning([])
        reasoning_chunks: List[str] = []

        # If json_object mode, inject schema into messages as a system instruction
        effective_messages = messages
        if response_schema and self.structured_output_mode == "json_object":
            effective_messages = self._inject_schema_prompt(messages, response_schema)

        if not use_tools:
            resp = None
            last_stream_error: Optional[Exception] = None

            for attempt in range(MAX_RETRIES):
                try:
                    req_kwargs = dict(self._request_kwargs)
                    if temperature is not None:
                        req_kwargs["temperature"] = temperature
                    if response_schema:
                        if self.structured_output_mode == "json_object":
                            req_kwargs["response_format"] = {"type": "json_object"}
                            logging.debug("[openai] Using json_object mode with prompt-based schema (stream)")
                        else:
                            schema_name = response_schema.get("title") if isinstance(response_schema, dict) else None
                            openai_schema = self._add_additional_properties(response_schema)
                            json_schema_config: Dict[str, Any] = {
                                "name": schema_name or "saiverse_structured_output",
                                "schema": openai_schema,
                                "strict": True,
                            }
                            response_format_config: Dict[str, Any] = {
                                "type": "json_schema",
                                "json_schema": json_schema_config,
                            }
                            if self.structured_output_backend:
                                json_schema_config["backend"] = self.structured_output_backend
                                response_format_config["backend"] = self.structured_output_backend
                                logging.info("Applying structured_output_backend='%s' to request (stream)", self.structured_output_backend)
                            req_kwargs["response_format"] = response_format_config
                    else:
                        req_kwargs["stream"] = True
                        req_kwargs["stream_options"] = {"include_usage": True}
                    resp = self._create_completion(
                        model=self.model,
                        messages=_prepare_openai_messages(effective_messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                        n=1,
                        **req_kwargs,
                    )
                    break  # Success, exit retry loop
                except Exception as e:
                    last_stream_error = e
                    if _should_retry(e) and attempt < MAX_RETRIES - 1:
                        backoff = INITIAL_BACKOFF * (2 ** attempt)
                        logging.warning(
                            "[openai] Retryable streaming error (attempt %d/%d): %s. Retrying in %.1fs...",
                            attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                        )
                        time.sleep(backoff)
                        continue
                    logging.exception("OpenAI call failed")
                    raise _convert_to_llm_error(e, "streaming")

            if resp is None:
                if last_stream_error:
                    raise _convert_to_llm_error(last_stream_error, f"streaming after {MAX_RETRIES} retries")
                raise LLMEmptyResponseError(f"OpenAI streaming failed after {MAX_RETRIES} retries with no response")

            # Handle streaming vs non-streaming response
            if req_kwargs.get("stream"):
                # Streaming mode: iterate over chunks
                prefix = "\n".join(history_snippets)
                if prefix:
                    yield prefix + "\n"
                last_chunk = None
                last_finish_reason = None
                reasoning_details_raw: List[Dict[str, Any]] = []
                for chunk in resp:
                    last_chunk = chunk
                    if chunk.choices and chunk.choices[0]:
                        fr = chunk.choices[0].finish_reason
                        if fr is not None:
                            last_finish_reason = fr
                    if chunk.choices and chunk.choices[0].delta:
                        delta = chunk.choices[0].delta

                        # Extract reasoning from delta attributes
                        extra_reasoning = _extract_reasoning_from_delta(delta)
                        for r in extra_reasoning:
                            reasoning_chunks.append(r)
                            yield {"type": "thinking", "content": r}

                        # Collect raw reasoning_details objects for multi-turn passback
                        reasoning_details_raw.extend(_extract_raw_reasoning_details_from_delta(delta))

                        content = delta.content
                        if content:
                            # Check for reasoning in structured content (list with type="reasoning")
                            text_fragment, reasoning_piece = _process_openai_stream_content(content)
                            for r in reasoning_piece:
                                reasoning_chunks.append(r)
                                yield {"type": "thinking", "content": r}
                            if text_fragment:
                                yield text_fragment
                # Check for content filter in streaming
                if last_finish_reason == "content_filter":
                    logging.warning(
                        "[openai] Stream output blocked by content filter. finish_reason=%s",
                        last_finish_reason,
                    )
                    raise SafetyFilterError(
                        "OpenAI streaming output blocked by content filter",
                        user_message="生成された内容がOpenAIのコンテンツフィルターによりブロックされました。入力内容を変更してお試しください。",
                    )

                # Store usage from last chunk (when stream_options.include_usage=True)
                if last_chunk and hasattr(last_chunk, "usage") and last_chunk.usage:
                    # Check for cached tokens (OpenAI Prompt Caching)
                    cached = 0
                    if hasattr(last_chunk.usage, "prompt_tokens_details") and last_chunk.usage.prompt_tokens_details:
                        cached = getattr(last_chunk.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                    self._store_usage(
                        input_tokens=last_chunk.usage.prompt_tokens or 0,
                        output_tokens=last_chunk.usage.completion_tokens or 0,
                        cached_tokens=cached,
                    )
                # Store reasoning collected during streaming
                self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
                # Store reasoning_details for multi-turn pass-back (structured objects only)
                if reasoning_details_raw:
                    self._store_reasoning_details(_merge_streaming_reasoning_details(reasoning_details_raw))
            else:
                # Non-streaming mode (response_schema case)
                choice = resp.choices[0]
                text_body, reasoning_entries, reasoning_details = _extract_reasoning_from_openai_message(choice.message)
                if not text_body:
                    text_body = choice.message.content or ""
                self._store_reasoning(reasoning_entries)
                self._store_reasoning_details(reasoning_details)
                prefix = "\n".join(history_snippets) + ("\n" if history_snippets and text_body else "")
                if prefix:
                    yield prefix
                if text_body:
                    yield text_body
            return

        if force_tool_choice is None:
            if tools is not None:
                # Explicit tools from runtime — let LLM decide natively
                force_tool_choice = "auto"
            else:
                # Legacy mode (tools=None → OPENAI_TOOLS_SPEC) — use router
                user_msg = next((m["content"] for m in reversed(messages)
                                 if m.get("role") == "user"), "")
                decision = route(user_msg, tools_spec)
                logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
                if decision["call"] == "yes" and decision["tool"]:
                    force_tool_choice = {
                        "type": "function",
                        "function": {"name": decision["tool"]}
                    }
                else:
                    force_tool_choice = "auto"

        resp = None
        last_tool_stream_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES):
            try:
                stream_req_kwargs = dict(self._request_kwargs)
                stream_req_kwargs["stream_options"] = {"include_usage": True}
                resp = self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                    tools=tools_spec,
                    tool_choice=force_tool_choice,
                    stream=True,
                    **stream_req_kwargs,
                )
                break  # Success, exit retry loop
            except Exception as e:
                last_tool_stream_error = e
                if _should_retry(e) and attempt < MAX_RETRIES - 1:
                    backoff = INITIAL_BACKOFF * (2 ** attempt)
                    logging.warning(
                        "[openai] Retryable tool streaming error (attempt %d/%d): %s. Retrying in %.1fs...",
                        attempt + 1, MAX_RETRIES, type(e).__name__, backoff
                    )
                    time.sleep(backoff)
                    continue
                logging.exception("OpenAI call failed")
                raise _convert_to_llm_error(e, "JSON mode call")

        if resp is None:
            if last_tool_stream_error:
                raise _convert_to_llm_error(last_tool_stream_error, f"JSON mode call after {MAX_RETRIES} retries")
            raise LLMEmptyResponseError(f"OpenAI JSON mode call failed after {MAX_RETRIES} retries with no response")

        call_buffer: dict[str, dict] = {}
        state = "TEXT"
        prefix_yielded = False
        reasoning_details_raw: List[Dict[str, Any]] = []

        try:
            current_call_id = None
            last_chunk = None

            for chunk in resp:
                last_chunk = chunk
                delta = chunk.choices[0].delta

                if delta.tool_calls:
                    state = "TOOL_CALL"
                    for call in delta.tool_calls:
                        tc_id = call.id or current_call_id
                        if tc_id is None:
                            logging.warning("tool_chunk without id; skipping")
                            continue
                        current_call_id = tc_id

                        buf = call_buffer.setdefault(tc_id, {
                            "id": tc_id,
                            "name": "",
                            "arguments": "",
                        })

                        logging.debug("tool_chunk id=%s name=%s args_part=%s",
                                      tc_id, call.function.name or "-", call.function.arguments or "-")

                        if call.function.name:
                            buf["name"] = call.function.name
                        if call.function.arguments:
                            buf["arguments"] += call.function.arguments
                    continue

                if state == "TEXT" and delta.content:
                    text_fragment, reasoning_piece = _process_openai_stream_content(delta.content)
                    for r in reasoning_piece:
                        reasoning_chunks.append(r)
                        yield {"type": "thinking", "content": r}
                    extra_reasoning = _extract_reasoning_from_delta(delta)
                    for r in extra_reasoning:
                        reasoning_chunks.append(r)
                        yield {"type": "thinking", "content": r}
                    if not text_fragment:
                        continue
                    if not prefix_yielded and history_snippets:
                        yield "\n".join(history_snippets) + "\n"
                        prefix_yielded = True
                    yield text_fragment
                    continue

                additional_reasoning = _extract_reasoning_from_delta(delta)
                for r in additional_reasoning:
                    reasoning_chunks.append(r)
                    yield {"type": "thinking", "content": r}

                # Collect raw reasoning_details objects for multi-turn passback
                reasoning_details_raw.extend(_extract_raw_reasoning_details_from_delta(delta))

            # Store usage from last chunk (when stream_options.include_usage=True)
            if last_chunk and hasattr(last_chunk, "usage") and last_chunk.usage:
                # Check for cached tokens (OpenAI Prompt Caching)
                cached = 0
                if hasattr(last_chunk.usage, "prompt_tokens_details") and last_chunk.usage.prompt_tokens_details:
                    cached = getattr(last_chunk.usage.prompt_tokens_details, "cached_tokens", 0) or 0
                self._store_usage(
                    input_tokens=last_chunk.usage.prompt_tokens or 0,
                    output_tokens=last_chunk.usage.completion_tokens or 0,
                    cached_tokens=cached,
                )

            # Store reasoning
            self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
            # Store reasoning_details for multi-turn pass-back (structured objects only)
            if reasoning_details_raw:
                self._store_reasoning_details(_merge_streaming_reasoning_details(reasoning_details_raw))

            # Store tool detection result if tool calls were detected
            if call_buffer:
                logging.debug("call_buffer final: %s", json.dumps(call_buffer, indent=2, ensure_ascii=False))
                # Get the first tool call (for now, only support single tool call)
                first_tc = next(iter(call_buffer.values()), None)
                if first_tc:
                    name = first_tc["name"].strip()
                    arg_str = first_tc["arguments"].strip()

                    if name and arg_str:
                        try:
                            args = json.loads(arg_str)
                        except json.JSONDecodeError:
                            logging.warning("tool_call arguments invalid JSON: %s", arg_str)
                            args = {}

                        # Collect all text that was yielded (approximation)
                        # Note: We don't have a perfect way to collect yielded text here,
                        # but in most cases tool_call doesn't come with text
                        self._store_tool_detection({
                            "type": "tool_call",
                            "tool_name": name,
                            "tool_args": args,
                            "raw_tool_call": first_tc,
                        })
                        logging.info("[openai] Tool detection stored: %s", name)
                    else:
                        logging.warning("tool_call has empty name or arguments; not storing")
                        self._store_tool_detection({"type": "text", "content": ""})
            else:
                # No tool call - store text-only result
                self._store_tool_detection({"type": "text", "content": ""})

        except LLMError:
            # Re-raise LLMError subclasses directly
            raise
        except Exception as e:
            logging.exception("OpenAI stream call failed")
            raise _convert_to_llm_error(e, "streaming call")

    def configure_parameters(self, parameters: Dict[str, Any] | None) -> None:
        if not isinstance(parameters, dict):
            return
        for key, value in parameters.items():
            if key not in OPENAI_ALLOWED_REQUEST_PARAMS:
                continue
            if value is None:
                self._request_kwargs.pop(key, None)
            else:
                self._request_kwargs[key] = value

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

__all__ = ["OpenAIClient", "OpenAI"]
OPENAI_ALLOWED_REQUEST_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "n",
    "user",
    "response_format",
    "logprobs",
    "top_logprobs",
    "reasoning_effort",
    "seed",
    "parallel_tool_calls",
}
