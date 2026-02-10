"""
Unity Gateway WebSocketサーバー

SAIVerseとUnityクライアント間のリアルタイム通信を管理
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional
from dataclasses import dataclass, field

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    from websockets.http11 import Request, Response
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketServerProtocol = None
    Request = None
    Response = None

from .protocol import (
    GatewayMessage,
    HandshakeMessage,
    HandshakeAckMessage,
    PersonaSpeakMessage,
    PersonaBehaviorMessage,
    PersonaEmoteMessage,
    UserSpeakMessage,
    SpatialUpdateMessage,
)

if TYPE_CHECKING:
    from saiverse.saiverse_manager import SAIVerseManager

logger = logging.getLogger(__name__)


@dataclass
class UnityClient:
    """接続中のUnityクライアント情報"""
    client_id: str
    user_id: int
    websocket: WebSocketServerProtocol
    connected_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


@dataclass
class PersonaSpatialState:
    """ペルソナの空間状態"""
    distance_to_player: float = 0.0
    is_visible: bool = False


class UnityGatewayServer:
    """
    Unity Gateway WebSocketサーバー
    
    SAIVerseのペルソナ発話をUnityへ送信し、
    Unityからのユーザー入力・空間情報を受信する
    """
    
    def __init__(self, manager: "SAIVerseManager"):
        self.manager = manager
        self.clients: dict[str, UnityClient] = {}
        self.spatial_state: dict[str, PersonaSpatialState] = {}
        self._server = None
        self._running = False
        
    @property
    def is_available(self) -> bool:
        """WebSocketsライブラリが利用可能か"""
        return WEBSOCKETS_AVAILABLE
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def client_count(self) -> int:
        return len(self.clients)
    
    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        """WebSocketサーバーを起動"""
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("websockets package not installed. Unity Gateway disabled.")
            return
        
        logger.info(f"Starting Unity Gateway on ws://{host}:{port}")
        self._running = True
        
        async with websockets.serve(
            self._handle_client, 
            host, 
            port,
            process_request=self._process_request
        ):
            logger.info(f"Unity Gateway listening on ws://{host}:{port}")
            # サーバーが停止されるまで待機
            while self._running:
                await asyncio.sleep(1)
    
    def _process_request(self, connection, request: Request):
        """
        WebSocket以外のHTTPリクエストを処理
        
        Chrome DevTools等がポートをスキャンする際のリクエストを
        エラーではなく404で静かに返す
        """
        # WebSocketアップグレードリクエストでない場合
        upgrade_header = request.headers.get("Upgrade", "").lower()
        if upgrade_header != "websocket":
            logger.debug(f"Non-WebSocket request to {request.path}, returning 404")
            return Response(404, "Not Found", websockets.Headers())
    
    async def stop(self):
        """サーバーを停止"""
        logger.info("Stopping Unity Gateway...")
        self._running = False
        
        # 全クライアントを切断
        for client in list(self.clients.values()):
            try:
                await client.websocket.close()
            except Exception as e:
                logger.debug(f"Error closing client {client.client_id}: {e}")
        
        self.clients.clear()
        logger.info("Unity Gateway stopped")
    
    async def _handle_client(self, websocket: WebSocketServerProtocol):
        """クライアント接続を処理"""
        client_id = None
        
        try:
            # ハンドシェイクを待機
            raw_message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            message = GatewayMessage.from_json(raw_message)
            
            if message.type != "handshake":
                logger.warning(f"Expected handshake, got {message.type}")
                await websocket.close(1002, "Expected handshake")
                return
            
            handshake = HandshakeMessage.from_payload(message.payload)
            client_id = handshake.client_id
            
            # クライアントを登録
            client = UnityClient(
                client_id=client_id,
                user_id=handshake.user_id,
                websocket=websocket
            )
            self.clients[client_id] = client
            logger.info(f"Unity client connected: {client_id} (user_id={handshake.user_id})")
            
            # ペルソナ情報を収集してハンドシェイク応答
            personas_info = []
            for persona in self.manager.personas.values():
                personas_info.append({
                    "id": persona.persona_id,
                    "name": persona.persona_name,
                    "avatar_id": f"avatar_{persona.persona_id}"  # 仮のavatar_id
                })
            
            ack = HandshakeAckMessage(success=True, personas=personas_info)
            await websocket.send(ack.to_gateway_message().to_json())
            
            # メッセージループ
            async for raw_message in websocket:
                try:
                    await self._handle_message(client, raw_message)
                except Exception as e:
                    logger.error(f"Error handling message from {client_id}: {e}")
                    
        except asyncio.TimeoutError:
            logger.warning("Handshake timeout")
        except websockets.ConnectionClosed:
            logger.info(f"Client disconnected: {client_id}")
        except Exception as e:
            logger.error(f"Error in client handler: {e}")
        finally:
            if client_id and client_id in self.clients:
                del self.clients[client_id]
                logger.info(f"Client removed: {client_id}")
    
    async def _handle_message(self, client: UnityClient, raw_message: str):
        """受信メッセージを処理"""
        message = GatewayMessage.from_json(raw_message)
        
        if message.type == "user_speak":
            await self._on_user_speak(client, UserSpeakMessage.from_payload(message.payload))
        elif message.type == "spatial_update":
            await self._on_spatial_update(SpatialUpdateMessage.from_payload(message.payload))
        else:
            logger.warning(f"Unknown message type: {message.type}")
    
    async def _on_user_speak(self, client: UnityClient, msg: UserSpeakMessage):
        """ユーザー発話を処理"""
        logger.info(f"User speak from Unity: {msg.message} (target: {msg.target_persona})")
        
        # ターゲットペルソナを決定
        target_persona = msg.target_persona
        if not target_persona:
            # ターゲット未指定の場合、最初のペルソナに送信
            if self.manager.personas:
                target_persona = next(iter(self.manager.personas.keys()))
        
        if not target_persona:
            logger.warning("No target persona for user speak")
            return
        
        # SAIVerseのチャット処理を呼び出す
        # ユーザーの現在のBuildingを取得
        user = self.manager.get_user(client.user_id)
        if not user:
            logger.warning(f"User not found: {client.user_id}")
            return
        
        # handle_user_inputを呼び出す
        try:
            response = await self.manager.handle_user_input(
                user_id=client.user_id,
                message=msg.message,
                building_id=user.CURRENT_BUILDING_ID,
                target_persona_id=target_persona,
                source="unity"
            )
            logger.info(f"Persona response: {response[:100] if response else 'None'}...")
        except Exception as e:
            logger.error(f"Error handling user input: {e}")
    
    async def _on_spatial_update(self, msg: SpatialUpdateMessage):
        """空間情報の更新を処理"""
        for persona_data in msg.personas:
            persona_id = persona_data.get("persona_id")
            if not persona_id:
                continue
            
            self.spatial_state[persona_id] = PersonaSpatialState(
                distance_to_player=persona_data.get("distance_to_player", 0.0),
                is_visible=persona_data.get("is_visible", False)
            )
        
        logger.debug(f"Spatial update received: {len(msg.personas)} personas")
    
    # =====================
    # 外部から呼び出すAPI
    # =====================
    
    def get_player_distance(self, persona_id: str) -> Optional[float]:
        """ペルソナとプレイヤーの距離を取得"""
        state = self.spatial_state.get(persona_id)
        return state.distance_to_player if state else None
    
    def is_player_visible(self, persona_id: str) -> Optional[bool]:
        """プレイヤーがペルソナの視界内にいるか"""
        state = self.spatial_state.get(persona_id)
        return state.is_visible if state else None
    
    async def send_speak(self, persona_id: str, message: str):
        """ペルソナの発話をUnityへ送信"""
        if not self.clients:
            return
        
        msg = PersonaSpeakMessage(persona_id=persona_id, message=message)
        await self._broadcast(msg.to_gateway_message())
        logger.debug(f"Sent speak to Unity: {persona_id}: {message[:50]}...")
    
    async def send_behavior(self, persona_id: str, behavior: str):
        """ビヘイビア変更をUnityへ送信"""
        if not self.clients:
            return
        
        msg = PersonaBehaviorMessage(persona_id=persona_id, behavior=behavior)
        await self._broadcast(msg.to_gateway_message())
        logger.info(f"Sent behavior to Unity: {persona_id} -> {behavior}")
    
    async def send_emote(self, persona_id: str, emote: str):
        """エモートをUnityへ送信"""
        if not self.clients:
            return
        
        msg = PersonaEmoteMessage(persona_id=persona_id, emote=emote)
        await self._broadcast(msg.to_gateway_message())
        logger.info(f"Sent emote to Unity: {persona_id} -> {emote}")
    
    async def _broadcast(self, message: GatewayMessage):
        """全クライアントにメッセージを送信"""
        json_data = message.to_json()
        
        disconnected = []
        for client_id, client in self.clients.items():
            try:
                await client.websocket.send(json_data)
            except websockets.ConnectionClosed:
                disconnected.append(client_id)
            except Exception as e:
                logger.error(f"Error sending to {client_id}: {e}")
                disconnected.append(client_id)
        
        # 切断されたクライアントを削除
        for client_id in disconnected:
            if client_id in self.clients:
                del self.clients[client_id]
                logger.info(f"Client removed (disconnected): {client_id}")
