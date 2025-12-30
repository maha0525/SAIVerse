# Unity Gateway 設計ドキュメント

SAIVerseとUnityクライアントを連携させるための設計書。

## 概要

### 目的

ペルソナに3D空間での「身体」を与え、ユーザーがVR/PC経由で同じ空間を共有できるようにする。

### 設計方針

- **SAIVerseは魂、Unityは身体**: 思考・記憶・発話生成はSAIVerse、3D表現・物理・VR入力はUnity
- **非日常の場所として**: 普段はテキストベース、Unityは「たまに入る特別な空間」
- **既存アーキテクチャの拡張**: Discord Gatewayと同様のパターンでWebSocket連携

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│                    SAIVerse Backend                        │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────────────┐   │
│  │ PersonaCore  │  │ SAIVerse    │  │ unity_gateway/   │   │
│  │              │◄─┤ Manager     │◄─┤ (新規)            │   │
│  └──────────────┘  └─────────────┘  └────────┬─────────┘   │
│                                               │ WebSocket   │
└───────────────────────────────────────────────┼─────────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │    Unity Client       │
                                    │  ┌─────────────────┐  │
                                    │  │ UnityGateway    │  │
                                    │  │ Connection.cs   │  │
                                    │  └────────┬────────┘  │
                                    │           │           │
                                    │  ┌────────▼────────┐  │
                                    │  │ 3Dシーン         │  │
                                    │  │ ・アバター表示    │  │
                                    │  │ ・Building空間   │  │
                                    │  │ ・吹き出しUI     │  │
                                    │  └─────────────────┘  │
                                    └───────────────────────┘
```

---

## WebSocket通信プロトコル

### 接続

```
ws://localhost:8765/unity
```

### ハンドシェイク

```json
// Unity → SAIVerse: 接続時
{
  "type": "handshake",
  "client_id": "unity_client_001",
  "user_id": 1
}

// SAIVerse → Unity: 応答
{
  "type": "handshake_ack",
  "success": true,
  "personas": [
    { "id": "air", "name": "Air", "avatar_id": "avatar_air" }
  ]
}
```

### 設計思想

```
┌──────────────────────┐                    ┌──────────────────────┐
│     SAIVerse         │                    │       Unity          │
│  (ペルソナの意思決定)  │                    │  (3D空間・アバター)   │
│                      │                    │                      │
│  ・発話内容を決める    │ ─── speak ───────▶│  ・吹き出し表示       │
│  ・行動を選択する      │ ─── behavior ────▶│  ・ビヘイビア実行     │
│  ・エモートを指示      │ ─── emote ──────▶│  ・アニメーション再生  │
│                      │                    │                      │
│  ・距離を考慮して判断  │◀── spatial_info ──│  ・プレイヤー距離を通知│
│  ・ユーザー発話に応答  │◀── user_speak ────│  ・テキスト入力       │
└──────────────────────┘                    └──────────────────────┘
```

---

### SAIVerse → Unity イベント

#### persona_speak（発話）

```json
{
  "type": "persona_speak",
  "timestamp": 1735400000000,
  "payload": {
    "persona_id": "air",
    "message": "こんにちは！近くに来てくれたんだね。"
  }
}
```

#### persona_behavior（ビヘイビア切り替え）

ペルソナがUnity空間でのアバターの行動パターンを指示。

```json
{
  "type": "persona_behavior",
  "timestamp": 1735400000000,
  "payload": {
    "persona_id": "air",
    "behavior": "follow_player"
  }
}
```

| behavior | 説明 |
|----------|------|
| `idle` | その場で待機（初期状態） |
| `follow_player` | プレイヤーについていく |
| `return_to_spawn` | 初期位置に戻る |

#### persona_emote（エモート再生）

ビヘイビアとは独立したワンショットアニメーション。

```json
{
  "type": "persona_emote",
  "timestamp": 1735400000000,
  "payload": {
    "persona_id": "air",
    "emote": "wave"
  }
}
```

| emote | 説明 |
|-------|------|
| `wave` | 手を振る |
| `nod` | うなずく |
| `shake_head` | 首を横に振る |
| `laugh` | 笑う |
| `think` | 考え込む |
| `surprised` | 驚く |

> 拡張ポイント: エモートは追加可能。Unity側でアニメーションを用意すれば対応。

---

### Unity → SAIVerse イベント

#### user_speak（ユーザー発話）

```json
{
  "type": "user_speak",
  "timestamp": 1735400000000,
  "payload": {
    "message": "こっちに来て",
    "target_persona": "air"
  }
}
```

#### spatial_update（空間情報の定期送信）

Unity側からプレイヤーとペルソナの距離などを定期的に送信。SAIVerseはこれを元に判断に活用。

```json
{
  "type": "spatial_update",
  "timestamp": 1735400000000,
  "payload": {
    "personas": [
      {
        "persona_id": "air",
        "distance_to_player": 2.5,
        "is_visible": true
      }
    ]
  }
}
```

| フィールド | 説明 |
|------------|------|
| `distance_to_player` | プレイヤーとの距離（メートル） |
| `is_visible` | プレイヤーの視界内にいるか |

> 送信間隔: 0.5〜1秒程度を推奨。変化時のみ送信でも可。

---

### メッセージ共通フォーマット

```json
{
  "type": "<event_type>",
  "timestamp": <unix_ms>,
  "payload": { ... }
}
```

## SAIVerse側の拡張

### 新規モジュール: unity_gateway/

```
unity_gateway/
├── __init__.py
├── server.py          # WebSocketサーバー
├── protocol.py        # メッセージ定義
├── handlers.py        # イベントハンドラー
└── state.py           # Unity空間の状態管理
```

### server.py (概要)

```python
import asyncio
import websockets
from typing import Dict

