from __future__ import annotations

from datetime import timedelta

import pytest

from discord_gateway.bot.auth import AuthService, OAuthStateError
from discord_gateway.bot.database import (
    BotDatabase,
    LocalAppSession,
    hash_token,
    utcnow,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class FakeOAuthClient:
    def __init__(self, user_id: str = "discord-user"):
        self.calls: list[tuple[str, str]] = []
        self._user_id = user_id

    async def __aenter__(self) -> FakeOAuthClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, data: dict, headers: dict | None = None) -> FakeResponse:
        self.calls.append(("post", url))
        return FakeResponse(200, {"access_token": "access-token", "token_type": "Bearer"})

    async def get(self, url: str, headers: dict | None = None) -> FakeResponse:
        self.calls.append(("get", url))
        return FakeResponse(200, {"id": self._user_id})


def create_service(tmp_path, make_settings):
    db_path = tmp_path / "bot.db"
    settings = make_settings(database_url=f"sqlite:///{db_path}")
    database = BotDatabase(settings.database_url)
    database.migrate()
    clients: list[FakeOAuthClient] = []

    def factory() -> FakeOAuthClient:
        client = FakeOAuthClient()
        clients.append(client)
        return client

    service = AuthService(settings, database, http_client_factory=factory)
    return service, database, settings, clients


def test_begin_authorization_persists_state(tmp_path, make_settings):
    service, database, settings, _ = create_service(tmp_path, make_settings)
    session = service.begin_authorization()

    assert settings.oauth_client_id in session.authorize_url
    assert session.state in session.authorize_url

    stored = database.consume_oauth_state(session.state)
    assert stored is not None
    assert stored.state == session.state


@pytest.mark.asyncio
async def test_complete_authorization_returns_session_token(tmp_path, make_settings):
    service, database, settings, clients = create_service(tmp_path, make_settings)
    authorization = service.begin_authorization()

    issued = await service.complete_authorization("auth-code", authorization.state, label="desktop")

    assert len(issued.token) == settings.session_token_length
    auth_session = database.authenticate_token(issued.token)
    assert auth_session is not None
    assert auth_session.discord_user_id == issued.discord_user_id
    assert clients  # ensure HTTP client was used


@pytest.mark.asyncio
async def test_complete_authorization_rejects_unknown_state(tmp_path, make_settings):
    service, _, _, _ = create_service(tmp_path, make_settings)

    with pytest.raises(OAuthStateError):
        await service.complete_authorization("code", "missing-state")


@pytest.mark.asyncio
async def test_revoke_token_marks_session(tmp_path, make_settings):
    service, database, _, _ = create_service(tmp_path, make_settings)
    state = service.begin_authorization().state
    issued = await service.complete_authorization("auth-code", state)

    assert service.revoke_token(issued.token) is True
    assert database.authenticate_token(issued.token) is None


def test_cleanup_artifacts_removes_entries(tmp_path, make_settings):
    service, database, _, _ = create_service(tmp_path, make_settings)
    state = service.begin_authorization().state
    database.consume_oauth_state(state)
    with database.session() as session:
        session.add(
            LocalAppSession(
                discord_user_id="user",
                token_hash=hash_token("token"),
                expires_at=utcnow() - timedelta(hours=1),
            )
        )
    result = service.cleanup_artifacts()
    assert "sessions" in result and result["sessions"] >= 1
    assert "oauth_states" in result
