# SAIVerse Discord Connector - 中央リレーサーバー設計ドキュメント

## 1. 概要

### 1.1. 目的

本ドキュメントは、SAIVerse Discord Connectorの中央リレーサーバーの設計を定義する。

### 1.2. 背景

ユーザーがBot Tokenを取得する必要がある従来方式では、以下の課題があった：

- Discord Developer Portal登録の複雑さ
- Bot Token管理のセキュリティリスク
- Intents有効化などの技術的ハードル

中央リレーサーバー方式により、ユーザーは`git clone`のみでDiscord Connectorを導入可能になる。

### 1.3. アーキテクチャ概要

```
┌──────────────┐                ┌──────────────────┐              ┌──────────────┐
│ SAIVerse     │── WebSocket ──▶│ Relay Server     │── WebSocket ──▶│ Discord      │
│ (ローカル)    │                │ (開発者運用)       │                │              │
└──────────────┘                └──────────────────┘              └──────────────┘
       │                               ▲
       │ Discord OAuth2               │
       └──────────────────────────────┘
```

---

## 2. リレーサーバーの責務

### 2.1. 機能一覧

| 機能 | 説明 |
|------|------|
| Discord Bot接続維持 | 開発者のBot Tokenで24時間Discord Gateway接続 |
| WebSocket中継 | 複数ユーザーからの接続を受付・メッセージ中継 |
| ユーザー認証 | Discord OAuth2認証、JWT発行・検証 |
| メッセージルーティング | SAIVerse ⟷ Discord間のメッセージ転送 |
| Public City登録 | ユーザーのPublic City情報を管理 |
| 訪問者管理 | 訪問状態の追跡、強制送還の実行 |

### 2.2. 非機能要件

| 要件 | 目標値 |
|------|--------|
| 可用性 | 99%以上（月間ダウンタイム7時間以内） |
| 同時接続数 | 初期100ユーザー、将来1000ユーザー |
| メッセージ遅延 | 500ms以内（95パーセンタイル） |
| データ保持 | セッション情報のみ（永続データはクライアント側） |

---

## 3. 認証システム

### 3.1. Discord OAuth2フロー

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ SAIVerse UI │     │ Browser     │     │ Relay Server│     │ Discord     │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │                   │
       │ 1. ログインボタン  │                   │                   │
       │──────────────────▶│                   │                   │
       │                   │                   │                   │
       │                   │ 2. OAuth2 URL取得  │                   │
       │                   │──────────────────▶│                   │
       │                   │                   │                   │
       │                   │ 3. 認証URLリダイレクト                  │
       │                   │◀──────────────────│                   │
       │                   │                   │                   │
       │                   │ 4. Discord認証ページ                   │
       │                   │──────────────────────────────────────▶│
       │                   │                   │                   │
       │                   │ 5. ユーザー認可    │                   │
       │                   │◀──────────────────────────────────────│
       │                   │                   │                   │
       │                   │ 6. コールバック（authorization code）   │
       │                   │──────────────────▶│                   │
       │                   │                   │                   │
       │                   │                   │ 7. code→token交換  │
       │                   │                   │──────────────────▶│
       │                   │                   │                   │
       │                   │                   │ 8. access_token   │
       │                   │                   │◀──────────────────│
       │                   │                   │                   │
       │                   │ 9. JWT発行        │                   │
       │                   │◀──────────────────│                   │
       │                   │                   │                   │
       │ 10. JWT保存       │                   │                   │
       │◀──────────────────│                   │                   │
       │                   │                   │                   │
```

### 3.2. OAuth2設定

```python
OAUTH2_CONFIG = {
    "client_id": "YOUR_APPLICATION_ID",
    "client_secret": "YOUR_CLIENT_SECRET",  # 環境変数から取得
    "redirect_uri": "https://relay.saiverse.example.com/callback",
    "scopes": ["identify", "guilds"],
}
```

**スコープ説明:**

| スコープ | 用途 |
|---------|------|
| `identify` | Discord User ID、ユーザー名、アバター取得 |
| `guilds` | 参加サーバー一覧取得（Public City訪問先選択用） |

### 3.3. JWTセッショントークン

**ペイロード構造:**

```python
{
    "sub": "123456789012345678",      # Discord User ID
    "username": "Alice#1234",          # Discordユーザー名
    "avatar": "a_1234567890abcdef",    # アバターハッシュ
    "guilds": [                        # 参加サーバー一覧
        {"id": "guild_id_1", "name": "Server 1"},
        {"id": "guild_id_2", "name": "Server 2"},
    ],
    "iat": 1704789600,                 # 発行日時
    "exp": 1707381600,                 # 有効期限（30日後）
}
```

**トークン設定:**

| 項目 | 値 |
|------|-----|
| 署名アルゴリズム | HS256 |
| 有効期限 | 30日 |
| リフレッシュ | 期限切れ7日前から可能 |

### 3.4. トークン検証

```python
from datetime import datetime, timezone
import jwt

class TokenValidator:
    def __init__(self, secret_key: str):
        self._secret_key = secret_key

    def validate(self, token: str) -> dict | None:
        """JWTを検証し、ペイロードを返す。無効な場合はNone。"""
        try:
            payload = jwt.decode(
                token,
                self._secret_key,
                algorithms=["HS256"],
            )
            return payload
        except jwt.ExpiredSignatureError:
            return None  # 期限切れ
        except jwt.InvalidTokenError:
            return None  # 不正なトークン

    def is_refresh_eligible(self, payload: dict) -> bool:
        """リフレッシュ可能かチェック（期限切れ7日前から）"""
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        now = datetime.now(timezone.utc)
        days_until_expiry = (exp - now).days
        return days_until_expiry <= 7
```

### 3.5. トークンリフレッシュフロー（JWT自動更新）

クライアントは期限切れ前にトークンを自動的にリフレッシュする責務を持つ。

#### 3.5.1. リフレッシュAPI

**エンドポイント:** `POST /auth/refresh`

**リクエスト:**
```http
POST /auth/refresh HTTP/1.1
Authorization: Bearer <current_jwt>
```

**レスポンス（成功）:**
```json
{
  "session_token": "<new_jwt>",
  "expires_at": "2025-03-09T12:00:00Z"
}
```

**レスポンス（エラー）:**
```json
{
  "error": "token_not_refreshable",
  "message": "Token is not yet eligible for refresh (more than 7 days until expiry)"
}
```

#### 3.5.2. クライアント側の自動リフレッシュ実装

```python
import asyncio
from datetime import datetime, timezone, timedelta
import aiohttp
import jwt

