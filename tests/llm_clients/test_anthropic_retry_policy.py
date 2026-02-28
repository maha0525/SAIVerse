from __future__ import annotations

import httpx

import anthropic

from llm_clients.anthropic_retry_policy import (
    _convert_to_llm_error,
    _is_authentication_error,
    _is_content_policy_error,
    _is_payment_error,
    _is_rate_limit_error,
    _is_server_error,
    _is_timeout_error,
    _should_retry,
)
from llm_clients.exceptions import (
    AuthenticationError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    ServerError,
)


def test_should_retry_rate_limit_and_not_authentication() -> None:
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    response = httpx.Response(429, request=request)
    err = anthropic.RateLimitError("rate limit", response=response, body=None)

    assert _is_rate_limit_error(err)
    assert _should_retry(err)

    auth_err = anthropic.AuthenticationError("invalid key", response=httpx.Response(401, request=request), body=None)
    assert _is_authentication_error(auth_err)
    assert not _should_retry(auth_err)


def test_server_timeout_payment_content_policy_detection() -> None:
    request = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    server_err = anthropic.APIStatusError("server unavailable", response=httpx.Response(503, request=request), body=None)

    assert _is_server_error(server_err)
    assert _is_timeout_error(anthropic.APITimeoutError(request))
    assert _is_payment_error(Exception("402 payment required"))
    assert _is_content_policy_error(Exception("content policy violation"))


def test_convert_to_llm_error_maps_to_specific_types() -> None:
    assert isinstance(_convert_to_llm_error(Exception("402 billing")), PaymentError)
    assert isinstance(_convert_to_llm_error(Exception("429 rate")), RateLimitError)
    assert isinstance(_convert_to_llm_error(Exception("timed out")), LLMTimeoutError)
    assert isinstance(_convert_to_llm_error(Exception("503 unavailable")), ServerError)
    assert isinstance(_convert_to_llm_error(Exception("401 invalid api key")), AuthenticationError)
