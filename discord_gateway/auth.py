from __future__ import annotations

from abc import ABC, abstractmethod

from .config import GatewaySettings


class TokenProviderError(RuntimeError):
    """トークン取得・保存に失敗した場合の例外。"""


class TokenProvider(ABC):
    """Gatewayハンドシェイクに利用するSAIVerse認証トークンの取得インターフェース。"""

    @abstractmethod
    def get_token(self) -> str:
        """ハンドシェイク用の認証トークンを取得する。"""


class StaticTokenProvider(TokenProvider):
    """設定ファイルに格納されたトークンをそのまま返す最小実装。"""

    def __init__(self, token: str | None = None, settings: GatewaySettings | None = None):
        if token is None and settings is None:
            raise TokenProviderError("token または settings のいずれかを指定してください。")
        self._token = token
        self._settings = settings

    def get_token(self) -> str:
        if self._token is not None:
            return self._token
        if self._settings is None:
            raise TokenProviderError("トークンを取得できません。設定情報が不足しています。")
        return self._settings.handshake_token.get_secret_value()
