from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class GatewayEvent:
    """Botから受信したイベントを表現する汎用データ構造。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class GatewayCommand:
    """Botへ送信するコマンド（SAIVerse本体のアクション指示）。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class GatewayTranslator:
    """Gateway内部でやり取りするJSONをアプリ用オブジェクトへ変換する責務を持つ。"""

    def decode_event(self, message: dict[str, Any]) -> GatewayEvent:
        event_type = message.get("type", "unknown")
        payload = message.get("payload", {})
        if not isinstance(payload, dict):
            payload = {"value": payload}
        return GatewayEvent(type=event_type, payload=payload, raw=message)

    def encode_command(self, command: GatewayCommand) -> dict[str, Any]:
        return {"type": command.type, "payload": command.payload}
