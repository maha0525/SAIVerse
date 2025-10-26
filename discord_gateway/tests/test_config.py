import pytest


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    env_vars = [
        "DISCORD_BOT_TOKEN",
        "SAIVERSE_BOT_DATABASE_URL",
        "SAIVERSE_WS_HOST",
        "SAIVERSE_WS_PORT",
        "SAIVERSE_WS_PATH",
        "SAIVERSE_WS_HEARTBEAT_SECONDS",
        "SAIVERSE_WS_MAX_PAYLOAD_KB",
        "DISCORD_OAUTH_CLIENT_ID",
        "DISCORD_OAUTH_CLIENT_SECRET",
        "SAIVERSE_OAUTH_REDIRECT_URI",
        "SAIVERSE_OAUTH_SCOPES",
        "SAIVERSE_OAUTH_STATE_TTL",
        "SAIVERSE_SESSION_TOKEN_LENGTH",
        "SAIVERSE_SESSION_TOKEN_TTL_HOURS",
        "SAIVERSE_MAX_MESSAGE_LENGTH",
    ]
    for key in env_vars:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in env_vars:
        monkeypatch.delenv(key, raising=False)


def test_settings_defaults(tmp_path, make_settings):
    database_path = tmp_path / "bot.db"
    settings = make_settings(
        discord_bot_token="another-token",
        database_url=f"sqlite:///{database_path}",
    )

    assert settings.discord_bot_token == "another-token"
    assert settings.websocket_path.startswith("/")
    assert settings.websocket_max_size == settings.websocket_max_payload_kb * 1024
    assert settings.database_url == f"sqlite:///{database_path}"


def test_settings_enforces_leading_slash(monkeypatch, make_settings):
    monkeypatch.setenv("SAIVERSE_WS_PATH", "custom")
    settings = make_settings()
    assert settings.websocket_path == "/custom"
