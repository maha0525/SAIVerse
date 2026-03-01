"""OpenAI (and compatible) chat completion client."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterator, List, Optional

import openai
from openai import OpenAI

from tools import OPENAI_TOOLS_SPEC
from saiverse.llm_router import route

from . import openai_errors
from . import openai_runtime
from .base import LLMClient, get_llm_logger
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
from .openai_message_preparer import (
    prepare_openai_messages,
)
from .openai_reasoning import (
    extract_raw_reasoning_details_from_delta,
    extract_reasoning_from_delta,
    extract_reasoning_from_message,
    merge_streaming_reasoning_details,
    process_openai_stream_content,
)
from .utils import merge_reasoning_strings


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
    """Backward-compatible wrapper for OpenAI message preparation."""
    return prepare_openai_messages(
        messages=messages,
        supports_images=supports_images,
        max_image_bytes=max_image_bytes,
        convert_system_to_user=convert_system_to_user,
        reasoning_passback_field=reasoning_passback_field,
    )


def _extract_json_object_candidate(text: str) -> str:
    """Extract first JSON object candidate from a model response."""
    candidate = (text or "").strip()
    if not candidate:
        return ""

    if candidate.startswith("```"):
        for segment in candidate.split("```"):
            segment = segment.strip()
            if segment.startswith("json"):
                segment = segment[4:].strip()
            if segment.startswith("{") and segment.endswith("}"):
                return segment

    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        return candidate[start : end + 1]
    return candidate


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

    def _build_request_kwargs(
        self,
        *,
        response_schema: Optional[Dict[str, Any]],
        temperature: float | None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        req = openai_runtime.build_request_kwargs(
            self._request_kwargs,
            temperature=temperature,
            response_schema=response_schema,
            structured_output_mode=self.structured_output_mode,
            structured_output_backend=self.structured_output_backend,
            add_additional_properties=self._add_additional_properties,
            stream=stream,
            include_stream_usage=True,
        )
        if response_schema and self.structured_output_mode == "json_object":
            logging.debug("[openai] Using json_object mode with prompt-based schema")
        if response_schema and self.structured_output_mode != "json_object":
            if self.structured_output_backend:
                logging.info("Applying structured_output_backend='%s' to request (both locations)", self.structured_output_backend)
            logging.debug("response_format: %s", req.get("response_format"))
        return req

    def _generate_text_or_structured(
        self,
        messages: List[Dict[str, Any]],
        history_snippets: Optional[List[str]],
        response_schema: Optional[Dict[str, Any]],
        *,
        temperature: float | None,
    ) -> str | Dict[str, Any]:
        snippets: List[str] = list(history_snippets or [])
        effective_messages = messages
        if response_schema and self.structured_output_mode == "json_object":
            effective_messages = self._inject_schema_prompt(messages, response_schema)

        context = "API call"
        try:
            resp = openai_runtime.call_with_retry(
                lambda: self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(
                        effective_messages,
                        self.supports_images,
                        self.max_image_bytes,
                        self.convert_system_to_user,
                        self.reasoning_passback_field,
                    ),
                    n=1,
                    **self._build_request_kwargs(response_schema=response_schema, temperature=temperature),
                ),
                context=context,
                max_retries=MAX_RETRIES,
                initial_backoff=INITIAL_BACKOFF,
                should_retry=_should_retry,
            )
        except Exception as e:
            logging.exception("OpenAI call failed")
            raise _convert_to_llm_error(e, context)

        get_llm_logger().debug("OpenAI raw:\n%s", resp.model_dump_json(indent=2))
        openai_runtime.store_usage_from_response(
            resp,
            lambda i, o, c: self._store_usage(input_tokens=i, output_tokens=o, cached_tokens=c),
        )

        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            logging.warning("[openai] Output blocked by content filter. finish_reason=%s", choice.finish_reason)
            openai_runtime.raise_content_filter_error(context="output")

        text_body, reasoning_entries, reasoning_details = extract_reasoning_from_message(choice.message)
        if not text_body:
            text_body = choice.message.content or ""
        self._store_reasoning(reasoning_entries)
        self._store_reasoning_details(reasoning_details)

        if response_schema:
            candidate = _extract_json_object_candidate(text_body)

            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as e:
                preview = candidate.replace("\n", "\\n")[:300]
                logging.warning("[openai] Failed to parse structured output: %s (candidate=%r)", e, preview)
                raise InvalidRequestError(
                    "Failed to parse JSON response from structured output",
                    e,
                    user_message="LLMからの応答を解析できませんでした。再度お試しください。",
                ) from e

        if not text_body.strip():
            logging.error(
                "[openai] Empty text response. Model returned empty content. finish_reason=%s",
                choice.finish_reason,
            )
            raise LLMEmptyResponseError("OpenAI returned empty response")
        if snippets:
            prefix = "\n".join(snippets)
            return prefix + ("\n" if text_body and prefix else "") + text_body
        return text_body

    def _generate_tool_detection(
        self,
        messages: List[Dict[str, Any]],
        tools_spec: list,
        *,
        temperature: float | None,
    ) -> Dict[str, Any]:
        context = "API call (tool mode)"
        try:
            resp = openai_runtime.call_with_retry(
                lambda: self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(
                        messages,
                        self.supports_images,
                        self.max_image_bytes,
                        self.convert_system_to_user,
                        self.reasoning_passback_field,
                    ),
                    tools=tools_spec,
                    tool_choice="auto",
                    n=1,
                    **self._build_request_kwargs(response_schema=None, temperature=temperature),
                ),
                context=context,
                max_retries=MAX_RETRIES,
                initial_backoff=INITIAL_BACKOFF,
                should_retry=_should_retry,
            )
        except Exception as e:
            logging.exception("OpenAI call failed")
            raise _convert_to_llm_error(e, context)

        get_llm_logger().debug("OpenAI raw (tool detection):\n%s", resp.model_dump_json(indent=2))
        openai_runtime.store_usage_from_response(
            resp,
            lambda i, o, c: self._store_usage(input_tokens=i, output_tokens=o, cached_tokens=c),
        )

        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            logging.warning("[openai] Output blocked by content filter (tool mode). finish_reason=%s", choice.finish_reason)
            openai_runtime.raise_content_filter_error(context="output")

        tool_calls = getattr(choice.message, "tool_calls", [])
        text_body, reasoning_entries, reasoning_details = extract_reasoning_from_message(choice.message)
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

        content = text_body or choice.message.content or ""
        if not content.strip():
            logging.error(
                "[openai] Empty text response without tool call. Model returned empty content. finish_reason=%s",
                choice.finish_reason,
            )
            raise LLMEmptyResponseError("OpenAI returned empty response without tool call")
        return {"type": "text", "content": content}

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
        self._store_reasoning([])

        if response_schema and use_tools:
            logging.warning("response_schema specified alongside tools; structured output is ignored for tool runs.")
            response_schema = None

        if not use_tools:
            return self._generate_text_or_structured(
                messages,
                history_snippets,
                response_schema,
                temperature=temperature,
            )

        logging.info("[openai] Sending %d tools to API", len(tools_spec))
        for i, tool in enumerate(tools_spec):
            logging.info("[openai] Tool[%d]: %s", i, tool)
        return self._generate_tool_detection(messages, tools_spec, temperature=temperature)

    def _resolve_tool_choice(
        self,
        *,
        force_tool_choice: Optional[dict | str],
        tools: Optional[list],
        tools_spec: list,
        messages: List[Dict[str, Any]],
    ) -> dict | str:
        if force_tool_choice is not None:
            return force_tool_choice
        if tools is not None:
            return "auto"
        user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        decision = route(user_msg, tools_spec)
        logging.info("Router decision:\n%s", json.dumps(decision, indent=2, ensure_ascii=False))
        if decision["call"] == "yes" and decision["tool"]:
            return {"type": "function", "function": {"name": decision["tool"]}}
        return "auto"

    def _store_stream_tool_detection_from_buffer(self, call_buffer: Dict[str, Dict[str, str]]) -> None:
        if not call_buffer:
            self._store_tool_detection({"type": "text", "content": ""})
            return

        logging.debug("call_buffer final: %s", json.dumps(call_buffer, indent=2, ensure_ascii=False))
        first_tc = next(iter(call_buffer.values()), None)
        if not first_tc:
            self._store_tool_detection({"type": "text", "content": ""})
            return

        name = first_tc["name"].strip()
        arg_str = first_tc["arguments"].strip()
        if not name or not arg_str:
            logging.warning("tool_call has empty name or arguments; not storing")
            self._store_tool_detection({"type": "text", "content": ""})
            return

        try:
            args = json.loads(arg_str)
        except json.JSONDecodeError:
            logging.warning(
                "tool_call arguments invalid JSON; fallback to empty object. raw=%s",
                arg_str,
            )
            args = {}

        self._store_tool_detection({
            "type": "tool_call",
            "tool_name": name,
            "tool_args": args,
            "raw_tool_call": first_tc,
        })
        logging.info("[openai] Tool detection stored: %s", name)

    def _stream_text_mode(
        self,
        *,
        resp: Any,
        history_snippets: List[str],
        req_kwargs: Dict[str, Any],
        response_schema: Optional[Dict[str, Any]],
        reasoning_chunks: List[str],
    ) -> Iterator[str]:
        if req_kwargs.get("stream"):
            last_chunk = None
            last_finish_reason = None
            reasoning_details_raw: List[Dict[str, Any]] = []
            prefix_yielded = False

            for chunk in resp:
                last_chunk = chunk
                if chunk.choices and chunk.choices[0]:
                    fr = chunk.choices[0].finish_reason
                    if fr is not None:
                        last_finish_reason = fr
                if not chunk.choices or not chunk.choices[0].delta:
                    continue

                delta = chunk.choices[0].delta
                extra_reasoning = extract_reasoning_from_delta(delta)
                for r in extra_reasoning:
                    reasoning_chunks.append(r)
                    yield {"type": "thinking", "content": r}

                reasoning_details_raw.extend(extract_raw_reasoning_details_from_delta(delta))

                content = delta.content
                if not content:
                    continue
                text_fragment, reasoning_piece = process_openai_stream_content(content)
                for r in reasoning_piece:
                    reasoning_chunks.append(r)
                    yield {"type": "thinking", "content": r}
                if not text_fragment:
                    continue
                if not prefix_yielded and history_snippets:
                    yield "\n".join(history_snippets) + "\n"
                    prefix_yielded = True
                yield text_fragment

            if last_finish_reason == "content_filter":
                logging.warning("[openai] Stream output blocked by content filter. finish_reason=%s", last_finish_reason)
                openai_runtime.raise_content_filter_error(context="output")

            openai_runtime.store_usage_from_last_chunk(
                last_chunk,
                lambda i, o, c: self._store_usage(input_tokens=i, output_tokens=o, cached_tokens=c),
            )
            self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
            if reasoning_details_raw:
                self._store_reasoning_details(merge_streaming_reasoning_details(reasoning_details_raw))
            return

        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            logging.warning("[openai] Output blocked by content filter. finish_reason=%s", choice.finish_reason)
            openai_runtime.raise_content_filter_error(context="output")

        text_body, reasoning_entries, reasoning_details = extract_reasoning_from_message(choice.message)
        if not text_body:
            text_body = choice.message.content or ""
        self._store_reasoning(reasoning_entries)
        self._store_reasoning_details(reasoning_details)
        prefix = "\n".join(history_snippets) + ("\n" if history_snippets and text_body else "")
        if prefix:
            yield prefix
        if text_body:
            yield text_body

    def _stream_tool_mode(
        self,
        *,
        resp: Any,
        history_snippets: List[str],
        reasoning_chunks: List[str],
    ) -> Iterator[str]:
        call_buffer: Dict[str, Dict[str, str]] = {}
        state = "TEXT"
        prefix_yielded = False
        reasoning_details_raw: List[Dict[str, Any]] = []
        current_call_id: Optional[str] = None
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
                    buf = call_buffer.setdefault(tc_id, {"id": tc_id, "name": "", "arguments": ""})
                    logging.debug(
                        "tool_chunk id=%s name=%s args_part=%s",
                        tc_id,
                        call.function.name or "-",
                        call.function.arguments or "-",
                    )
                    if call.function.name:
                        buf["name"] = call.function.name
                    if call.function.arguments:
                        buf["arguments"] += call.function.arguments
                continue

            if state == "TEXT" and delta.content:
                text_fragment, reasoning_piece = process_openai_stream_content(delta.content)
                for r in reasoning_piece:
                    reasoning_chunks.append(r)
                    yield {"type": "thinking", "content": r}
                extra_reasoning = extract_reasoning_from_delta(delta)
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

            additional_reasoning = extract_reasoning_from_delta(delta)
            for r in additional_reasoning:
                reasoning_chunks.append(r)
                yield {"type": "thinking", "content": r}
            reasoning_details_raw.extend(extract_raw_reasoning_details_from_delta(delta))

        openai_runtime.store_usage_from_last_chunk(
            last_chunk,
            lambda i, o, c: self._store_usage(input_tokens=i, output_tokens=o, cached_tokens=c),
        )
        self._store_reasoning(merge_reasoning_strings(reasoning_chunks))
        if reasoning_details_raw:
            self._store_reasoning_details(merge_streaming_reasoning_details(reasoning_details_raw))
        self._store_stream_tool_detection_from_buffer(call_buffer)

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

        effective_messages = messages
        if response_schema and self.structured_output_mode == "json_object":
            effective_messages = self._inject_schema_prompt(messages, response_schema)

        if not use_tools:
            req_kwargs = openai_runtime.build_request_kwargs(
                self._request_kwargs,
                temperature=temperature,
                response_schema=response_schema,
                structured_output_mode=self.structured_output_mode,
                structured_output_backend=self.structured_output_backend,
                add_additional_properties=self._add_additional_properties,
                stream=not bool(response_schema),
                include_stream_usage=True,
            )
            if response_schema and self.structured_output_mode == "json_object":
                logging.debug("[openai] Using json_object mode with prompt-based schema (stream)")
            if response_schema and self.structured_output_mode != "json_object" and self.structured_output_backend:
                logging.info("Applying structured_output_backend='%s' to request (stream)", self.structured_output_backend)
            try:
                resp = openai_runtime.call_with_retry(
                    lambda: self._create_completion(
                        model=self.model,
                        messages=_prepare_openai_messages(effective_messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                        n=1,
                        **req_kwargs,
                    ),
                    context="streaming",
                    max_retries=MAX_RETRIES,
                    initial_backoff=INITIAL_BACKOFF,
                    should_retry=_should_retry,
                )
            except Exception as e:
                logging.exception("OpenAI call failed")
                raise _convert_to_llm_error(e, "streaming")

            yield from self._stream_text_mode(
                resp=resp,
                history_snippets=history_snippets,
                req_kwargs=req_kwargs,
                response_schema=response_schema,
                reasoning_chunks=reasoning_chunks,
            )
            return

        force_tool_choice = self._resolve_tool_choice(
            force_tool_choice=force_tool_choice,
            tools=tools,
            tools_spec=tools_spec,
            messages=messages,
        )

        stream_req_kwargs = dict(self._request_kwargs)
        stream_req_kwargs["stream_options"] = {"include_usage": True}
        try:
            resp = openai_runtime.call_with_retry(
                lambda: self._create_completion(
                    model=self.model,
                    messages=_prepare_openai_messages(messages, self.supports_images, self.max_image_bytes, self.convert_system_to_user, self.reasoning_passback_field),
                    tools=tools_spec,
                    tool_choice=force_tool_choice,
                    stream=True,
                    **stream_req_kwargs,
                ),
                context="JSON mode call",
                max_retries=MAX_RETRIES,
                initial_backoff=INITIAL_BACKOFF,
                should_retry=_should_retry,
            )
        except Exception as e:
            logging.exception("OpenAI call failed")
            raise _convert_to_llm_error(e, "JSON mode call")

        try:
            yield from self._stream_tool_mode(
                resp=resp,
                history_snippets=history_snippets,
                reasoning_chunks=reasoning_chunks,
            )
        except LLMError:
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
