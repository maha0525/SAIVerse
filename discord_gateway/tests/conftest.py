import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pytest

from discord_gateway.bot.config import BotSettings


@pytest.fixture
def settings_kwargs() -> dict[str, object]:
    return {
        "discord_bot_token": "bot-token",
        "discord_application_id": "1234567890",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "very-secret",
        "oauth_redirect_uri": "https://example.com/callback",
        "oauth_scopes": ["identify"],
    }


@pytest.fixture
def make_settings(settings_kwargs):
    def _factory(**overrides) -> BotSettings:
        data = {**settings_kwargs, **overrides}
        return BotSettings(_env_file=None, **data)

    return _factory
