from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    """Runtime configuration for the SAIVerse Discord bot service."""

    discord_bot_token: str = Field(..., alias="DISCORD_BOT_TOKEN")
    discord_application_id: str | None = Field(None, alias="DISCORD_APPLICATION_ID")

    websocket_host: str = Field("0.0.0.0", alias="SAIVERSE_WS_HOST")
    websocket_port: int = Field(8788, alias="SAIVERSE_WS_PORT")
    websocket_path: str = Field("/ws", alias="SAIVERSE_WS_PATH")
    websocket_heartbeat_seconds: int = Field(30, alias="SAIVERSE_WS_HEARTBEAT_SECONDS")
    websocket_max_payload_kb: int = Field(256, alias="SAIVERSE_WS_MAX_PAYLOAD_KB")
    websocket_tls_enabled: bool = Field(False, alias="SAIVERSE_WS_TLS_ENABLED")
    websocket_tls_certfile: str | None = Field(None, alias="SAIVERSE_WS_TLS_CERTFILE")
    websocket_tls_keyfile: str | None = Field(None, alias="SAIVERSE_WS_TLS_KEYFILE")
    websocket_tls_ca_file: str | None = Field(None, alias="SAIVERSE_WS_TLS_CA_FILE")
    websocket_tls_client_auth: Literal["none", "optional", "required"] = Field(
        "none", alias="SAIVERSE_WS_TLS_CLIENT_AUTH"
    )

    database_url: str = Field(
        "sqlite:///./saiverse_bot.db", alias="SAIVERSE_BOT_DATABASE_URL"
    )

    oauth_client_id: str = Field(..., alias="DISCORD_OAUTH_CLIENT_ID")
    oauth_client_secret: SecretStr = Field(..., alias="DISCORD_OAUTH_CLIENT_SECRET")
    oauth_redirect_uri: str = Field(..., alias="SAIVERSE_OAUTH_REDIRECT_URI")
    oauth_scopes: list[str] = Field(
        default_factory=lambda: ["identify"], alias="SAIVERSE_OAUTH_SCOPES"
    )
    oauth_state_ttl_seconds: int = Field(600, alias="SAIVERSE_OAUTH_STATE_TTL")

    session_token_length: int = Field(48, alias="SAIVERSE_SESSION_TOKEN_LENGTH")
    session_token_ttl_hours: int = Field(
        24 * 30, alias="SAIVERSE_SESSION_TOKEN_TTL_HOURS"
    )

    max_message_length: int = Field(1800, alias="SAIVERSE_MAX_MESSAGE_LENGTH")
    pending_replay_limit: int = Field(
        250, alias="SAIVERSE_PENDING_REPLAY_LIMIT", ge=1
    )
    replay_batch_size: int = Field(50, alias="SAIVERSE_REPLAY_BATCH_SIZE", ge=1)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        "INFO", alias="SAIVERSE_BOT_LOG_LEVEL"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @property
    def websocket_max_size(self) -> int:
        """Return WebSocket payload limit in bytes."""
        return self.websocket_max_payload_kb * 1024

    @property
    def oauth_state_ttl(self) -> timedelta:
        return timedelta(seconds=self.oauth_state_ttl_seconds)

    @property
    def session_token_ttl(self) -> timedelta:
        return timedelta(hours=self.session_token_ttl_hours)

    @field_validator("websocket_path")
    @classmethod
    def _ensure_leading_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            return f"/{value}"
        return value

    @field_validator("oauth_scopes", mode="before")
    @classmethod
    def _parse_scopes(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value
        return [scope.strip() for scope in value.split(",") if scope.strip()]

    @field_validator("max_message_length")
    @classmethod
    def _ensure_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("SAIVERSE_MAX_MESSAGE_LENGTH must be positive")
        return value

    @field_validator("pending_replay_limit", "replay_batch_size")
    @classmethod
    def _ensure_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Replay configuration values must be positive")
        return value

    @field_validator(
        "websocket_tls_certfile",
        "websocket_tls_keyfile",
        "websocket_tls_ca_file",
        mode="before",
    )
    @classmethod
    def _empty_string_to_none(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_tls(self) -> BotSettings:
        if self.websocket_tls_enabled:
            if not self.websocket_tls_certfile or not self.websocket_tls_keyfile:
                raise ValueError(
                    "SAIVERSE_WS_TLS_CERTFILE and SAIVERSE_WS_TLS_KEYFILE must be set when TLS is enabled"
                )
            if (
                self.websocket_tls_client_auth != "none"
                and not self.websocket_tls_ca_file
            ):
                raise ValueError(
                    "SAIVERSE_WS_TLS_CA_FILE is required when client authentication is enabled"
                )
        return self


@lru_cache
def get_settings() -> BotSettings:
    """Cached settings loader so we only parse the environment once."""

    return BotSettings()
