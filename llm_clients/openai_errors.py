"""OpenAI client error classification and conversion helpers."""
from __future__ import annotations

from llm_clients.exceptions import (
    AuthenticationError,
    LLMError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    SafetyFilterError,
    ServerError,
)

import openai


def is_rate_limit_error(err: Exception) -> bool:
    if isinstance(err, openai.RateLimitError):
        return True
    msg = str(err).lower()
    return "rate" in msg or "429" in msg or "quota" in msg or "overload" in msg


def is_server_error(err: Exception) -> bool:
    if isinstance(err, openai.APIStatusError):
        return err.status_code >= 500
    msg = str(err).lower()
    return "503" in msg or "502" in msg or "504" in msg or "unavailable" in msg


def is_timeout_error(err: Exception) -> bool:
    if isinstance(err, openai.APITimeoutError):
        return True
    if isinstance(err, openai.APIConnectionError):
        return True
    msg = str(err).lower()
    return "timeout" in msg or "timed out" in msg


def is_authentication_error(err: Exception) -> bool:
    if isinstance(err, openai.AuthenticationError):
        return True
    msg = str(err).lower()
    return "401" in msg or "403" in msg or "authentication" in msg or "invalid api key" in msg


def is_payment_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "402" in msg
        or "payment required" in msg
        or "spend limit" in msg
        or "billing" in msg
        or "insufficient_quota" in msg
    )


def is_content_policy_error(err: Exception) -> bool:
    if isinstance(err, openai.BadRequestError):
        body = getattr(err, "body", None)
        if isinstance(body, dict):
            code = body.get("error", {}).get("code", "") or ""
            if "content_policy" in code or "content_filter" in code:
                return True
    msg = str(err).lower()
    return "content_policy" in msg or "content_filter" in msg and "safety" in msg


def should_retry(err: Exception) -> bool:
    if is_payment_error(err) or is_authentication_error(err):
        return False
    return is_rate_limit_error(err) or is_server_error(err) or is_timeout_error(err)


def convert_to_llm_error(err: Exception, context: str = "API call") -> LLMError:
    if is_payment_error(err):
        return PaymentError(f"OpenAI {context} failed: payment required", err)
    if is_rate_limit_error(err):
        return RateLimitError(f"OpenAI {context} failed: rate limit exceeded", err)
    if is_timeout_error(err):
        return LLMTimeoutError(f"OpenAI {context} failed: timeout", err)
    if is_server_error(err):
        return ServerError(f"OpenAI {context} failed: server error", err)
    if is_authentication_error(err):
        return AuthenticationError(f"OpenAI {context} failed: authentication error", err)
    if is_content_policy_error(err):
        return SafetyFilterError(
            f"OpenAI {context} failed: content policy violation",
            err,
            user_message="入力内容がOpenAIのコンテンツポリシーによりブロックされました。入力内容を変更してお試しください。",
        )
    return LLMError(f"OpenAI {context} failed: {err}", err)
