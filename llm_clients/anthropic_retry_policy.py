"""Anthropic error classification and retry policy helpers."""
from __future__ import annotations

import anthropic

from .exceptions import (
    AuthenticationError,
    LLMError,
    LLMTimeoutError,
    PaymentError,
    RateLimitError,
    ServerError,
)


def _is_rate_limit_error(err: Exception) -> bool:
    if isinstance(err, anthropic.RateLimitError):
        return True
    msg = str(err).lower()
    return "rate" in msg or "429" in msg or "quota" in msg or "overload" in msg


def _is_server_error(err: Exception) -> bool:
    if isinstance(err, anthropic.APIStatusError):
        return err.status_code >= 500
    msg = str(err).lower()
    return "503" in msg or "502" in msg or "504" in msg or "unavailable" in msg


def _is_timeout_error(err: Exception) -> bool:
    if isinstance(err, anthropic.APITimeoutError):
        return True
    if isinstance(err, anthropic.APIConnectionError):
        return True
    msg = str(err).lower()
    return "timeout" in msg or "timed out" in msg


def _is_authentication_error(err: Exception) -> bool:
    if isinstance(err, anthropic.AuthenticationError):
        return True
    msg = str(err).lower()
    return "401" in msg or "403" in msg or "authentication" in msg or "invalid api key" in msg


def _is_payment_error(err: Exception) -> bool:
    msg = str(err).lower()
    return (
        "402" in msg
        or "payment required" in msg
        or "spend limit" in msg
        or "billing" in msg
        or "insufficient_quota" in msg
    )


def _is_content_policy_error(err: Exception) -> bool:
    msg = str(err).lower()
    return any(
        kw in msg
        for kw in ("content policy", "harmful", "unsafe content", "violates", "not allowed", "blocked")
    )


def _should_retry(err: Exception) -> bool:
    if _is_payment_error(err) or _is_authentication_error(err):
        return False
    return _is_rate_limit_error(err) or _is_server_error(err) or _is_timeout_error(err)


def _convert_to_llm_error(err: Exception, context: str = "API call") -> LLMError:
    if _is_payment_error(err):
        return PaymentError(f"Anthropic {context} failed: payment required", err)
    if _is_rate_limit_error(err):
        return RateLimitError(f"Anthropic {context} failed: rate limit exceeded", err)
    if _is_timeout_error(err):
        return LLMTimeoutError(f"Anthropic {context} failed: timeout", err)
    if _is_server_error(err):
        return ServerError(f"Anthropic {context} failed: server error", err)
    if _is_authentication_error(err):
        return AuthenticationError(f"Anthropic {context} failed: authentication error", err)
    return LLMError(f"Anthropic {context} failed: {err}", err)
