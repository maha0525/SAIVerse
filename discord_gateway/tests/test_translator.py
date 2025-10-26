from discord_gateway.translator import GatewayCommand, GatewayEvent, GatewayTranslator


def test_translator_roundtrip():
    translator = GatewayTranslator()
    command = GatewayCommand(type="send_message", payload={"content": "hi"})
    encoded = translator.encode_command(command)
    assert encoded == {"type": "send_message", "payload": {"content": "hi"}}

    message = {"type": "discord_message", "payload": {"text": "hello"}}
    event = translator.decode_event(message)
    assert isinstance(event, GatewayEvent)
    assert event.type == "discord_message"
    assert event.payload["text"] == "hello"
    assert event.raw == message