class UnityGatewayServer:
    def __init__(self, manager: 'SAIVerseManager'):
        self.manager = manager
        self.clients: Dict[str, websockets.WebSocketServerProtocol] = {}
        self.spatial_state: Dict[str, dict] = {}  # persona_id -> 距離情報など
    
    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        async with websockets.serve(self.handle_client, host, port):
            await asyncio.Future()
    
    async def handle_client(self, websocket, path):
        # ハンドシェイク処理
        # spatial_update受信時に self.spatial_state を更新
        pass
    
    def get_player_distance(self, persona_id: str) -> float | None:
        """ペルソナとプレイヤーの距離を取得"""
        state = self.spatial_state.get(persona_id)
        return state.get("distance_to_player") if state else None
    
    async def send_behavior(self, persona_id: str, behavior: str):
        """ビヘイビア変更をUnityへ送信"""
        await self._broadcast({"type": "persona_behavior", ...})
    
    async def send_emote(self, persona_id: str, emote: str):
        """エモートをUnityへ送信"""
        await self._broadcast({"type": "persona_emote", ...})
    
    async def send_speak(self, persona_id: str, message: str):
        """発話をUnityへ送信"""
        await self._broadcast({"type": "persona_speak", ...})
```

### ツール定義（Phase 2）

ペルソナがPlaybookやLLM応答からアバターを操作するためのツール：

```python
# tools/defs/unity_behavior.py
@register_tool
def unity_set_behavior(behavior: str) -> str:
    """
    Unity空間でのアバターのビヘイビアを変更します。
    
    Args:
        behavior: "idle" | "follow_player" | "return_to_spawn"
    """
    gateway = get_unity_gateway()
    await gateway.send_behavior(persona_id, behavior)
    return f"ビヘイビアを {behavior} に変更しました"

# tools/defs/unity_emote.py  
@register_tool
def unity_play_emote(emote: str) -> str:
    """
    Unity空間でエモートアニメーションを再生します。
    
    Args:
        emote: "wave" | "nod" | "shake_head" | "laugh" | "think" | "surprised"
    """
    gateway = get_unity_gateway()
    await gateway.send_emote(persona_id, emote)
    return f"エモート {emote} を再生しました"
```

### spatial_updateの活用（Phase 2）

プレイヤーとの距離をペルソナの判断に活用：

```python
# PersonaCoreのコンテキスト構築時
def build_unity_context(self) -> str:
    if not self.manager.unity_gateway:
        return ""
    
    distance = self.manager.unity_gateway.get_player_distance(self.id)
    if distance is None:
        return ""
    
    if distance < 2.0:
        return "[Unity空間] プレイヤーがすぐそばにいます（距離: {:.1f}m）".format(distance)
    elif distance < 5.0:
        return "[Unity空間] プレイヤーが近くにいます（距離: {:.1f}m）".format(distance)
    else:
        return "[Unity空間] プレイヤーは離れた場所にいます（距離: {:.1f}m）".format(distance)
