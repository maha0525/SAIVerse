"""Custom exception classes for LLM clients.

These exceptions provide structured error information that can be
displayed to users in the chat UI.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all LLM-related errors."""

    error_code: str = "llm_error"
    user_message: str = "LLMでエラーが発生しました"

    def __init__(
        self,
        message: str,
        original_error: Exception | None = None,
        user_message: str | None = None,
    ):
        super().__init__(message)
        self.original_error = original_error
        if user_message is not None:
            self.user_message = user_message

    def to_dict(self) -> dict:
        """Convert to a dictionary for NDJSON serialization."""
        return {
            "type": "error",
            "error_code": self.error_code,
            "content": self.user_message,
            "technical_detail": str(self),
        }


class RateLimitError(LLMError):
    """Raised when API rate limit is exceeded."""

    error_code = "rate_limit"
    user_message = "APIの利用制限に達しました。しばらく待ってから再度お試しください。"


class LLMTimeoutError(LLMError):
    """Raised when API request times out.

    Note: Named LLMTimeoutError to avoid conflict with built-in TimeoutError.
    """

    error_code = "timeout"
    user_message = "応答がタイムアウトしました。サーバーが混雑しているか、リクエストが複雑すぎる可能性があります。"


class ServerError(LLMError):
    """Raised when LLM server returns 5xx error."""

    error_code = "server_error"
    user_message = "LLMサーバーでエラーが発生しました。しばらく待ってから再度お試しください。"


class SafetyFilterError(LLMError):
    """Raised when content is blocked by safety filters."""

    error_code = "safety_filter"
    user_message = "コンテンツが安全性フィルターによりブロックされました。入力内容を変更してお試しください。"


class EmptyResponseError(LLMError):
    """Raised when LLM returns an empty response."""

    error_code = "empty_response"
    user_message = "LLMから空の応答が返されました。再度お試しください。"


class AuthenticationError(LLMError):
    """Raised when API key is invalid or expired."""

    error_code = "authentication"
    user_message = "APIキーが無効または期限切れです。管理者にお問い合わせください。"


class ModelNotFoundError(LLMError):
    """Raised when the specified model is not found or unavailable."""

    error_code = "model_not_found"
    user_message = "指定されたモデルが見つからないか、利用できません。"


class InvalidRequestError(LLMError):
    """Raised when the request is invalid (bad parameters, etc.)."""

    error_code = "invalid_request"
    user_message = "リクエストが不正です。入力内容を確認してください。"
