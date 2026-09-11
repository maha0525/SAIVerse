"""Shared runtime helpers for OpenAI client request execution."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from .exceptions import LLMError, SafetyFilterError


_CONTENT_FILTER_USER_MESSAGE = "生成された内容がOpenAIのコンテンツフィルターによりブロックされました。入力内容を変更してお試しください。"


def build_request_kwargs(
    base_request_kwargs: Dict[str, Any],
    *,
    temperature: float | None = None,
    response_schema: Optional[Dict[str, Any]] = None,
    structured_output_mode: str = "native",
    structured_output_backend: str | None = None,
    add_additional_properties: Callable[[Dict[str, Any]], Dict[str, Any]],
    stream: bool = False,
    include_stream_usage: bool = False,
) -> Dict[str, Any]:
    """Build OpenAI request kwargs while preserving existing behavior."""
    req = dict(base_request_kwargs)
    if temperature is not None:
        req["temperature"] = temperature

    if response_schema:
        if structured_output_mode == "json_object":
            req["response_format"] = {"type": "json_object"}
        else:
            schema_name = response_schema.get("title") if isinstance(response_schema, dict) else None
            openai_schema = add_additional_properties(response_schema)
            json_schema_config: Dict[str, Any] = {
                "name": schema_name or "saiverse_structured_output",
                "schema": openai_schema,
                "strict": True,
            }
            response_format_config: Dict[str, Any] = {
                "type": "json_schema",
                "json_schema": json_schema_config,
            }
            if structured_output_backend:
                json_schema_config["backend"] = structured_output_backend
                response_format_config["backend"] = structured_output_backend
            req["response_format"] = response_format_config
    elif stream:
        req["stream"] = True
        if include_stream_usage:
            req["stream_options"] = {"include_usage": True}
    return req


#: Keyword arguments of ``chat.completions.create`` that are not body
#: parameters. The SDK turns them into request options instead of body fields
#: (openai ``_base_client.make_request_options``): ``extra_headers`` become
#: headers, ``extra_query`` URL parameters, ``timeout`` the transport timeout,
#: and ``extra_body`` is merged over the finished JSON body with its own keys
#: winning (``_base_client.BaseClient._build_request`` via ``_merge_mappings``).
#: Every other key of a ``build_request_kwargs`` result is a body parameter.
SDK_REQUEST_OPTION_KEYS = frozenset({"extra_headers", "extra_query", "extra_body", "timeout"})


def split_sdk_request_options(req: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Separate ``build_request_kwargs`` output into body parameters and SDK options.

    For a path that sends the request without the SDK. Routing the options the
    way the SDK does is what keeps that path's request identical to the SDK
    path's for the same model config: left in the body they would reach the API
    as unknown parameters, and dropped they silently change what the model runs
    with (docs/intent/model_provider_management.md §10「経路の網羅」).
    """
    body_params = {key: value for key, value in req.items() if key not in SDK_REQUEST_OPTION_KEYS}
    options = {key: value for key, value in req.items() if key in SDK_REQUEST_OPTION_KEYS}
    return body_params, options


def call_with_retry(
    create_completion: Callable[[], Any],
    *,
    context: str,
    max_retries: int,
    initial_backoff: float,
    should_retry: Callable[[Exception], bool],
) -> Any:
    """Execute completion call with exponential-backoff retry."""
    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return create_completion()
        except Exception as err:  # noqa: BLE001
            last_error = err
            if should_retry(err) and attempt < max_retries - 1:
                backoff = initial_backoff * (2 ** attempt)
                logging.warning(
                    "[openai] Retryable error in %s (attempt %d/%d): %s. Retrying in %.1fs...",
                    context,
                    attempt + 1,
                    max_retries,
                    type(err).__name__,
                    backoff,
                )
                time.sleep(backoff)
                continue
            raise
    if last_error:
        raise last_error
    raise LLMError(f"OpenAI call failed in {context} with no response")


def store_usage_from_response(response: Any, store_usage: Callable[[int, int, int], None]) -> None:
    usage = getattr(response, "usage", None)
    if not usage:
        return
    cached = 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        cached = getattr(prompt_details, "cached_tokens", 0) or 0
    store_usage(usage.prompt_tokens or 0, usage.completion_tokens or 0, cached)


def store_usage_from_last_chunk(last_chunk: Any, store_usage: Callable[[int, int, int], None]) -> None:
    usage = getattr(last_chunk, "usage", None) if last_chunk else None
    if not usage:
        return
    cached = 0
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    if prompt_details:
        cached = getattr(prompt_details, "cached_tokens", 0) or 0
    store_usage(usage.prompt_tokens or 0, usage.completion_tokens or 0, cached)


def raise_content_filter_error(*, context: str) -> None:
    raise SafetyFilterError(
        f"OpenAI {context} blocked by content filter",
        user_message=_CONTENT_FILTER_USER_MESSAGE,
    )
