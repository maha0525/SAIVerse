"""
Unity Gateway プロトコル定義

SAIVerse ↔ Unity 間のWebSocketメッセージ形式を定義
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
import time
import json


@dataclass
class GatewayMessage:
    """基底メッセージクラス"""
    type: str
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    payload: dict = field(default_factory=dict)
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)
    
    @classmethod
    def from_json(cls, data: str | dict) -> "GatewayMessage":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(
            type=data.get("type", ""),
            timestamp=data.get("timestamp", int(time.time() * 1000)),
            payload=data.get("payload", {})
        )


# =====================
# Unity → SAIVerse
# =====================

@dataclass
class HandshakeMessage:
    """接続時のハンドシェイク"""
    client_id: str
    user_id: int
    
    @classmethod
    def from_payload(cls, payload: dict) -> "HandshakeMessage":
        return cls(
            client_id=payload.get("client_id", ""),
            user_id=payload.get("user_id", 0)
        )


@dataclass
class UserSpeakMessage:
    """ユーザーからの発話"""
    message: str
    target_persona: Optional[str] = None
    
    @classmethod
    def from_payload(cls, payload: dict) -> "UserSpeakMessage":
        return cls(
            message=payload.get("message", ""),
            target_persona=payload.get("target_persona")
        )


@dataclass
class SpatialUpdateMessage:
    """空間情報の更新（プレイヤーとの距離など）"""
    personas: list  # [{persona_id, distance_to_player, is_visible}, ...]
    
    @classmethod
    def from_payload(cls, payload: dict) -> "SpatialUpdateMessage":
        return cls(
            personas=payload.get("personas", [])
        )


# =====================
# SAIVerse → Unity
# =====================

@dataclass
class HandshakeAckMessage:
    """ハンドシェイク応答"""
    success: bool
    personas: list  # [{id, name, avatar_id}, ...]
    error: Optional[str] = None
    
    def to_gateway_message(self) -> GatewayMessage:
        payload = {
            "success": self.success,
            "personas": self.personas,
        }
        if self.error:
            payload["error"] = self.error
        return GatewayMessage(type="handshake_ack", payload=payload)


@dataclass
class PersonaSpeakMessage:
    """ペルソナの発話"""
    persona_id: str
    message: str
    
    def to_gateway_message(self) -> GatewayMessage:
        return GatewayMessage(
            type="persona_speak",
            payload={
                "persona_id": self.persona_id,
                "message": self.message,
            }
        )


BehaviorType = Literal["idle", "follow_player", "return_to_spawn"]

@dataclass
class PersonaBehaviorMessage:
    """ペルソナのビヘイビア変更"""
    persona_id: str
    behavior: BehaviorType
    
    def to_gateway_message(self) -> GatewayMessage:
        return GatewayMessage(
            type="persona_behavior",
            payload={
                "persona_id": self.persona_id,
                "behavior": self.behavior,
            }
        )


EmoteType = Literal["wave", "nod", "shake_head", "laugh", "think", "surprised"]

@dataclass
class PersonaEmoteMessage:
    """ペルソナのエモート再生"""
    persona_id: str
    emote: EmoteType
    
    def to_gateway_message(self) -> GatewayMessage:
        return GatewayMessage(
            type="persona_emote",
            payload={
                "persona_id": self.persona_id,
                "emote": self.emote,
            }
        )
