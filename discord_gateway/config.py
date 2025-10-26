from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """設定ファイル (.env) からGatewayクライアントの動作パラメータを読み込む。"""

    bot_ws_url: str = Field(..., alias="SAIVERSE_GATEWAY_WS_URL")
    handshake_token: SecretStr = Field(..., alias="SAIVERSE_GATEWAY_TOKEN")

    reconnect_initial_delay: float = Field(1.0, alias="SAIVERSE_GATEWAY_RECONNECT_INITIAL")
    reconnect_max_delay: float = Field(30.0, alias="SAIVERSE_GATEWAY_RECONNECT_MAX")
    reconnect_jitter: float = Field(0.3, alias="SAIVERSE_GATEWAY_RECONNECT_JITTER")

    handshake_timeout: float = Field(10.0, alias="SAIVERSE_GATEWAY_HANDSHAKE_TIMEOUT")
    recv_timeout: float = Field(60.0, alias="SAIVERSE_GATEWAY_RECV_TIMEOUT")

    incoming_queue_maxsize: int = Field(0, alias="SAIVERSE_GATEWAY_INCOMING_MAXSIZE")
    outgoing_queue_maxsize: int = Field(0, alias="SAIVERSE_GATEWAY_OUTGOING_MAXSIZE")

    max_payload_bytes: int = Field(512 * 1024, alias="SAIVERSE_GATEWAY_MAX_PAYLOAD")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        "INFO", alias="SAIVERSE_GATEWAY_LOG_LEVEL"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @validator("bot_ws_url")
    def _validate_ws_url(cls, value: str) -> str:
        if not value.startswith(("ws://", "wss://")):
            raise ValueError("SAIVERSE_GATEWAY_WS_URL must start with ws:// or wss://")
        return value

    @validator("reconnect_max_delay")
    def _validate_backoff(cls, value: float, values: dict) -> float:
        initial = values.get("reconnect_initial_delay", 1.0)
        if value < initial:
            raise ValueError("reconnect_max_delay must be >= reconnect_initial_delay")
        return value

    @property
    def handshake_timeout_seconds(self) -> float:
        return self.handshake_timeout

    @property
    def recv_timeout_seconds(self) -> float:
        return self.recv_timeout


@lru_cache
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
