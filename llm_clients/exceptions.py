"""Custom exception classes for LLM clients.

These exceptions provide structured error information that can be
displayed to users in the chat UI.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base class for all LLM-related errors."""

    error_code: str = "llm_error"
    user_message: str = "LLMでエラーが発生しました"
    #: この失敗で止まった返事の、「続きの生成」を出すペルソナの発言の id。
    #: 埋めるのは返事の実行の一番外側の後始末 (sea/reply_stop_exit.py) だけで、
    #: 画面はこの id の発言に「続きの生成」を出し、エラー札の案内もそちらを
    #: 指す。一番外側が埋めてそのまま画面へ出す単方向の属性 — 内側の経路が
    #: これを読んで何かを引き取ることはしない。
    interrupted_message_id: str | None = None
    #: 上の発言が、返事の部屋 (ユーザーが発言を送った部屋) と違う部屋にある回の、
    #: その部屋の id と表示名。画面は部屋の名前つきで「その部屋へ行って押す」を
    #: 案内する。
    interrupted_building_id: str | None = None
    interrupted_building_name: str | None = None
    #: 失敗した呼び出しのモデル名 (分かるときだけ)。LLM ノード
    #: (sea/runtime_llm.py の lg_llm_node) が、その Beat で選んだモデルを後から
    #: 書き足す。発言が生まれなかった回の知らせの詳細 (画面だけに出す) に載る
    #: (docs/intent/no_utterance_error_notice.md)。
    model: str | None = None

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
        event = {
            "type": "error",
            "error_code": self.error_code,
            "content": self.user_message,
            "technical_detail": str(self),
        }
        # 案内の材料は値が立っているときだけ載せる (docs/intent/reply_stop_exit.md)。
        if self.interrupted_message_id:
            event["interrupted_message_id"] = str(self.interrupted_message_id)
        if self.interrupted_building_id:
            event["interrupted_building_id"] = str(self.interrupted_building_id)
        if self.interrupted_building_name:
            event["interrupted_building_name"] = str(self.interrupted_building_name)
        return event


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
    user_message = "コンテンツが安全性フィルターによりブロックされました。"


class EmptyResponseError(LLMError):
    """Raised when LLM returns an empty response."""

    error_code = "empty_response"
    user_message = "LLMから空の応答が返されました。再度お試しください。"


class AuthenticationError(LLMError):
    """Raised when API key is invalid or expired."""

    error_code = "authentication"
    user_message = "APIキーが無効または期限切れです。管理者にお問い合わせください。"


class PaymentError(LLMError):
    """Raised when payment is required (402) or billing limit exceeded."""

    error_code = "payment"
    user_message = "APIの利用料金が上限に達しました。APIキーの残高や支払い設定を確認してください。"


class ModelNotFoundError(LLMError):
    """Raised when the specified model is not found or unavailable."""

    error_code = "model_not_found"
    user_message = "指定されたモデルが見つからないか、利用できません。"


class InvalidRequestError(LLMError):
    """Raised when the request is invalid (bad parameters, etc.)."""

    error_code = "invalid_request"
    user_message = "リクエストが不正です。入力内容を確認してください。"


class ContextLengthExceededError(LLMError):
    """Raised when the request is longer than the model's context window.

    Separated from :class:`InvalidRequestError` because it is a common failure
    with one clear remedy for the user: switch to a model with a larger context
    (docs/intent/no_utterance_error_notice.md, fix 5).
    """

    error_code = "context_length_exceeded"
    user_message = (
        "会話の長さが、使っているモデルの上限を超えました。"
        "より長い会話を扱えるモデル (コンテキストの上限が大きいモデル) に切り替えてください。"
    )


#: 会話の長さの上限超過を表す、各社のエラー文の手がかり (小文字で照合する)。
#: - OpenAI / OpenAI 互換 (OpenRouter など): "maximum context length" /
#:   エラーコード "context_length_exceeded"
#: - llama.cpp server (OpenAI 互換の口): "exceeds the available context size"
#: - Anthropic: "prompt is too long" / "exceed context limit"
#: - Gemini: "exceeds the maximum number of tokens allowed"
#: - xAI: "maximum prompt length"
_CONTEXT_LENGTH_MARKERS = (
    "context_length_exceeded",
    "maximum context length",
    "exceeds the available context size",
    "prompt is too long",
    "exceed context limit",
    "exceeds the maximum number of tokens allowed",
    "maximum prompt length",
)


def is_context_length_error(err: BaseException) -> bool:
    """プロバイダーの例外が、会話の長さの上限超過を表しているか。

    各クライアントのエラー分類 (``*_convert_to_llm_error``) が、他の分類より先に
    呼ぶ。手がかりはエラー文と、OpenAI 形式の本文の ``error.code``。
    """
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        error_obj = body.get("error")
        if isinstance(error_obj, dict) and error_obj.get("code") == "context_length_exceeded":
            return True
    msg = str(err).lower()
    return any(marker in msg for marker in _CONTEXT_LENGTH_MARKERS)


class ModelUnavailableError(LLMError):
    """The model a persona is set to use has no definition, or cannot be connected to.

    SAIVerse does not substitute another model in that case
    (docs/intent/persona_model_selection.md, decision 7). The ``user_message``
    names who could not speak, which model is missing or unreachable, where to
    reselect it, and that no restart is needed. It is shown in the chat screen,
    and the failed reply also leaves a host row in the building log whose body
    says only that the persona could not speak; this ``user_message`` goes into
    that row's server-only metadata, never into any persona's memory
    (docs/intent/no_utterance_error_notice.md).

    Attributes:
        role: ``"default_model"`` or ``"lightweight_model"``.
        reason: ``"missing"`` (no definition) or ``"unreachable"`` (the
            definition exists but a client could not be created).
        model: the model name that could not be used.
        persona_id: the persona that could not use it.
    """

    error_code = "model_unavailable"
    user_message = "ペルソナが使うモデルを使えません。"

    def __init__(
        self,
        message: str,
        *,
        role: str,
        reason: str,
        model: str | None = None,
        persona_id: str | None = None,
        original_error: Exception | None = None,
        user_message: str | None = None,
    ):
        super().__init__(message, original_error=original_error, user_message=user_message)
        self.role = role
        self.reason = reason
        self.model = model
        self.persona_id = persona_id
