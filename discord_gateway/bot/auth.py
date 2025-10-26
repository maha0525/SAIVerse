from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from .config import BotSettings
from .database import BotDatabase, IssuedToken, utcnow

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


class OAuthStateError(Exception):
    """Raised when the OAuth2 state parameter is invalid or expired."""


class TokenExchangeError(Exception):
    """Raised when the OAuth2 token exchange fails."""


@dataclass(slots=True)
class AuthorizationSession:
    authorize_url: str
    state: str


class AuthService:
    """Handle OAuth2 authorization and long-lived session token lifecycle."""

    def __init__(
        self,
        settings: BotSettings,
        database: BotDatabase,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self._settings = settings
        self._database = database
        self._http_client_factory = http_client_factory or self._default_http_client_factory

    def begin_authorization(
        self,
        *,
        redirect_uri: str | None = None,
        scopes: Sequence[str] | None = None,
    ) -> AuthorizationSession:
        state = self._generate_state()
        redirect = redirect_uri or self._settings.oauth_redirect_uri
        scope_values = list(scopes) if scopes is not None else list(self._settings.oauth_scopes)

        self._database.create_oauth_state(state, redirect, self._settings.oauth_state_ttl)

        query = urlencode(
            {
                "client_id": self._settings.oauth_client_id,
                "response_type": "code",
                "scope": " ".join(scope_values),
                "state": state,
                "redirect_uri": redirect,
                "prompt": "consent",
            }
        )
        return AuthorizationSession(
            authorize_url=f"{DISCORD_AUTHORIZE_URL}?{query}",
            state=state,
        )

    async def complete_authorization(
        self,
        code: str,
        state: str,
        *,
        label: str | None = None,
    ) -> IssuedToken:
        state_record = self._database.consume_oauth_state(state)
        if not state_record:
            raise OAuthStateError("State is invalid or has expired.")

        async with self._http_client_factory() as client:
            access_token, token_type = await self._exchange_code(
                client, code, state_record.redirect_uri
            )
            discord_user_id = await self._fetch_user_id(client, access_token, token_type)

        raw_token = self._generate_session_token()
        expires_at = utcnow() + self._settings.session_token_ttl
        return self._database.create_session_token(
            discord_user_id=discord_user_id,
            raw_token=raw_token,
            label=label,
            expires_at=expires_at,
            state_id=state_record.id,
        )

    def revoke_token(self, raw_token: str) -> bool:
        return self._database.revoke_token(raw_token)

    def revoke_tokens_for_user(self, discord_user_id: str) -> int:
        return self._database.revoke_tokens_for_user(discord_user_id)

    def cleanup_artifacts(self) -> dict[str, int]:
        return {
            "oauth_states": self._database.prune_oauth_states(),
            "sessions": self._database.prune_expired_sessions(),
        }

    async def _exchange_code(
        self,
        client: httpx.AsyncClient,
        code: str,
        redirect_uri: str,
    ) -> tuple[str, str]:
        response = await client.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": self._settings.oauth_client_id,
                "client_secret": self._settings.oauth_client_secret.get_secret_value(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            raise TokenExchangeError(f"Discord token endpoint returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("Failed to decode token response") from exc

        access_token = payload.get("access_token")
        token_type = payload.get("token_type", "Bearer")
        if not access_token:
            raise TokenExchangeError("Token response missing access_token")
        return access_token, token_type

    async def _fetch_user_id(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        token_type: str,
    ) -> str:
        response = await client.get(
            DISCORD_USER_URL,
            headers={"Authorization": f"{token_type} {access_token}"},
        )
        if response.status_code != 200:
            raise TokenExchangeError(f"Discord user endpoint returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TokenExchangeError("Failed to decode user info response") from exc

        user_id = payload.get("id")
        if not user_id:
            raise TokenExchangeError("User info response missing id")
        return str(user_id)

    def _generate_state(self) -> str:
        return secrets.token_urlsafe(32)

    def _generate_session_token(self) -> str:
        # token_urlsafe may create slightly longer strings, so we trim to the requested length.
        length = self._settings.session_token_length
        token = secrets.token_urlsafe(length)
        return token[:length]

    @staticmethod
    def _default_http_client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10)