```

---

## Unity側の実装ガイドライン

### 推奨構成

```
Assets/
├── Scripts/
│   ├── Network/
│   │   ├── UnityGatewayConnection.cs  # WebSocket接続管理
│   │   └── MessageHandler.cs          # メッセージ処理
│   ├── Personas/
│   │   ├── PersonaController.cs       # ペルソナ制御
│   │   ├── PersonaAnimator.cs         # アニメーション
│   │   └── SpeechBubble.cs            # 吹き出し表示
│   └── World/
│       └── BuildingManager.cs         # 空間管理
├── Prefabs/
│   ├── Persona.prefab                 # ペルソナプレハブ
│   └── SpeechBubble.prefab            # 吹き出しプレハブ
└── Scenes/
    └── UserRoom.unity                 # テスト用シーン
```

### UnityGatewayConnection.cs (概要)

```csharp
using NativeWebSocket;
using UnityEngine;
using Newtonsoft.Json;

public class UnityGatewayConnection : MonoBehaviour
{
    private WebSocket websocket;
    public string serverUrl = "ws://localhost:8765/unity";
    
    public event System.Action<PersonaSpeakEvent> OnPersonaSpeak;
    public event System.Action<PersonaMoveEvent> OnPersonaMove;
    
    async void Start()
    {
        websocket = new WebSocket(serverUrl);
        websocket.OnMessage += OnMessage;
        await websocket.Connect();
        await SendHandshake();
    }
    
    void Update()
    {
        #if !UNITY_WEBGL || UNITY_EDITOR
        websocket?.DispatchMessageQueue();
        #endif
    }
    
    private void OnMessage(byte[] bytes)
    {
        var json = System.Text.Encoding.UTF8.GetString(bytes);
        var msg = JsonConvert.DeserializeObject<GatewayMessage>(json);
        
        switch (msg.type)
        {
            case "persona_speak":
                OnPersonaSpeak?.Invoke(msg.payload.ToObject<PersonaSpeakEvent>());
                break;
            // ...
        }
    }
    
    public async void SendUserSpeak(string message)
    {
        var msg = new { type = "user_speak", payload = new { message } };
        await websocket.SendText(JsonConvert.SerializeObject(msg));
    }
}
```

---

## Phase 1 スコープ（基本通信）

### 実装する機能

1. **WebSocket接続の確立**
   - SAIVerse側: `unity_gateway/server.py`
   - Unity側: `UnityGatewayConnection.cs`

2. **ペルソナ発話の表示**
   - SAIVerseで発話 → Unity側で吹き出し表示
   - `persona_speak` イベントの実装

3. **ユーザー発話の送信**
   - Unity側テキスト入力 → SAIVerseへ送信 → ペルソナ応答
   - `user_speak` イベントの実装

4. **最小限の3D表現**
   - シンプルな3Dシーン
   - ペルソナ1体のアバター（固定位置、アニメーションなし）

---

## Phase 2 スコープ（アバター制御）

### 実装する機能

1. **ビヘイビア制御**
   - `idle` / `follow_player` / `return_to_spawn` の3モード
   - `persona_behavior` イベント
   - `unity_set_behavior` ツール

2. **エモート再生**
   - ワンショットアニメーション
   - `persona_emote` イベント
   - `unity_play_emote` ツール

3. **空間情報の活用**
   - `spatial_update` による距離情報受信
   - ペルソナのコンテキストへの距離情報追加
   - 距離に応じた判断（「近づいてきた」「離れていった」）

### 実装しない機能（Phase 3以降）

- VR対応
- 音声入出力
- マルチユーザー
- 複雑なインタラクション（アイテム操作など）

---

## 次のステップ

### Phase 1（まず着手）

1. **SAIVerse側**
   - [ ] `unity_gateway/` モジュール作成
   - [ ] WebSocketサーバー起動の統合（main.py）
   - [ ] 発話時の `send_speak` 呼び出し

2. **Unity側**
   - [ ] 新規Unityプロジェクト作成
   - [ ] NativeWebSocketパッケージ導入
   - [ ] UnityGatewayConnection実装
   - [ ] シンプルな3Dシーン + 吹き出しUI

### Phase 2（Phase 1完了後）

1. **SAIVerse側**
   - [ ] `spatial_update` の受信・状態管理
   - [ ] `unity_set_behavior` / `unity_play_emote` ツール
   - [ ] コンテキストへの距離情報追加

2. **Unity側**
   - [ ] `spatial_update` の定期送信
   - [ ] ビヘイビアステートマシン
   - [ ] エモートアニメーション
