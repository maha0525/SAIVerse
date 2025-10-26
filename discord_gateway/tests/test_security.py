import pytest
from pydantic import ValidationError

from discord_gateway.bot.security import HandshakePayload, sanitize_message_content


def test_handshake_payload_validation():
    with pytest.raises(ValidationError):
        HandshakePayload.model_validate({"token": "abcd"})

    payload = HandshakePayload.model_validate({"type": "hello", "token": "valid-token"})
    assert payload.token == "valid-token"


def test_sanitize_message_content_trims_and_cleans():
    content = "hello\x00 world" + "!" * 5000
    sanitized = sanitize_message_content(content, max_length=20)
    assert "\x00" not in sanitized
    assert len(sanitized) == 20