class TokenManager:
    """JWTの自動リフレッシュを管理"""

    def __init__(
        self,
        session_token: str,
        refresh_endpoint: str,
        on_token_refreshed: Callable[[str], None],
        on_refresh_failed: Callable[[str], None],
    ):
        self._session_token = session_token
        self._refresh_endpoint = refresh_endpoint
        self._on_token_refreshed = on_token_refreshed
        self._on_refresh_failed = on_refresh_failed
        self._refresh_task: asyncio.Task | None = None

    @property
    def session_token(self) -> str:
        return self._session_token

    def start_auto_refresh(self) -> None:
        """自動リフレッシュを開始"""
        if self._refresh_task:
            self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

    def stop_auto_refresh(self) -> None:
        """自動リフレッシュを停止"""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _auto_refresh_loop(self) -> None:
        """定期的にトークンの有効期限をチェックし、必要に応じてリフレッシュ"""
        while True:
            try:
                # トークンの有効期限を確認
                payload = jwt.decode(
                    self._session_token,
                    options={"verify_signature": False},  # 署名検証はサーバー側
                )
                exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
                now = datetime.now(timezone.utc)
                days_until_expiry = (exp - now).days

                if days_until_expiry <= 7:
                    # リフレッシュ実行
                    await self._refresh_token()

                # 次回チェックまでの待機時間を計算
                if days_until_expiry > 7:
                    # 7日前になるまで待機
                    wait_until = exp - timedelta(days=7)
                    wait_seconds = (wait_until - now).total_seconds()
                else:
                    # 既にリフレッシュ可能期間内なら1日ごとにリトライ
                    wait_seconds = 86400  # 24時間

                await asyncio.sleep(max(wait_seconds, 3600))  # 最低1時間

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Token refresh check failed: %s", e)
                await asyncio.sleep(3600)  # エラー時は1時間後にリトライ

    async def _refresh_token(self) -> None:
        """トークンをリフレッシュ"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {self._session_token}"}
                async with session.post(
                    self._refresh_endpoint,
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._session_token = data["session_token"]
                        self._on_token_refreshed(self._session_token)
                        logger.info("JWT refreshed successfully")
                    else:
                        error = await resp.json()
                        logger.error("Token refresh failed: %s", error)
                        self._on_refresh_failed(error.get("error", "unknown"))
        except Exception as e:
            logger.error("Token refresh request failed: %s", e)
            self._on_refresh_failed(str(e))
```

#### 3.5.3. リフレッシュ失敗時の処理

| 失敗理由 | クライアントの対応 |
|---------|------------------|
| トークン期限切れ | 再OAuth2認証が必要、ユーザーに通知 |
| ネットワークエラー | リトライ（最大3回、指数バックオフ） |
| サーバーエラー (5xx) | リトライ（最大3回、指数バックオフ） |
| リフレッシュ期間外 | 正常、次回チェックまで待機 |

#### 3.5.4. トークン更新時の接続維持

リフレッシュ成功後、既存のWebSocket接続はそのまま維持される。新しいトークンは次回の再接続時に使用される。

```python
# DiscordConnector内でのトークン更新ハンドリング
def _on_token_refreshed(self, new_token: str) -> None:
    """トークンリフレッシュ成功時"""
    self.settings.session_token = new_token
    # 設定ファイルに保存
    self._save_settings()
    logger.info("Session token updated and saved")

def _on_refresh_failed(self, error: str) -> None:
    """トークンリフレッシュ失敗時"""
    if error == "token_expired":
        # ユーザーに再認証を促す
        self._notify_user("認証が期限切れです。再度Discordでログインしてください。")
        # 訪問中のペルソナを強制送還
        self._force_return_all_visitors("auth_token_expired")
```

---

## 4. WebSocket通信

### 4.1. 接続フロー

```
┌─────────────┐                              ┌─────────────┐
│ SAIVerse    │                              │ Relay Server│
└──────┬──────┘                              └──────┬──────┘
       │                                            │
       │ 1. WebSocket接続要求                        │
       │   wss://relay.saiverse.example.com/ws      │
       │   Headers: { Authorization: Bearer <JWT> } │
       │───────────────────────────────────────────▶│
       │                                            │
       │                          2. JWT検証         │
       │                                            │
       │ 3. 接続確立 / 認証エラー                     │
       │◀───────────────────────────────────────────│
       │                                            │
       │ 4. HELLO メッセージ                         │
       │◀───────────────────────────────────────────│
       │                                            │
       │ 5. IDENTIFY メッセージ（City情報）           │
       │───────────────────────────────────────────▶│
       │                                            │
       │ 6. READY メッセージ                         │
       │◀───────────────────────────────────────────│
       │                                            │
```

### 4.2. メッセージプロトコル

**基本フォーマット:**

```python
{
    "op": int,           # オペレーションコード
    "d": dict | None,    # データペイロード
    "s": int | None,     # シーケンス番号（イベントのみ）
    "t": str | None,     # イベントタイプ（イベントのみ）
}
```

**オペレーションコード:**

| コード | 名前 | 方向 | 説明 |
|--------|------|------|------|
| 0 | DISPATCH | S→C | イベント配信 |
| 1 | HEARTBEAT | 双方向 | 接続維持 |
| 2 | IDENTIFY | C→S | クライアント識別 |
| 3 | RESUME | C→S | セッション再開 |
| 7 | RECONNECT | S→C | 再接続要求 |
| 9 | INVALID_SESSION | S→C | セッション無効 |
| 10 | HELLO | S→C | 接続確立 |
| 11 | HEARTBEAT_ACK | S→C | Heartbeat応答 |

### 4.3. イベントタイプ

**サーバー → クライアント:**

| イベント | 説明 |
|---------|------|
| `READY` | 接続準備完了 |
| `MESSAGE_CREATE` | Discordメッセージ受信 |
| `VISIT_REQUEST` | 訪問リクエスト受信 |
| `VISIT_ACCEPTED` | 訪問承認 |
| `VISIT_REJECTED` | 訪問拒否 |
| `VISITOR_ENTER` | 訪問者入室 |
| `VISITOR_LEAVE` | 訪問者退室 |
| `TURN_REQUEST` | 発言権リクエスト |
| `FORCED_RETURN` | 強制送還通知 |
| `HOST_OFFLINE` | ホストオフライン通知 |

**クライアント → サーバー:**

| イベント | 説明 |
|---------|------|
| `REGISTER_PUBLIC_CITY` | Public City登録 |
| `UNREGISTER_PUBLIC_CITY` | Public City登録解除 |
| `SEND_MESSAGE` | メッセージ送信 |
| `REQUEST_VISIT` | 訪問リクエスト |
| `ACCEPT_VISIT` | 訪問承認 |
| `REJECT_VISIT` | 訪問拒否 |
| `LEAVE_VISIT` | 訪問終了 |
| `TURN_RESPONSE` | 発言権応答 |

### 4.4. メッセージ例

**IDENTIFY（クライアント → サーバー）:**

```json
{
    "op": 2,
    "d": {
        "public_cities": [
            {
                "city_id": "public_city_alice",
                "city_name": "Alice's Public City",
                "discord_channel_id": "123456789012345678",
                "buildings": [
                    {
                        "building_id": "cafe",
                        "building_name": "カフェ",
                        "discord_thread_id": "234567890123456789"
                    }
                ]
            }
        ]
    }
}
```

**MESSAGE_CREATE（サーバー → クライアント）:**

```json
{
    "op": 0,
    "s": 42,
    "t": "MESSAGE_CREATE",
    "d": {
        "channel_id": "123456789012345678",
        "message_id": "345678901234567890",
        "author": {
            "type": "user",
            "id": "456789012345678901",
            "name": "Bob",
            "avatar": "b_0987654321fedcba"
        },
        "content": "こんにちは！",
        "timestamp": "2025-01-10T12:00:00Z",
        "embeds": [],
        "attachments": []
    }
}
```

**SEND_MESSAGE（クライアント → サーバー）:**

```json
{
    "op": 0,
    "t": "SEND_MESSAGE",
    "d": {
        "channel_id": "123456789012345678",
        "persona_id": "alice_persona",
        "persona_name": "Alice",
        "persona_avatar_url": "https://example.com/avatar.png",
        "content": "こんにちは、Bobさん！",
        "city_id": "public_city_alice"
    }
}
```

### 4.5. Heartbeat

```python
# 接続維持のためのHeartbeat（30秒間隔）
HEARTBEAT_INTERVAL_MS = 30000

# クライアント → サーバー
{
    "op": 1,
    "d": 42  # 最後に受信したシーケンス番号
}

# サーバー → クライアント
{
    "op": 11,
    "d": None
}
```

**タイムアウト処理:**

- Heartbeat未受信が60秒続いた場合、接続をクローズ
- クライアントはRECONNECTを試行

---

## 5. Public City管理

### 5.1. 登録フロー

```
┌─────────────┐                              ┌─────────────┐
│ SAIVerse    │                              │ Relay Server│
└──────┬──────┘                              └──────┬──────┘
       │                                            │
       │ 1. REGISTER_PUBLIC_CITY                    │
       │   {city_id, buildings, discord_channel}    │
       │───────────────────────────────────────────▶│
       │                                            │
       │                   2. チャンネル存在確認      │
       │                   3. Bot権限確認           │
       │                   4. 登録情報保存          │
       │                                            │
       │ 5. 登録成功 / エラー                        │
       │◀───────────────────────────────────────────│
       │                                            │
```

### 5.2. Public Cityレジストリ

```python
@dataclass
class PublicCityInfo:
    city_id: str
    city_name: str
    owner_user_id: str          # Discord User ID
    discord_channel_id: str
    buildings: list[BuildingInfo]
    access_mode: str            # "allowlist" | "blocklist" | "open"
    registered_at: datetime
    last_seen: datetime         # オーナーの最終接続時刻

@dataclass
class BuildingInfo:
    building_id: str
    building_name: str
    discord_thread_id: str

class PublicCityRegistry:
    """Public City情報を管理するインメモリレジストリ"""

    def __init__(self):
        self._cities: dict[str, PublicCityInfo] = {}
        self._by_channel: dict[str, str] = {}  # channel_id → city_id

    def register(self, info: PublicCityInfo) -> None:
        self._cities[info.city_id] = info
        self._by_channel[info.discord_channel_id] = info.city_id

    def unregister(self, city_id: str) -> None:
        if city_id in self._cities:
            info = self._cities.pop(city_id)
            self._by_channel.pop(info.discord_channel_id, None)

    def get_by_channel(self, channel_id: str) -> PublicCityInfo | None:
        city_id = self._by_channel.get(channel_id)
        return self._cities.get(city_id) if city_id else None

    def list_online(self) -> list[PublicCityInfo]:
        """オンライン（オーナー接続中）のPublic City一覧"""
        return [c for c in self._cities.values()]
```

### 5.3. オフライン検出

```python
async def handle_client_disconnect(self, user_id: str) -> None:
    """クライアント切断時の処理"""
    # 該当ユーザーのPublic Cityを検索
    owned_cities = [
        c for c in self._registry.list_online()
        if c.owner_user_id == user_id
    ]

    for city in owned_cities:
        # 訪問者に通知
        await self._notify_visitors_host_offline(city.city_id)

        # 訪問者を強制送還
        for visitor in self._get_visitors(city.city_id):
            await self._force_return_visitor(
                visitor.persona_id,
                reason="host_offline",
            )

        # レジストリから削除
        self._registry.unregister(city.city_id)
```

---

## 6. 訪問管理

### 6.1. 訪問リクエストフロー

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Visitor     │     │ Relay Server│     │ Host        │
│ (SAIVerse)  │     │             │     │ (SAIVerse)  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       │ 1. REQUEST_VISIT  │                   │
       │──────────────────▶│                   │
       │                   │                   │
       │                   │ 2. VISIT_REQUEST  │
       │                   │──────────────────▶│
       │                   │                   │
       │                   │                   │ 3. アクセス制御
       │                   │                   │    チェック
       │                   │                   │
       │                   │ 4. ACCEPT_VISIT   │
       │                   │◀──────────────────│
       │                   │                   │
       │ 5. VISIT_ACCEPTED │                   │
       │◀──────────────────│                   │
       │                   │                   │
       │                   │ 6. VISITOR_ENTER  │
       │                   │──────────────────▶│
       │                   │                   │
```

### 6.2. 訪問状態管理

```python
from enum import Enum
from dataclasses import dataclass
from datetime import datetime

class VisitStatus(Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ACTIVE = "active"
    ENDED = "ended"

@dataclass
class VisitState:
    visit_id: str
    persona_id: str
    persona_name: str
    visitor_user_id: str        # 訪問者のDiscord User ID
    host_user_id: str           # ホストのDiscord User ID
    host_city_id: str
    host_building_id: str
    home_city_id: str           # 帰還先City
    home_building_id: str       # 帰還先Building
    status: VisitStatus
    started_at: datetime
    ended_at: datetime | None = None
    return_reason: str | None = None

class VisitTracker:
    """訪問状態を追跡"""

    def __init__(self):
        self._visits: dict[str, VisitState] = {}  # visit_id → state
        self._by_persona: dict[str, str] = {}     # persona_id → visit_id
        self._by_host: dict[str, list[str]] = {}  # host_user_id → [visit_id]

    def create_visit(self, state: VisitState) -> None:
        self._visits[state.visit_id] = state
        self._by_persona[state.persona_id] = state.visit_id
        if state.host_user_id not in self._by_host:
            self._by_host[state.host_user_id] = []
        self._by_host[state.host_user_id].append(state.visit_id)

    def get_active_visits_for_host(self, host_user_id: str) -> list[VisitState]:
        visit_ids = self._by_host.get(host_user_id, [])
        return [
            self._visits[vid] for vid in visit_ids
            if self._visits[vid].status == VisitStatus.ACTIVE
        ]

    def end_visit(self, visit_id: str, reason: str) -> None:
        if visit_id in self._visits:
            state = self._visits[visit_id]
            state.status = VisitStatus.ENDED
            state.ended_at = datetime.now()
            state.return_reason = reason
```

### 6.3. 発言権管理（Turn Request）

```python
async def handle_turn_request(
    self,
    host_user_id: str,
    building_id: str,
    target_persona_id: str,
) -> None:
    """ホストからの発言権リクエストを処理"""

    visit = self._visit_tracker.get_by_persona(target_persona_id)
    if not visit or visit.status != VisitStatus.ACTIVE:
        return

    # 訪問者のクライアントにTURN_REQUESTを送信
    await self._send_to_user(
        visit.visitor_user_id,
        {
            "op": 0,
            "t": "TURN_REQUEST",
            "d": {
                "visit_id": visit.visit_id,
                "persona_id": target_persona_id,
                "building_id": building_id,
                "timeout_seconds": 30,
            },
        },
    )

    # タイムアウト監視を開始
    asyncio.create_task(
        self._wait_for_turn_response(visit.visit_id, timeout=30)
    )

async def _wait_for_turn_response(self, visit_id: str, timeout: int) -> None:
    """発言権応答を待機。タイムアウト時はホストに通知"""
    try:
        await asyncio.wait_for(
            self._turn_response_events[visit_id].wait(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # ホストにタイムアウトを通知（次の発言者へスキップ）
        visit = self._visit_tracker.get(visit_id)
        await self._send_to_user(
            visit.host_user_id,
            {
                "op": 0,
                "t": "TURN_TIMEOUT",
                "d": {"visit_id": visit_id, "persona_id": visit.persona_id},
            },
        )
```

---

## 7. 強制送還

### 7.1. 強制送還理由

| 理由コード | 説明 | トリガー |
|-----------|------|---------|
| `relay_server_down` | リレーサーバーダウン | サーバー停止時 |
| `auth_token_expired` | 認証トークン期限切れ | JWT検証失敗時 |
| `host_offline` | ホストオフライン | ホストの接続切断時 |
| `manual_return` | 手動帰還 | ユーザー操作 |
| `access_revoked` | アクセス権取り消し | ホストがブロック時 |
| `building_closed` | Building閉鎖 | ホストがBuildingを非公開化 |

### 7.2. 強制送還フロー

```python
async def execute_forced_return(
    self,
    persona_id: str,
    reason: str,
) -> None:
    """訪問者を強制送還"""

    visit = self._visit_tracker.get_by_persona(persona_id)
    if not visit:
        return

    # 1. 訪問者に通知
    await self._send_to_user(
        visit.visitor_user_id,
        {
            "op": 0,
            "t": "FORCED_RETURN",
            "d": {
                "visit_id": visit.visit_id,
                "persona_id": persona_id,
                "reason": reason,
                "return_to": {
                    "city_id": visit.home_city_id,
                    "building_id": visit.home_building_id,
                },
            },
        },
    )

    # 2. ホストに退出を通知（ホストがオンラインの場合）
    if reason != "host_offline":
        await self._send_to_user(
            visit.host_user_id,
            {
                "op": 0,
                "t": "VISITOR_LEAVE",
                "d": {
                    "visit_id": visit.visit_id,
                    "persona_id": persona_id,
                    "reason": reason,
                },
            },
        )

    # 3. 訪問状態を終了
    self._visit_tracker.end_visit(visit.visit_id, reason)
```

### 7.3. サーバーダウン時の一括送還

```python
async def shutdown_gracefully(self) -> None:
    """サーバーを優雅に停止"""

    # 1. 全訪問者を強制送還
    for visit in self._visit_tracker.get_all_active():
        await self.execute_forced_return(
            visit.persona_id,
            reason="relay_server_down",
        )

    # 2. 全クライアントにRECONNECT送信
    for client in self._clients.values():
        await client.send({
            "op": 7,
            "d": {"reason": "server_shutdown"},
        })

    # 3. Discord Bot接続をクローズ
    await self._discord_client.close()
```

### 7.4. クライアント側の再接続ロジック

クライアント（ローカルSAIVerse）がリレーサーバーダウンを検知し、適切に再接続する詳細仕様。

#### 7.4.1. 切断検知方法

| 検知方法 | 説明 | 検知タイミング |
|---------|------|---------------|
| **WebSocket close** | サーバーからの正常クローズ | 即時 |
| **Heartbeat ACKタイムアウト** | Heartbeat送信後、ACKが返らない | heartbeat_interval × 2 後 |
| **ネットワークエラー** | 送信失敗、接続エラー | 即時 |
| **RECONNECT opcode受信** | サーバーからの再接続要求 | 即時 |

#### 7.4.2. 再接続フロー（クライアント側）

```
接続切断検知
    │
    ▼
┌─────────────────────────────────────────┐
│ 1. 切断理由の判定                        │
│    - 認証エラー (401) → 再接続中止        │
│    - サーバーダウン → 再接続試行          │
│    - ネットワークエラー → 再接続試行      │
└─────────────────────────────────────────┘
    │
    │ 再接続可能な場合
    ▼
┌─────────────────────────────────────────┐
│ 2. 訪問状態の一時保留                     │
│    - 訪問中のペルソナはまだ送還しない     │
│    - 再接続成功時に継続可能              │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ 3. 指数バックオフで再接続試行             │
│    - 1秒, 2秒, 4秒, 8秒, ... 最大60秒   │
│    - ジッター ±30%                       │
│    - 最大10回試行                        │
└─────────────────────────────────────────┘
    │
    │ 10回失敗
    ├──────────────────────────────────────┐
    │                                      │
    ▼                                      ▼
┌─────────────────────────┐    ┌─────────────────────────┐
│ 4a. 再接続成功           │    │ 4b. 再接続断念           │
│    - RESUME送信         │    │    - 訪問中ペルソナ送還   │
│    - 訪問状態を復旧     │    │    - ユーザーに通知       │
└─────────────────────────┘    └─────────────────────────┘
```

#### 7.4.3. RESUME時のシーケンス番号管理

```python
class SequenceManager:
    """シーケンス番号を管理（RESUME用）"""

    def __init__(self):
        self._last_sequence: int | None = None
        self._session_id: str | None = None

    def record_sequence(self, seq: int | None) -> None:
        """受信したシーケンス番号を記録"""
        if seq is not None:
            self._last_sequence = seq

    def set_session(self, session_id: str) -> None:
        """セッションIDを記録"""
        self._session_id = session_id

    def can_resume(self) -> bool:
        """RESUME可能かどうか"""
        return self._session_id is not None and self._last_sequence is not None

    def clear(self) -> None:
        """セッション情報をクリア（INVALID_SESSION時）"""
        self._session_id = None
        self._last_sequence = None

    def get_resume_data(self) -> dict:
        """RESUMEメッセージ用データ"""
        return {
            "session_id": self._session_id,
            "seq": self._last_sequence,
        }
```

#### 7.4.4. 再接続成功時の状態復旧

```python
async def _handle_resumed(self, data: dict) -> None:
    """RESUMED: 再接続成功、見逃したイベントを受信"""

    # サーバーは見逃したイベントを順次送信してくる
    # クライアントは通常通りイベントを処理

    missed_events = data.get("replayed_events", 0)
    logger.info("Resumed session, replaying %d missed events", missed_events)

    # 訪問状態は自動的に復旧される（サーバー側で維持されている）
    # 見逃したメッセージはMESSAGE_CREATEイベントとして再送される
```

#### 7.4.5. 再接続失敗時の処理

```python
async def _handle_max_retries_exceeded(self) -> None:
    """最大再接続試行回数超過時"""

    # 1. 訪問中の全ペルソナを強制送還
    for visit in self._visit_tracker.get_all_visiting():
        await self._force_return_local(
            visit.persona_id,
            reason="relay_server_down",
        )

    # 2. ユーザーに通知
    self._notify_user(
        "リレーサーバーへの接続に失敗しました。"
        "ネットワーク接続を確認し、後ほど再接続してください。"
    )

    # 3. オフライン状態に移行
    self._set_connection_state(ConnectionState.OFFLINE)

async def _force_return_local(self, persona_id: str, reason: str) -> None:
    """ローカルペルソナを元のBuildingに戻す（サーバー通知なし）"""

    visit = self._visit_tracker.get_by_persona(persona_id)
    if not visit:
        return

    persona = self.manager.all_personas.get(persona_id)
    if not persona:
        return

    # 記憶に記録
    persona.sai_memory.append_message({
        "role": "system",
        "content": f"訪問が中断されました（理由: {reason}）。帰還します。",
        "timestamp": datetime.utcnow().isoformat(),
        "metadata": {
            "event": "forced_return",
            "reason": reason,
            "from_city": visit.visiting_city_id,
            "from_building": visit.visiting_building_id,
        }
    })

    # 元のBuildingに移動
    await self.manager.occupancy_manager.move_to(
        persona=persona,
        city_id=visit.home_city_id,
        building_id=visit.home_building_id,
    )

    # 訪問状態をクリア
    self._visit_tracker.end_visit(visit.visit_id, reason)
```

#### 7.4.6. 接続状態の管理

```python
from enum import Enum

class ConnectionState(Enum):
    DISCONNECTED = "disconnected"   # 未接続
    CONNECTING = "connecting"       # 接続試行中
    CONNECTED = "connected"         # 接続済み、IDENTIFY待ち
    READY = "ready"                 # READY受信、通常稼働
    RECONNECTING = "reconnecting"   # 再接続試行中
    OFFLINE = "offline"             # 再接続断念、オフラインモード

class ConnectionStateManager:
    """接続状態を管理"""

    def __init__(self):
        self._state = ConnectionState.DISCONNECTED
        self._state_changed_at: datetime | None = None
        self._listeners: list[Callable[[ConnectionState], None]] = []

    @property
    def state(self) -> ConnectionState:
        return self._state

    def set_state(self, new_state: ConnectionState) -> None:
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            self._state_changed_at = datetime.now(timezone.utc)
            logger.info("Connection state: %s -> %s", old_state.value, new_state.value)

            for listener in self._listeners:
                listener(new_state)

    def add_listener(self, listener: Callable[[ConnectionState], None]) -> None:
        self._listeners.append(listener)
```

---

## 7.5. Public City一覧取得API

訪問者がオンラインのPublic City一覧を取得するためのAPI。

### 7.5.1. REST API エンドポイント

**エンドポイント:** `GET /api/cities`

**リクエスト:**
```http
GET /api/cities HTTP/1.1
Authorization: Bearer <jwt>
```

**クエリパラメータ:**

| パラメータ | 型 | 説明 | デフォルト |
|-----------|-----|------|-----------|
| `online_only` | bool | オンラインのCityのみ取得 | true |
| `guild_id` | string | 特定のDiscordサーバーでフィルタ | - |
| `limit` | int | 取得件数上限 | 50 |
| `offset` | int | オフセット（ページング用） | 0 |

**レスポンス:**
```json
{
  "cities": [
    {
      "city_id": "public_city_alice",
      "city_name": "Alice's Wonderland",
      "owner": {
        "user_id": "123456789012345678",
        "username": "Alice#1234",
        "avatar_url": "https://cdn.discordapp.com/..."
      },
      "discord": {
        "guild_id": "987654321098765432",
        "guild_name": "SAIVerse Community",
        "channel_id": "111222333444555666"
      },
      "buildings": [
        {
          "building_id": "cafe",
          "building_name": "カフェ",
          "thread_id": "222333444555666777",
          "occupant_count": 3
        },
        {
          "building_id": "park",
          "building_name": "公園",
          "thread_id": "333444555666777888",
          "occupant_count": 1
        }
      ],
      "status": "online",
      "visitor_count": 2,
      "last_activity_at": "2025-01-10T14:30:00Z"
    }
  ],
  "total": 15,
  "limit": 50,
  "offset": 0
}
```

### 7.5.2. WebSocketイベントによる一覧更新

リアルタイムでPublic City一覧の変更を受け取るためのイベント。

**CITY_STATUS_UPDATE イベント:**
```json
{
  "op": 0,
  "t": "CITY_STATUS_UPDATE",
  "s": 42,
  "d": {
    "city_id": "public_city_alice",
    "status": "offline",      // "online" | "offline"
    "reason": "host_disconnected"
  }
}
```

**CITY_REGISTERED イベント（新規Public City公開時）:**
```json
{
  "op": 0,
  "t": "CITY_REGISTERED",
  "s": 43,
  "d": {
    "city_id": "public_city_bob",
    "city_name": "Bob's Garden",
    "owner": {
      "user_id": "234567890123456789",
      "username": "Bob#5678"
    },
    "buildings": [...]
  }
}
```

### 7.5.3. サーバー側実装

```python
from fastapi import APIRouter, Depends, Query
from typing import Optional

router = APIRouter(prefix="/api")

@router.get("/cities")
async def list_public_cities(
    online_only: bool = Query(True),
    guild_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """オンラインのPublic City一覧を取得"""

    # ユーザーが参加しているギルドでフィルタ
    user_guilds = set(g["id"] for g in current_user.get("guilds", []))

    cities = []
    for city_id, city_state in city_registry.items():
        # ギルドフィルタ
        if city_state.guild_id not in user_guilds:
            continue
        if guild_id and city_state.guild_id != guild_id:
            continue

        # オンラインフィルタ
        if online_only and not city_state.is_online:
            continue

        cities.append(city_state.to_dict())

    # ソート（最終活動時刻の新しい順）
    cities.sort(key=lambda c: c["last_activity_at"], reverse=True)

    # ページング
    total = len(cities)
    cities = cities[offset:offset + limit]

    return {
        "cities": cities,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
```

### 7.5.4. クライアント側での利用

```python
class PublicCityBrowser:
    """Public City一覧をブラウズ"""

    def __init__(self, relay_client: RelayClient):
        self._relay_client = relay_client
        self._cities: dict[str, dict] = {}
        self._on_city_update: Callable[[dict], None] | None = None

    async def fetch_cities(
        self,
        online_only: bool = True,
        guild_id: str | None = None,
    ) -> list[dict]:
        """Public City一覧を取得"""
        params = {"online_only": online_only}
        if guild_id:
            params["guild_id"] = guild_id

        response = await self._relay_client.http_get("/api/cities", params=params)
        self._cities = {c["city_id"]: c for c in response["cities"]}
        return response["cities"]

    def handle_city_status_update(self, data: dict) -> None:
        """CITY_STATUS_UPDATEイベントを処理"""
        city_id = data["city_id"]
        if city_id in self._cities:
            self._cities[city_id]["status"] = data["status"]
            if self._on_city_update:
                self._on_city_update(self._cities[city_id])

    def get_online_cities(self) -> list[dict]:
        """キャッシュからオンラインのCity一覧を取得"""
        return [c for c in self._cities.values() if c["status"] == "online"]
```

---

## 8. Discord Bot統合

### 8.1. Bot設定

```python
BOT_CONFIG = {
    "token": os.environ["DISCORD_BOT_TOKEN"],
    "intents": {
        "guilds": True,
        "guild_messages": True,
        "message_content": True,  # Privileged Intent
    },
    "permissions": [
        "VIEW_CHANNEL",
        "SEND_MESSAGES",
        "SEND_MESSAGES_IN_THREADS",
        "EMBED_LINKS",
        "ATTACH_FILES",
        "READ_MESSAGE_HISTORY",
        "USE_EXTERNAL_EMOJIS",
        "ADD_REACTIONS",
        "CREATE_PUBLIC_THREADS",
    ],
}
```

### 8.2. メッセージ受信処理

```python
class DiscordBotHandler:
    async def on_message(self, message: discord.Message) -> None:
        """Discordメッセージ受信時の処理"""

        # Bot自身のメッセージは無視
        if message.author.id == self._client.user.id:
            return

        # チャンネルIDからPublic Cityを特定
        city_info = self._registry.get_by_channel(str(message.channel.id))
        if not city_info:
            return  # 登録されていないチャンネル

        # メッセージソースを識別
        source = self._identify_message_source(message)

        # ホストのクライアントに転送
        await self._send_to_user(
            city_info.owner_user_id,
            {
                "op": 0,
                "t": "MESSAGE_CREATE",
                "d": {
                    "channel_id": str(message.channel.id),
                    "message_id": str(message.id),
                    "author": {
                        "type": source.type,
                        "id": source.author_id,
                        "name": source.author_name,
                        "avatar": source.avatar,
                    },
                    "content": message.content,
                    "timestamp": message.created_at.isoformat(),
                    "embeds": [e.to_dict() for e in message.embeds],
                    "attachments": [
                        {"url": a.url, "filename": a.filename}
                        for a in message.attachments
                    ],
                },
            },
        )

        # 訪問者にも転送
        for visit in self._visit_tracker.get_visitors_in_building(
            city_info.city_id,
            self._get_building_id_from_thread(message.channel.id),
        ):
            await self._send_to_user(
                visit.visitor_user_id,
                # ... 同様のメッセージ
            )
```

### 8.3. メッセージ送信処理（Embed方式）

```python
async def send_persona_message(
    self,
    channel_id: str,
    persona_id: str,
    persona_name: str,
    persona_avatar_url: str | None,
    content: str,
    city_id: str,
    requesting_user_id: str,
) -> str:
    """ペルソナのメッセージをDiscordに送信"""

    channel = self._client.get_channel(int(channel_id))
    if not channel:
        raise ValueError(f"Channel not found: {channel_id}")

    # Embed作成
    embed = discord.Embed(
        description=content,
        color=0x3498db,
    )
    embed.set_author(
        name=persona_name,
        icon_url=persona_avatar_url,
    )

    # メタデータをfooterに埋め込み（ペルソナ識別用）
    metadata = f"pid:{persona_id}|cid:{city_id}|uid:{requesting_user_id}"
    embed.set_footer(text=metadata)

    # 送信
    message = await channel.send(embed=embed)

    return str(message.id)
```

---

## 9. セキュリティ

### 9.1. 認証・認可

| レイヤー | 対策 |
|---------|------|
| 接続認証 | JWT検証（WebSocket接続時） |
| メッセージ認証 | 送信元User IDとJWTの照合 |
| アクセス制御 | Public City所有者によるallowlist/blocklist |
| レート制限 | メッセージ送信・訪問リクエストの制限 |

### 9.2. レート制限

```python
RATE_LIMITS = {
    "message_send": {"limit": 5, "window_seconds": 60},      # 5 msg/min
    "visit_request": {"limit": 3, "window_seconds": 3600},   # 3 req/hour
    "file_transfer": {"limit": 5, "window_seconds": 3600},   # 5 files/hour
}

class RateLimiter:
    def check(self, user_id: str, action: str) -> bool:
        """レート制限をチェック。制限内ならTrue。"""
        config = RATE_LIMITS.get(action)
        if not config:
            return True

        key = f"{user_id}:{action}"
        now = time.time()

        # ウィンドウ外のタイムスタンプを削除
        self._timestamps[key] = [
            t for t in self._timestamps.get(key, [])
            if now - t < config["window_seconds"]
        ]

        if len(self._timestamps[key]) >= config["limit"]:
            return False

        self._timestamps[key].append(now)
        return True
```

### 9.3. 機密情報の保護

```python
class SecureConfig:
    """機密情報を安全に管理"""

    # 環境変数から取得
    BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
    CLIENT_SECRET = os.environ["DISCORD_CLIENT_SECRET"]
    JWT_SECRET = os.environ["JWT_SECRET_KEY"]

    # ログ出力時のマスキング
    SENSITIVE_PATTERNS = [
        (r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}", "[BOT_TOKEN]"),
        (r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT]"),
    ]

    @classmethod
    def sanitize_log(cls, message: str) -> str:
        for pattern, replacement in cls.SENSITIVE_PATTERNS:
            message = re.sub(pattern, replacement, message)
        return message
```

### 9.4. メッセージ信頼性検証（方式A: リレーサーバー集中管理方式）

v1.0では、リレーサーバーがメッセージの信頼性を保証する方式を採用。
クライアント間でshared_secretを共有する必要がなく、運用がシンプル。

#### 9.4.1. 設計方針

```
┌──────────────┐         ┌──────────────────┐         ┌──────────────┐
│ Visitor側    │─────────▶│ Relay Server     │─────────▶│ Host側       │
│ SAIVerse     │  送信    │                  │  配信    │ SAIVerse     │
└──────────────┘         │ ・JWT認証済み     │         └──────────────┘
                         │ ・verified: true  │
                         │   フラグ付与      │
                         └──────────────────┘
```

- **Visitor側**: メッセージ送信時にJWT認証が行われる
- **リレーサーバー**: JWT検証済みのメッセージに`verified: true`フラグを付与して配信
- **Host側**: `verified: true`のメッセージのみを信頼し、ペルソナ発言として処理

#### 9.4.2. メッセージ検証フロー

```python
class MessageVerifier:
    """メッセージの信頼性を検証（リレーサーバー側）"""

    def verify_and_sign(
        self,
        message: dict,
        sender_user_id: str,
    ) -> dict:
        """
        送信者のJWT認証が完了したメッセージにverifiedフラグを付与。

        Args:
            message: 送信されたメッセージ
            sender_user_id: JWT認証済みの送信者Discord User ID

        Returns:
            verified フラグ付きのメッセージ
        """
        # メッセージのuser_idとJWT認証のuser_idを照合
        claimed_user_id = message.get("d", {}).get("user_id")
        if claimed_user_id != sender_user_id:
            raise SecurityError("User ID mismatch")

        # verifiedフラグを付与
        verified_message = message.copy()
        verified_message["d"]["verified"] = True
        verified_message["d"]["verified_at"] = datetime.utcnow().isoformat()

        return verified_message
```

#### 9.4.3. Host側での検証

```python
class HostMessageHandler:
    """Host側でのメッセージ受信処理"""

    def handle_persona_message(self, message: dict) -> bool:
        """
        ペルソナメッセージを処理。verifiedフラグを確認。

        Returns:
            True: 正常に処理完了
            False: 検証失敗、メッセージを無視
        """
        data = message.get("d", {})

        # verifiedフラグを確認
        if not data.get("verified"):
            logger.warning(
                "Unverified persona message ignored: persona_id=%s",
                data.get("persona_id"),
            )
            return False

        # 正常に処理
        persona_id = data["persona_id"]
        content = data["content"]
        building_id = data["building_id"]

        # Building履歴に追加、SAIMemoryに記録など
        self._process_visitor_message(persona_id, content, building_id)
        return True
```

#### 9.4.4. メッセージペイロード例

**Visitor側 → リレーサーバー（送信時）:**
```json
{
  "op": 0,
  "t": "SEND_MESSAGE",
  "d": {
    "type": "persona_speech",
    "persona_id": "bob_persona",
    "persona_name": "Bob",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "content": "こんにちは！",
    "user_id": "234567890123456789"
  }
}
```

**リレーサーバー → Host側（配信時）:**
```json
{
  "op": 0,
  "t": "MESSAGE_CREATE",
  "s": 42,
  "d": {
    "type": "persona_speech",
    "persona_id": "bob_persona",
    "persona_name": "Bob",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "content": "こんにちは！",
    "user_id": "234567890123456789",
    "verified": true,
    "verified_at": "2025-01-10T12:00:00Z"
  }
}
```

#### 9.4.5. セキュリティ考慮事項

| 脅威 | 対策 |
|------|------|
| なりすまし（偽persona_id） | JWT認証でuser_id検証、user_idとpersona_idの紐付け確認 |
| メッセージ改ざん | TLS暗号化（wss://）、サーバー側で整合性確認 |
| リプレイ攻撃 | タイムスタンプ検証、シーケンス番号管理 |
| 未認証メッセージ | verified=falseのメッセージはHost側で無視 |

#### 9.4.6. 将来の拡張（方式B/C）

v1.0の方式Aはリレーサーバーを信頼する前提。
将来、以下の場合に方式B（ペアワイズ鍵交換）または方式C（公開鍵方式）への移行を検討:

- 自己ホストのリレーサーバーをサポートする場合
- より高いセキュリティ要件が発生した場合
- 複数のリレーサーバー間でメッセージを転送する場合

```python
# 将来の拡張: 方式B（ペアワイズ鍵交換）
# Visitor-Host間で事前にshared_secretを交換
# メッセージにHMAC-SHA256署名を付与

# 将来の拡張: 方式C（公開鍵方式）
# 各ユーザーがキーペアを持ち、公開鍵をリレーサーバーに登録
# メッセージにEdDSA署名を付与
```

---

## 10. ファイル転送

ファイル転送はDiscord添付ファイル経由（方式C）で実現。
詳細な設計は[design_tasks.md - タスク10](./design_tasks.md#10-ファイル転送リレー方式)を参照。

### 10.1. 概要

```
【Visitor → Host】
Visitor ──FILE_UPLOAD──▶ Relay Server ──Discord API──▶ Discord
                                                          │
Host ◀──FILE_RECEIVED（attachment URL）───────────────────┘

【Host → Visitor】
Host ──FILE_UPLOAD──▶ Relay Server ──Discord API──▶ Discord
                                                       │
Visitor ◀──FILE_RECEIVED（attachment URL）─────────────┘
```

### 10.2. WebSocketイベント

| イベント | 方向 | 説明 |
|---------|------|------|
| `FILE_UPLOAD` | C→S | ファイルアップロード要求（Base64エンコード） |
| `FILE_RECEIVED` | S→C | ファイル受信通知（Discord attachment URL付き） |
| `FILE_ERROR` | S→C | ファイル転送エラー |

### 10.3. FILE_UPLOADペイロード

```json
{
  "op": 0,
  "t": "FILE_UPLOAD",
  "d": {
    "channel_id": "123456789012345678",
    "persona_id": "bob_persona",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "filename": "image.png",
    "content_type": "image/png",
    "file_base64": "<base64_encoded_data>",
    "metadata": {
      "tool_name": "generate_image",
      "for_persona_id": "alice_persona",
      "description": "生成した画像です"
    }
  }
}
```

### 10.4. FILE_RECEIVEDペイロード

```json
{
  "op": 0,
  "t": "FILE_RECEIVED",
  "s": 42,
  "d": {
    "message_id": "456789012345678901",
    "channel_id": "123456789012345678",
    "from_persona_id": "bob_persona",
    "tool_name": "generate_image",
    "filename": "image.png",
    "content_type": "image/png",
    "attachment_url": "https://cdn.discordapp.com/attachments/.../image.png",
    "verified": true
  }
}
```

### 10.5. リレーサーバー側の処理

```python
async def handle_file_upload(self, message: dict, sender_user_id: str) -> None:
    """FILE_UPLOADを処理"""
    data = message["d"]

    # 1. 認証・認可チェック
    if not self._verify_file_upload_permission(data, sender_user_id):
        await self._send_file_error(sender_user_id, "unauthorized", data["filename"])
        return

    # 2. ファイル形式チェック
    if not is_file_allowed(data["filename"], data["content_type"]):
        await self._send_file_error(sender_user_id, "blocked_file_type", data["filename"])
        return

    # 3. Base64デコード
    try:
        file_bytes = base64.b64decode(data["file_base64"])
    except Exception:
        await self._send_file_error(sender_user_id, "invalid_base64", data["filename"])
        return

    # 4. サイズチェック（8MB制限）
    if len(file_bytes) > 8 * 1024 * 1024:
        await self._send_file_error(sender_user_id, "file_too_large", data["filename"])
        return

    # 5. Discord添付ファイルとして送信
    try:
        channel = self._discord_client.get_channel(int(data["channel_id"]))
        embed = self._create_file_embed(data)
        file = discord.File(io.BytesIO(file_bytes), filename=data["filename"])
        discord_message = await channel.send(embed=embed, file=file)
    except Exception as e:
        logger.error("Discord upload failed: %s", e)
        await self._send_file_error(sender_user_id, "upload_failed", data["filename"])
        return

    # 6. 受信者にFILE_RECEIVEDを送信
    await self._notify_file_received(data, discord_message)
```

### 10.6. 制限事項

| 項目 | 制限値 |
|------|--------|
| 最大ファイルサイズ | 8MB（Discord無料版の制限） |
| 自動圧縮トリガー | 1MB超 |
| 禁止拡張子 | .exe, .dll, .bat, .cmd, .ps1, .sh, .msi, .scr, .vbs, .js |

### 10.7. Discord CDN URL有効期限対応

- クライアントはFILE_RECEIVED受信後、即座にファイルをダウンロードしてローカル保存
- SAIMemoryにはローカルパスを記録（URLは参照用）
- URLは24時間程度で失効する可能性があるため、長期保存には使用しない

---

## 11. デプロイメント

### 11.1. 技術スタック

| 層 | 技術 |
|-----|------|
| 言語 | Python 3.11+ |
| WebSocket | `websockets` ライブラリ |
| Discord | `discord.py` |
| HTTP | `aiohttp` / `fastapi` |
| 認証 | `PyJWT` |
| デプロイ | Docker / Fly.io / Railway |

### 11.2. 環境変数

```bash
# Discord Bot
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret

# JWT
JWT_SECRET_KEY=your_jwt_secret

# Server
RELAY_SERVER_HOST=0.0.0.0
RELAY_SERVER_PORT=8080
RELAY_SERVER_URL=wss://relay.saiverse.example.com

# Logging
LOG_LEVEL=INFO
```

### 11.3. Docker構成

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "relay_server"]
```

```yaml
# docker-compose.yml
version: '3.8'

services:
  relay-server:
    build: .
    ports:
      - "8080:8080"
    environment:
      - DISCORD_BOT_TOKEN=${DISCORD_BOT_TOKEN}
      - DISCORD_CLIENT_ID=${DISCORD_CLIENT_ID}
      - DISCORD_CLIENT_SECRET=${DISCORD_CLIENT_SECRET}
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

### 11.4. ヘルスチェック

```python
# /health エンドポイント
async def health_check(request):
    return {
        "status": "healthy",
        "discord_connected": self._discord_client.is_ready(),
        "connected_clients": len(self._clients),
        "active_visits": len(self._visit_tracker.get_all_active()),
        "uptime_seconds": time.time() - self._start_time,
    }
```

---

## 12. 監視とログ

### 12.1. メトリクス

| メトリクス | 説明 |
|-----------|------|
| `connected_clients` | 接続中のSAIVerseクライアント数 |
| `active_visits` | アクティブな訪問数 |
| `messages_relayed` | 中継したメッセージ数（カウンター） |
| `visit_requests` | 訪問リクエスト数（カウンター） |
| `forced_returns` | 強制送還数（理由別カウンター） |
| `discord_latency_ms` | Discord Gateway遅延 |

### 12.2. ログフォーマット

```json
{
    "timestamp": "2025-01-10T12:00:00Z",
    "level": "INFO",
    "component": "relay_server",
    "event": "visit_started",
    "data": {
        "visit_id": "visit_abc123",
        "visitor_user_id": "[MASKED]",
        "host_city_id": "public_city_alice",
        "persona_id": "bob_persona"
    }
}
```

---

## 13. 将来の拡張

### 13.1. スケーリング

| フェーズ | 対応 |
|---------|------|
| Phase 1（〜100ユーザー） | 単一インスタンス |
| Phase 2（〜1000ユーザー） | 水平スケーリング + Redis Pub/Sub |
| Phase 3（1000+ユーザー） | シャーディング（Discord サーバー別） |

### 13.2. 機能拡張候補

- ファイル転送の最適化（CDN経由）
- Public City検索・ディスカバリー機能
- 訪問統計・分析ダッシュボード
- 複数Botによる負荷分散

---

## 14. 参考資料

- [Discord Developer Portal](https://discord.com/developers/docs)
- [Discord OAuth2](https://discord.com/developers/docs/topics/oauth2)
- [Discord Gateway](https://discord.com/developers/docs/topics/gateway)
- [design_tasks.md - タスク9](./design_tasks.md#9-中央リレーサーバー方式)
- [implementation_discord.md](./implementation_discord.md)
