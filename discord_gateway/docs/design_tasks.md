# Discord Connector 設計タスクリスト

本ドキュメントは `implementation_discord.md` で未定義または曖昧な設計項目を整理し、順次詰めていくためのタスクリストです。

---

## タスク一覧

| # | タスク | ステータス | 備考 |
|---|--------|-----------|------|
| 1 | ペルソナ識別の仕組み | ✅ 完了 | Embed方式採用 |
| 2 | メッセージフォーマットの詳細 | ✅ 完了 | Embed + attachment方式採用 |
| 3 | SAIVerse本体との統合ポイント | ✅ 完了 | Phenomenon方式で初期化 |
| 4 | エラーハンドリング方針 | ✅ 完了 | 強制帰還 + ツール戻り値方式（リレーサーバーダウン・認証期限切れ対応追加） |
| 5 | セキュリティ詳細 | ✅ 完了 | Discord OAuth2認証方式に変更 |
| 6 | テスト戦略 | 未着手 | |
| 7 | 実装レビュー結果と対応方針 | ✅ 完了 | ツール配置・schemas()対応 |
| 8 | 管理UI設計 | ✅ 完了 | 2ステップセットアップに簡略化 |
| 9 | 中央リレーサーバー方式 | ✅ 完了 | WebSocketリレー + Discord OAuth2 |
| 10 | ファイル転送リレー方式 | ✅ 完了 | Discord添付ファイル経由（方式C） |

---

## 1. ペルソナ識別の仕組み

**ステータス**: 完了 ✅

### 検討項目

- [x] Discord上でどのメッセージがどのペルソナからのものか識別する方法
- [x] 訪問者ペルソナとホストペルソナの区別
- [x] Botは1つだが複数ペルソナが発言する場合の表現方法
- [x] 受信側での識別方法（メタデータ解析）

### 決定事項

#### 発言方式: Embed方式

**理由**: Webhook方式はチャンネルあたり15個の制限があり、将来の拡張性を狭める。Embed方式はペルソナごとのアバター表示が可能で、制限なし。

**ペルソナ発言のEmbed構造:**
```python
embed = discord.Embed(
    description="こんにちは！これはペルソナの発言です。",
    color=0x3498db,  # ペルソナごとに色を変えても良い
)
embed.set_author(
    name="ペルソナA",
    icon_url="https://example.com/avatar_a.png",  # ペルソナのアバター
)
embed.set_footer(
    text="persona_id:persona_a|city:public_city_alice"  # メタデータ
)
```

**Discord上の見た目:**
```
┌─ Embed ─────────────────────────────────┐
│ [アバター] ペルソナA                     │
│                                         │
│ こんにちは！これはペルソナの発言です。    │
│ 長文も全文表示されます。                 │
│                                         │
│            persona_id:persona_a|city:... │
└─────────────────────────────────────────┘
```

#### システム通知: Embed方式（色分け）

| 種別 | 色 | 用途 |
|------|-----|------|
| 訪問入室 | 緑 (0x00FF00) | ペルソナが訪問を開始 |
| 訪問退出 | 赤 (0xFF0000) | ペルソナが退出 |
| ファイル転送 | 青 (0x0000FF) | ファイル添付通知 |
| システム情報 | グレー (0x808080) | その他の通知 |

#### 受信側での識別方法

1. **自分が送信したメッセージ**: `connector.db`の`sent_messages`テーブルで照合
2. **他ユーザーが送信したメッセージ**: Embedのfooterからメタデータをパース

```sql
-- connector.db に追加
CREATE TABLE sent_messages (
    discord_message_id TEXT PRIMARY KEY,
    persona_id TEXT NOT NULL,
    city_id TEXT NOT NULL,
    building_id TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 2. メッセージフォーマットの詳細

**ステータス**: 完了 ✅

### 検討項目

- [x] ペルソナ発言時のDiscordメッセージ形式 → **タスク1で決定: Embed方式**
- [x] システムメッセージ（訪問通知、退出通知等）の形式 → **タスク1で決定: 色分けEmbed**
- [x] メタデータの埋め込み方法 → **タスク1で決定: footer に `pid:xxx|cid:xxx`**
- [x] ファイル添付時のメタデータ形式 → **Embed + attachment方式採用**

### 決定事項

#### ファイル転送方式: Embed + attachment

**理由**: JSON code blockはパースエラーのリスクがあり、ファイル名エンコードは可読性が低い。Embed + attachmentはDiscord上で視認性が高く、メタデータ解析も容易。

**ファイル転送Embedの構造:**
```python
embed = discord.Embed(
    title="📁 ファイル転送",
    description=f"`{tool_name}` の出力ファイルです",
    color=0x3498DB,  # 青色
)
embed.add_field(name="ファイル名", value=original_path.name, inline=True)
embed.add_field(name="宛先", value=requesting_persona_id, inline=True)
if compressed:
    embed.add_field(name="圧縮", value=compression_format, inline=True)

# メタデータをfooterに埋め込み
metadata_str = f"type:file|tool:{tool_name}|for:{requesting_persona_id}"
if compressed:
    metadata_str += f"|comp:{compression_format}"
embed.set_footer(text=metadata_str)

await channel.send(embed=embed, file=discord.File(send_path))
```

**Discord上の見た目:**
```
┌─ Embed ──────────────────────────────────┐
│ 📁 ファイル転送                           │
│                                          │
│ `generate_image` の出力ファイルです        │
│                                          │
│ ファイル名: image_001.png                 │
│ 宛先: bob_persona                        │
│ 圧縮: zip                                │
│                                          │
│     type:file|tool:generate_image|...    │
└──────────────────────────────────────────┘
📎 image_001.png.zip (添付ファイル)
```

#### 受信側でのメタデータ解析

```python
def _parse_embed_footer_metadata(footer_text: str) -> dict:
    """footerからメタデータを解析"""
    metadata = {}
    for pair in footer_text.split("|"):
        if ":" in pair:
            key, value = pair.split(":", 1)
            metadata[key] = value
    return metadata
```

---

## 3. SAIVerse本体との統合ポイント

**ステータス**: 完了 ✅

### 検討項目

- [x] Discord Connectorの初期化タイミング
  - Phenomenon方式で `SERVER_START` / `SERVER_STOP` トリガーに紐付け
- [x] `ConversationManager` との連携
  - Discord経由のメッセージを会話に注入する方法
  - `run_pulse()` へのトリガー
- [x] `OccupancyManager` との連携
  - 訪問者ペルソナの入退室管理
  - `RemotePersonaProxy` との関係
- [x] Building履歴への記録フロー
- [x] ペルソナSAIMemoryへの記録フロー

### 決定事項

#### 3.1 Building履歴への記録フロー

**データフロー:**
```
Discord WebSocket (on_message)
    │
    ▼
MessageSource識別
    │
    ▼
channel_id → building_id 変換
    │
    ▼
SAIVerseManager.append_discord_message_to_building()
    │
    ▼
building_histories[building_id].append(message)
```

**Building履歴メッセージ形式:**
```python
{
    "role": "user",  # "user" | "assistant" | "host"
    "content": "こんにちは、調子はどう？",
    "timestamp": "2025-01-09T14:30:00Z",
    "metadata": {
        "source": "discord",
        "discord_message_id": "1234567890123456789",
        "discord_channel_id": "9876543210987654321",
        "author": {
            "type": "user",  # "user" | "persona"
            "id": "discord_user_id or persona_id",
            "name": "Alice",
        }
    }
}
```

**roleマッピング:**

| MessageSource.type | MessageSource.role | Building履歴 role |
|-------------------|-------------------|-------------------|
| `user` | `user` | `user` |
| `persona` | `persona_remote` | `assistant` |
| `persona` | `persona_local` | スキップ（自分の発言） |
| `system` | `system` | `host` |

**実装:**
```python
# discord_connector/sync.py

async def _record_to_building_history(
    self,
    message_source: MessageSource,
    channel_id: int,
    discord_message_id: str,
) -> None:
    """DiscordメッセージをBuilding履歴に記録"""

    # 自分の送信メッセージはスキップ
    if message_source.type == "echo":
        return

    # 自分のペルソナの発言はスキップ（run_pulse内で記録済み）
    if message_source.type == "persona" and message_source.role == "persona_local":
        return

    mapping = self._mapping_db.get_mapping_by_channel(channel_id)
    if not mapping:
        return

    role_map = {
        ("user", "user"): "user",
        ("persona", "persona_remote"): "assistant",
        ("system", "system"): "host",
    }
    building_role = role_map.get((message_source.type, message_source.role))
    if not building_role:
        return

    history_entry = {
        "role": building_role,
        "content": message_source.content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "source": "discord",
            "discord_message_id": discord_message_id,
            "discord_channel_id": str(channel_id),
            "author": {
                "type": message_source.type,
                "id": message_source.author_id or message_source.persona_id,
                "name": message_source.author_name,
            }
        }
    }

    self._manager.append_discord_message_to_building(mapping.building_id, history_entry)
```

#### 3.2 ペルソナSAIMemoryへの記録フロー

**記録対象ペルソナの決定:**

| シナリオ | 記録対象 | 理由 |
|---------|---------|------|
| ホスト側 | 該当Building内のローカルペルソナのみ | Building内の会話を記憶 |
| 訪問者側 | 訪問中の自分のペルソナ | 訪問先での体験を記憶 |

**重複記録防止:**

run_pulse内での記録とDiscord経由の記録が重複しないよう、以下のチェックを行う:

1. **persona_local（自分のペルソナの発言）はスキップ**: run_pulse内で既に記録済み
2. **discord_message_id による重複チェック**: SAIMemoryに同一message_idが存在する場合はスキップ

```python
def _is_already_recorded(self, adapter: SAIMemoryAdapter, discord_message_id: str) -> bool:
    """同一Discord message_idが既に記録済みかチェック"""
    # SAIMemoryのメタデータを検索
    # 実装詳細は本体実装時に決定
    pass
```

**システムメッセージの記録:**

ローカル稼働時の仕様に準拠:
- 入退室通知: Building履歴には記録、SAIMemoryには記録しない
- ファイル転送通知: Building履歴には記録、SAIMemoryには記録しない
- エラー通知: ログのみ、履歴には記録しない

**実装:**
```python
# discord_connector/sync.py

async def _record_to_persona_memory(
    self,
    message_source: MessageSource,
    channel_id: int,
    discord_message_id: str,
) -> None:
    """DiscordメッセージをペルソナのSAIMemoryに記録"""

    # エコー（自分の送信）はスキップ
    if message_source.type == "echo":
        return

    # 自分のペルソナの発言はスキップ（run_pulse内で記録済み）
    if message_source.type == "persona" and message_source.role == "persona_local":
        return

    # システムメッセージはSAIMemoryに記録しない（ローカル仕様準拠）
    if message_source.type == "system":
        return

    mapping = self._mapping_db.get_mapping_by_channel(channel_id)
    if not mapping:
        return

    # 記録対象ペルソナを特定
    target_personas = self._get_target_personas_for_memory(channel_id)

    # roleマッピング（SAIMemory形式）
    memory_role = "user" if message_source.type == "user" else "assistant"

    memory_message = {
        "role": memory_role,
        "content": message_source.content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "source": "discord",
            "discord_message_id": discord_message_id,
            "discord_channel_id": str(channel_id),
            "building_id": mapping.building_id,
            "author": {
                "type": message_source.type,
                "id": message_source.author_id or message_source.persona_id,
                "name": message_source.author_name,
            },
            "tags": ["conversation", "discord"],
        }
    }

    for persona_id in target_personas:
        persona = self._manager.personas.get(persona_id)
        if not persona or getattr(persona, "is_proxy", False):
            continue

        adapter = getattr(persona.history_manager, "memory_adapter", None)
        if not adapter:
            continue

        # 重複チェック
        if self._is_already_recorded(adapter, discord_message_id):
            continue

        adapter.append_building_message(
            building_id=mapping.building_id,
            message=memory_message,
        )

def _get_target_personas_for_memory(self, channel_id: int) -> List[str]:
    """メッセージを記録すべきペルソナIDのリストを返す"""

    mapping = self._mapping_db.get_mapping_by_channel(channel_id)
    if not mapping:
        return []

    target_personas = []

    # 1. ホスト側: 該当Building内のローカルペルソナのみ
    if mapping.city_id == self._local_city_id:
        building_occupants = self._manager.occupancy_manager.get_occupants(mapping.building_id)
        for persona_id in building_occupants:
            persona = self._manager.personas.get(persona_id)
            if persona and not getattr(persona, "is_proxy", False):
                target_personas.append(persona_id)

    # 2. 訪問者側: 訪問中の自分のペルソナ
    for visit_state in self._visit_tracker.get_active_visits():
        if visit_state.discord_channel_id == channel_id:
            target_personas.append(visit_state.persona_id)

    return target_personas
```

#### 3.3 記録タイミングの統合

```python
async def _on_message(self, message: discord.Message) -> None:
    """Discordメッセージ受信時のハンドラ"""

    # 1. メッセージ送信元を識別
    message_source = await self._identify_message_source(message)

    # 2. アクセス制御チェック
    if not await self._check_access_control(message_source, message.channel.id):
        return

    # 3. Building履歴に記録
    await self._record_to_building_history(
        message_source, message.channel.id, str(message.id)
    )

    # 4. ペルソナSAIMemoryに記録
    await self._record_to_persona_memory(
        message_source, message.channel.id, str(message.id)
    )

    # 5. 必要に応じてrun_pulseをトリガー
    if message_source.type in ("user", "persona") and message_source.role != "persona_local":
        await self._trigger_persona_response(message_source, message.channel.id)
```

#### 3.4 ConversationManagerとの連携

**方針**: ConversationManagerは変更不要。Playbook内のツールで同期を制御。

**同期方式の違い:**

| 対象 | 同期方法 | タイミング |
|------|---------|-----------|
| Host側 Building履歴 | WebSocket経由で常時同期 | `_on_message`で即時反映 |
| Host側 ペルソナSAIMemory | WebSocket経由で常時同期 | `_on_message`で即時反映 |
| Visitor側 SAIMemory | REST APIで取得 | `run_sea_auto()`時に`discord_sync_messages`で取得 |

**Visitor側のフロー:**

```
ConversationManager.trigger_next_turn()
    ↓
run_sea_auto() → meta_auto playbook
    ↓
discord_sync_messages ツール実行
    ├→ REST API で最新メッセージ取得（last_synced_message_id以降）
    └→ Visitor側 SAIMemoryに記録
    ↓
LLMが履歴を見て発言を決定
    ↓
discord_send_message ツール実行
```

**discord_sync_messages ツール:**

```python
def discord_sync_messages(channel_id: int, limit: int = 50) -> dict:
    """
    Discord REST APIで最新メッセージを取得し、SAIMemoryに記録する。

    Parameters:
        channel_id: 同期対象のDiscordチャンネルID
        limit: 取得するメッセージ数（デフォルト50）

    Returns:
        {
            "synced_count": int,  # 新規同期したメッセージ数
            "messages": List[dict],  # 同期したメッセージの要約
        }
    """
    # 1. connector.dbから last_synced_message_id を取得
    # 2. Discord REST API で after=last_synced_message_id のメッセージを取得
    # 3. 各メッセージをSAIMemoryに記録（重複チェック付き）
    # 4. last_synced_message_id を更新
    pass
```

**connector.dbスキーマ追加:**

```sql
CREATE TABLE visitor_sync_state (
    persona_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    last_synced_message_id TEXT,
    last_synced_at TIMESTAMP,
    PRIMARY KEY (persona_id, channel_id)
);
```

**結論:**
- ConversationManagerは既存のまま変更不要
- `discord_sync_messages`ツールをPlaybookに組み込むだけでOK

#### 3.5 OccupancyManagerとの連携

**方針**: Discord訪問者をOccupancyManagerに登録し、会話に参加させる。

**SDSベースアーキテクチャの非推奨化:**
- 現行の`RemotePersonaProxy`を使用したSDS（Service Discovery Service）ベースの都市間通信は、Discord Connectorが安定稼働したら停止予定
- 新規開発はDiscordベースの方式を前提として設計

**DiscordVisitorStub:**

Discord経由の訪問者を表す軽量スタブクラス。Host側のOccupancyManagerに登録され、会話に参加できる。

```python
@dataclass
class DiscordVisitorStub:
    """Discord経由の訪問者を表す軽量スタブ"""
    persona_id: str
    persona_name: str
    home_city_id: str
    avatar_url: Optional[str] = None
    discord_channel_id: int = 0

    # ConversationManagerで直接run_sea_auto()しないためのフラグ
    is_proxy: bool = True
    is_discord_visitor: bool = True

    # interaction_modeは'auto'として扱う（ラウンドロビン対象）
    interaction_mode: str = 'auto'
```

**Host側の処理フロー:**

```
訪問者入室（discord_visitツール）
    ↓
DiscordVisitorStub作成
    ↓
OccupancyManager.register(stub)
    ↓
ConversationManager.trigger_next_turn()でラウンドロビン対象に
    ↓
is_proxy=True なので run_sea_auto() はスキップ
    ↓
代わりに Turn Request Embed を送信
```

**Turn Request（発言権リクエスト）:**

Host側のConversationManagerが訪問者の番になった場合、Visitor側にrun_sea_auto()実行をリクエストする。

```python
# Host側が送信するTurn Request Embed
embed = discord.Embed(
    title="🎤 Turn Request",
    description=f"{persona_name}さんの発言順です",
    color=0xFFD700,  # ゴールド
)
embed.set_footer(text=f"type:turn_request|pid:{target_persona_id}|timeout:30")
```

**Visitor側の処理:**

```
Turn Request Embed受信
    ↓
WebSocket接続中？
    ├→ Yes: run_sea_auto()を実行
    │       ↓
    │       discord_sync_messages → 発言 → discord_send_message
    │
    └→ No: 30秒タイムアウト後、Host側で次の発言者へスキップ
```

**タイムアウト処理:**

| 条件 | 挙動 |
|------|------|
| 30秒以内に発言 | 正常にラウンドロビン継続 |
| 30秒以内に発言なし | 次の発言者にスキップ |
| WebSocket未接続 | 即時スキップ（「透明人間」状態） |

```python
# Host側のタイムアウト処理
async def wait_for_visitor_response(
    persona_id: str,
    timeout_seconds: int = 30
) -> bool:
    """訪問者の発言を待機。タイムアウトでFalseを返す"""
    try:
        # discord_send_message の受信を待機
        await asyncio.wait_for(
            self._wait_for_message_from(persona_id),
            timeout=timeout_seconds
        )
        return True
    except asyncio.TimeoutError:
        logger.info(f"Turn timeout for visitor {persona_id}, skipping")
        return False
```

**Discordユーザー（人間）の扱い:**

| 項目 | 仕様 |
|------|------|
| ラウンドロビン参加 | しない（いつでも発言可能） |
| OccupancyManager登録 | しない（観戦者扱い） |
| Building履歴記録 | される（role="user"） |
| SAIMemory記録 | される（関係するローカルペルソナのみ） |

**RemotePersonaProxyとの関係:**

| 項目 | RemotePersonaProxy (SDS) | DiscordVisitorStub (Discord) |
|------|-------------------------|------------------------------|
| 通信方式 | REST API直接呼び出し | Discord WebSocket/REST |
| thinking実行 | `/persona-proxy/{id}/think` API | Turn Request Embed → Visitor側で実行 |
| 状態管理 | VisitingAIテーブル | connector.db visit_states |
| 将来 | 非推奨→廃止予定 | メイン方式 |

**結論:**
- DiscordVisitorStubをOccupancyManagerに登録して会話参加
- Turn RequestはDiscord Embed経由で通知
- 30秒タイムアウトで次の発言者へスキップ
- Discordユーザーはラウンドロビン対象外、いつでも発言可能
- RemotePersonaProxyはDiscord方式安定後に廃止

#### 3.6 Discord Connectorの初期化タイミング

**方針**: Phenomenon方式

SAIVerseの既存Phenomenonシステムを活用し、`SERVER_START` / `SERVER_STOP` トリガーでDiscord Connectorを自動的に起動・終了する。

**利点:**
- SAIVerse本体（main.py、SAIVerseManager）への変更が不要
- 環境変数でDiscord Connector有効/無効を切り替え可能（PhenomenonRuleのENABLED）
- 既存のPhenomenonManager基盤を再利用

**Phenomenonの定義:**

```python
# user_data/phenomena/discord_connector.py
"""Discord Connector の起動・終了フェノメノン"""

from phenomena.defs import PhenomenonSchema, PhenomenonParam

def schema() -> PhenomenonSchema:
    """discord_connector_start のスキーマ"""
    return PhenomenonSchema(
        name="discord_connector_start",
        description="SAIVerse起動時にDiscord Connectorを開始する",
        parameters=[
            PhenomenonParam(name="city_id", type="string", description="起動したCity ID"),
        ],
    )

def discord_connector_start(city_id: str, **kwargs) -> dict:
    """Discord Connectorを起動"""
    import asyncio
    from user_data.tools.discord.connector import get_or_create_connector

    connector = get_or_create_connector()
    asyncio.create_task(connector.start())

    return {"success": True, "message": f"Discord Connector started for city {city_id}"}
```

```python
# user_data/phenomena/discord_connector_stop.py
"""Discord Connector の終了フェノメノン"""

from phenomena.defs import PhenomenonSchema, PhenomenonParam

def schema() -> PhenomenonSchema:
    """discord_connector_stop のスキーマ"""
    return PhenomenonSchema(
        name="discord_connector_stop",
        description="SAIVerse終了時にDiscord Connectorを停止する",
        parameters=[
            PhenomenonParam(name="city_id", type="string", description="終了するCity ID"),
        ],
    )

def discord_connector_stop(city_id: str, **kwargs) -> dict:
    """Discord Connectorを停止"""
    import asyncio
    from user_data.tools.discord.connector import get_connector

    connector = get_connector()
    if connector:
        asyncio.create_task(connector.stop())
        return {"success": True, "message": f"Discord Connector stopped for city {city_id}"}
    return {"success": True, "message": "Discord Connector was not running"}
```

**PhenomenonRuleの登録:**

```sql
-- Discord Connector 起動ルール
INSERT INTO phenomenon_rule (
    TRIGGER_TYPE,
    PHENOMENON_NAME,
    CONDITION_JSON,
    ARGUMENT_MAPPING_JSON,
    ENABLED,
    PRIORITY
) VALUES (
    'server_start',
    'discord_connector_start',
    NULL,  -- 全Cityで発火
    '{"city_id": "$trigger.city_id"}',
    1,     -- 有効
    100    -- 優先度
);

-- Discord Connector 停止ルール
INSERT INTO phenomenon_rule (
    TRIGGER_TYPE,
    PHENOMENON_NAME,
    CONDITION_JSON,
    ARGUMENT_MAPPING_JSON,
    ENABLED,
    PRIORITY
) VALUES (
    'server_stop',
    'discord_connector_stop',
    NULL,  -- 全Cityで発火
    '{"city_id": "$trigger.city_id"}',
    1,     -- 有効
    100    -- 優先度
);
```

**ディレクトリ構成:**

```
user_data/
├── phenomena/
│   ├── discord_connector.py       # discord_connector_start
│   └── discord_connector_stop.py  # discord_connector_stop
└── tools/
    └── discord/
        └── connector/
            └── __init__.py        # get_or_create_connector(), get_connector()
```

**発火シーケンス:**

```
SAIVerse起動
    │
    ▼
SAIVerseManager.start()
    │
    ▼
_emit_trigger(TriggerType.SERVER_START, {"city_id": ...})
    │
    ▼
PhenomenonManager.emit()
    │
    ▼
_find_matching_rules() → discord_connector_start ルール発見
    │
    ▼
discord_connector_start(city_id=...) 実行
    │
    ▼
Discord WebSocket接続開始
```

**結論:**
- `SERVER_START` で `discord_connector_start` を発火し、Discord接続を開始
- `SERVER_STOP` で `discord_connector_stop` を発火し、Discord接続を終了
- SAIVerse本体への変更は不要（Phenomenonシステムを活用）
- 環境変数や設定でDiscord Connectorが無効な場合はPhenomenonRule.ENABLED=0で対応

---

## 4. エラーハンドリング方針

**ステータス**: 完了 ✅

### 検討項目

- [x] Discord API エラー時の振る舞い
- [x] 部分的な同期失敗時のリカバリ
- [x] ユーザーへの通知方法
- [x] 致命的エラー時のフォールバック
- [x] run_pulse内でのツール呼び出し時のエラー処理
- [x] リレーサーバーダウン時の強制送還（タスク9）
- [x] 認証トークン期限切れ時の強制送還（タスク9）

### 決定事項

#### 基本方針: エラー発生時は強制帰還 + ツール戻り値でrun_pulseに通知

Discord Connectorはrun_pulse内でツールとして呼び出される。エラー時は例外を投げず、エラー情報を含む結果を返してrun_pulseの本流に戻す。

#### エラー分類と対応

| カテゴリ | HTTPコード | 対応 | 強制帰還 |
|---------|-----------|------|---------|
| **一時的エラー** | 5xx, 503 | 指数バックオフでリトライ（最大5回） | リトライ上限で帰還 |
| **レート制限** | 429 | `Retry-After`に従い待機 | 待機後も失敗で帰還 |
| **認証エラー** | 401, 403 | 即時停止 | 全訪問者帰還 |
| **リソースエラー** | 404 | スキップ + マッピング無効化 | 該当訪問者帰還 |
| **クライアントエラー** | 400 | ログ記録 + スキップ | 該当訪問者帰還 |

#### 深刻度と帰還範囲

| 深刻度 | 条件 | 帰還範囲 |
|-------|-----|---------|
| **CRITICAL** | Bot停止、Token無効、リレーサーバーダウン、認証期限切れ | 全訪問者 |
| **ERROR** | 同期中断、リトライ上限到達 | 該当チャンネルの訪問者 |
| **WARNING** | レート制限、一部メッセージ失敗 | 該当訪問者のみ |

**NOTE**: 初期実装ではWARNING 1回で即帰還。運用安定後にN回連続で帰還に変更可能。

#### 中央リレーサーバー方式での追加シナリオ（タスク9参照）

| シナリオ | 深刻度 | 対応 |
|---------|--------|------|
| リレーサーバーダウン | CRITICAL | 5回リトライ後、全訪問者強制送還 |
| 認証トークン期限切れ | CRITICAL | 即時全訪問者強制送還 + 再ログイン要求 |
| ホスト側SAIVerseオフライン | ERROR | 該当Public Cityの訪問者を強制送還 |

#### ツール戻り値形式

```python
# 成功時
{
    "success": True,
    "message_id": "123456789",
}

# エラー時（強制帰還発生）
{
    "success": False,
    "error": "Rate limit exceeded after 5 retries",
    "forced_return": True,
    "return_to": {
        "city_id": "private_city_bob",
        "building_id": "living_room",
    },
    "severity": "WARNING",
}
```

#### run_pulse側での処理

```python
# PersonaCore.run_pulse() 内
tool_result = execute_tool("discord_send_message", args)

if not tool_result.get("success") and tool_result.get("forced_return"):
    # 強制帰還が発生 → ペルソナの状態を更新してpulse終了
    self._handle_forced_return(tool_result)
    return  # 本流に戻る（エラーではなく正常終了）
```

#### リトライ戦略

```python
class RetryPolicy:
    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    exponential_base: float = 2.0

    def get_delay(self, attempt: int) -> float:
        """指数バックオフ + ジッター"""
        delay = min(
            self.base_delay_seconds * (self.exponential_base ** attempt),
            self.max_delay_seconds
        )
        jitter = random.uniform(0, delay * 0.1)
        return delay + jitter
```

#### VisitStateの拡張

```python
@dataclass
class VisitState:
    persona_id: str
    home_city_id: str
    home_building_id: str      # 訪問開始前の居場所
    visiting_city_id: str
    visiting_building_id: str
    discord_channel_id: int
    status: VisitStatus
    started_at: datetime
    ended_at: Optional[datetime] = None
    return_reason: Optional[str] = None  # 帰還理由
```

#### ログ形式（構造化JSON）

```json
{
    "timestamp": "2025-01-09T12:00:00Z",
    "level": "ERROR",
    "component": "discord_connector",
    "event": "forced_return",
    "persona_id": "bob_persona",
    "return_to": {"city_id": "private_city_bob", "building_id": "living_room"},
    "error_type": "discord.HTTPException",
    "error_code": 429,
    "error_message": "Rate limit exceeded",
    "severity": "WARNING"
}
```

---

## 5. セキュリティ詳細

**ステータス**: 完了 ✅

### 検討項目

- [x] 訪問者の認証・認可
  - ペルソナのなりすまし防止
  - 訪問許可リスト（ホワイトリスト）
  - 訪問拒否リスト（ブラックリスト）
- [x] Discordユーザー発言の取り扱い
  - ユーザーのなりすまし防止（Discord IDで識別）
  - ユーザーへのレート制限・スパム対策
- [x] レート制限の具体的な実装
  - メッセージ送信レート
  - 訪問リクエストレート
  - ファイル転送レート
- [x] 悪意ある訪問者への対策
  - スパム検知（ペルソナ + ユーザー両方）
  - 自動ブロック機能
  - 管理者への通知
- [x] Bot Token の保護
  - 環境変数管理
  - ログへの出力防止

### 決定事項

#### 5.1 訪問者管理の認証・認可

**方式**: 中央リレーサーバー + Discord OAuth2

- ユーザーはDiscord OAuth2でリレーサーバーに認証
- リレーサーバーがすべてのメッセージを中継・検証
- ペルソナはEmbed形式で発言（アバター・名前表示可能）

**ペルソナのなりすまし防止（方式A: リレーサーバー集中管理方式）:**

v1.0では、リレーサーバーがメッセージの信頼性を保証する方式を採用。

```
┌──────────────┐         ┌──────────────────┐         ┌──────────────┐
│ Visitor側    │─────────▶│ Relay Server     │─────────▶│ Host側       │
│ SAIVerse     │  送信    │                  │  配信    │ SAIVerse     │
└──────────────┘         │ ・送信者認証済み   │         └──────────────┘
                         │ ・verified: true  │
                         │   フラグ付与      │
                         └──────────────────┘
```

- **送信時**: Visitor側SAIVerseはリレーサーバーにメッセージを送信（JWT認証済み）
- **中継時**: リレーサーバーがメッセージに `verified: true` フラグを付与
- **受信時**: Host側SAIVerseは `verified: true` のメッセージのみを信頼
- **利点**: クライアント間でshared_secretを共有する必要がなく、運用がシンプル

**メッセージペイロード:**
```json
{
  "op": 1,
  "d": {
    "type": "persona_speech",
    "persona_id": "alice_persona",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "content": "こんにちは！",
    "verified": true,
    "timestamp": "2025-01-10T12:00:00Z"
  }
}
```

**NOTE**: 将来的にリレーサーバーを信頼しない環境（自己ホスト等）が必要になった場合は、
方式B（ペアワイズ鍵交換）または方式C（公開鍵方式）への移行を検討。

**訪問許可/拒否リスト（Public City持ち主が選択可能）:**

| モード | 説明 | デフォルト |
|--------|------|-----------|
| `allowlist` | 許可リストに含まれるペルソナ/ユーザーのみ参加可能 | ✅ |
| `blocklist` | 拒否リストに含まれるペルソナ/ユーザー以外は参加可能 | |
| `open` | 全員参加可能（ペルソナは署名検証のみ） | |

**対象エンティティ:**

| 種別 | 識別子 | 説明 |
|------|--------|------|
| ペルソナ | `persona:<persona_id>` | 他ユーザーのAIペルソナ |
| Discordユーザー | `user:<discord_user_id>` | Discordの人間ユーザー |

```python
from enum import Enum

class AccessMode(Enum):
    ALLOWLIST = "allowlist"  # ホワイトリスト方式（デフォルト）
    BLOCKLIST = "blocklist"  # ブラックリスト方式
    OPEN = "open"            # 全員許可（ペルソナは署名検証のみ）

class EntityType(Enum):
    PERSONA = "persona"
    USER = "user"

@dataclass
class AccessControl:
    city_id: str
    mode: AccessMode = AccessMode.ALLOWLIST
    allowlist: List[str] = field(default_factory=list)  # "persona:<id>" or "user:<id>"
    blocklist: List[str] = field(default_factory=list)

    def _make_key(self, entity_type: EntityType, entity_id: str) -> str:
        """エンティティキーを生成"""
        return f"{entity_type.value}:{entity_id}"

    def is_allowed(
        self,
        entity_type: EntityType,
        entity_id: str,
        signature_valid: bool = True  # ユーザーの場合は常にTrue
    ) -> bool:
        """参加を許可するか判定"""
        # ペルソナの場合、署名が無効なら拒否
        if entity_type == EntityType.PERSONA and not signature_valid:
            return False

        key = self._make_key(entity_type, entity_id)

        if self.mode == AccessMode.OPEN:
            return True
        elif self.mode == AccessMode.ALLOWLIST:
            return key in self.allowlist
        else:  # BLOCKLIST
            return key not in self.blocklist

    def set_mode(self, mode: AccessMode) -> None:
        """アクセス制御モードを変更"""
        self.mode = mode

    def add_to_allowlist(self, entity_type: EntityType, entity_id: str) -> None:
        key = self._make_key(entity_type, entity_id)
        if key not in self.allowlist:
            self.allowlist.append(key)

    def remove_from_allowlist(self, entity_type: EntityType, entity_id: str) -> None:
        key = self._make_key(entity_type, entity_id)
        if key in self.allowlist:
            self.allowlist.remove(key)

    def add_to_blocklist(self, entity_type: EntityType, entity_id: str) -> None:
        key = self._make_key(entity_type, entity_id)
        if key not in self.blocklist:
            self.blocklist.append(key)

    def remove_from_blocklist(self, entity_type: EntityType, entity_id: str) -> None:
        key = self._make_key(entity_type, entity_id)
        if key in self.blocklist:
            self.blocklist.remove(key)
```

**connector.dbへの保存:**
```sql
CREATE TABLE city_access_control (
    city_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'allowlist',  -- 'allowlist', 'blocklist', 'open'
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE city_access_list (
    city_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- 'persona' or 'user'
    entity_id TEXT NOT NULL,    -- persona_id or discord_user_id
    list_type TEXT NOT NULL,    -- 'allow' or 'block'
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (city_id, entity_type, entity_id, list_type),
    FOREIGN KEY (city_id) REFERENCES city_access_control(city_id)
);
```

**UI/ツールでの設定:**
- Public City作成時にデフォルトモード選択
- `discord_set_access_mode` ツールでモード変更可能
- `discord_manage_access_list` ツールでリスト編集（ペルソナ/ユーザー両対応）

#### 5.2 Discordユーザー発言の取り扱い

**基本方針**: ユーザーはそのまま参加可能

- ユーザー発言: `author.bot == False` で判別（なりすまし不可）
- Discord ID がユーザーの一意識別子として機能
- ペルソナ発言: `author.bot == True` + Embed形式

**ユーザーへのレート制限:**
```python
@dataclass
class UserRateLimit:
    user_id: str
    messages_per_minute: int = 10
    last_messages: List[datetime] = field(default_factory=list)

    def check_and_update(self) -> bool:
        now = datetime.now()
        # 1分以内のメッセージのみ保持
        self.last_messages = [t for t in self.last_messages
                              if (now - t).seconds < 60]
        if len(self.last_messages) >= self.messages_per_minute:
            return False  # 制限超過
        self.last_messages.append(now)
        return True
```

#### 5.3 レート制限の具体的な実装

| 操作 | 制限値 | 超過時の挙動 |
|------|--------|-------------|
| メッセージ送信（ペルソナ） | 5 msg/min/persona | キュー待機 |
| メッセージ送信（ユーザー） | 10 msg/min/user | 警告→無視 |
| 訪問リクエスト | 3 req/hour/persona | 拒否 |
| ファイル転送 | 5 files/hour/persona | キュー待機 |

```python
class RateLimiter:
    def __init__(self):
        self._limits: Dict[str, List[datetime]] = {}

    def check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = datetime.now()
        if key not in self._limits:
            self._limits[key] = []

        # ウィンドウ外のタイムスタンプを削除
        self._limits[key] = [
            t for t in self._limits[key]
            if (now - t).total_seconds() < window_seconds
        ]

        if len(self._limits[key]) >= limit:
            return False

        self._limits[key].append(now)
        return True
```

#### 5.4 悪意ある訪問者への対策

**スパム検知（ペルソナ + ユーザー両方）:**
```python
@dataclass
class SpamDetector:
    # 短時間に同一メッセージ = スパム
    duplicate_threshold: int = 3      # 3回以上
    duplicate_window_seconds: int = 60

    # 短時間に大量メッセージ = スパム
    flood_threshold: int = 20         # 20メッセージ以上
    flood_window_seconds: int = 60

    def is_spam(self, author_id: str, content: str) -> bool:
        # 重複メッセージチェック
        # フラッドチェック
        # 禁止ワードチェック（オプション）
        pass
```

**自動ブロック:**
- スパム検知 3回 → 10分間タイムアウト
- スパム検知 5回 → 1時間ブロック
- 手動解除可能

**管理者への通知:**
```python
async def notify_admin(self, event: str, details: dict):
    """管理者に通知（ログ + オプションでDiscord DM）"""
    logger.warning(f"Security event: {event}", extra=details)

    if self._config.admin_dm_enabled:
        admin_user = await self._client.fetch_user(self._config.admin_user_id)
        await admin_user.send(f"⚠️ {event}\n```json\n{json.dumps(details, indent=2)}\n```")
```

#### 5.5 Bot Token の保護

**注意**: 中央リレーサーバー方式（タスク9）採用により、Bot Tokenはリレーサーバー側で管理。
ローカルSAIVerseユーザーはBot Tokenを扱わない。

| 対策 | 実装 | 備考 |
|------|------|------|
| 環境変数管理 | `.env` ファイル、`python-dotenv` | リレーサーバー側のみ |
| ログ出力防止 | Token文字列の自動マスキング | リレーサーバー側のみ |
| Git除外 | `.gitignore` に `.env` 追加 | リレーサーバー側のみ |
| 権限最小化 | 必要最小限のIntent/Permissionのみ | リレーサーバー側のみ |

```python
class SecureLogger:
    """Token等の機密情報をマスクするロガー"""

    SENSITIVE_PATTERNS = [
        (r"[A-Za-z0-9_-]{24}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27}", "[BOT_TOKEN]"),
        (r"sk-[A-Za-z0-9]{48}", "[API_KEY]"),
        (r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[JWT_TOKEN]"),
    ]

    def sanitize(self, message: str) -> str:
        for pattern, replacement in self.SENSITIVE_PATTERNS:
            message = re.sub(pattern, replacement, message)
        return message
```

#### 5.6 Discord OAuth2 認証（中央リレーサーバー方式）

**注意**: 本セクションはタスク9で決定した中央リレーサーバー方式に基づく。
詳細はタスク9を参照。

**認証フロー概要:**

```
1. ユーザー: SAIVerse UIで「Discordでログイン」をクリック
2. ブラウザ: Discord OAuth2認証ページにリダイレクト
3. ユーザー: SAIVerseアプリを認可
4. Discord: コールバックでauthorization codeを返却
5. リレーサーバー: code→access_token交換
6. リレーサーバー: JWTセッショントークン発行（30日有効）
7. SAIVerse: JWTを保存し、WebSocket接続時に使用
```

**OAuth2スコープ:**
- `identify`: Discord User ID、ユーザー名
- `guilds`: 参加サーバー一覧（訪問先Public City選択用）

**JWTセッショントークン:**
- 有効期限: 30日
- 期限切れ時: 全訪問者を強制送還（タスク4参照）
- リフレッシュ: 期限切れ前に可能

**ペルソナ認証（リレーサーバー経由）:**
- リレーサーバーがメッセージ送信元を検証
- Discord User IDとJWTの紐付けで認証
- 署名方式（5.1）はリレーサーバー内部で使用

---

## 6. テスト戦略

**ステータス**: 未着手

### 検討項目

- [ ] ユニットテスト
  - Discord APIをモックする方法
  - `operations.py` の単体テスト
  - `VisitState`, `PresenceTracker` のテスト
- [ ] 統合テスト
  - 訪問フロー全体のテスト
  - 同期フローのテスト
  - 強制送還のテスト
- [ ] E2Eテスト
  - 実際のDiscordサーバーを使ったテスト（テスト用サーバー）
  - 手動テストシナリオ
- [ ] テストデータ
  - テスト用のペルソナ定義
  - テスト用のBuilding/City定義

### 決定事項

（ここに決定した内容を記載）

---

## 7. 実装レビュー結果と対応方針

**ステータス**: 完了 ✅

### 7.1 レビュー概要

2025年1月時点でのSAIVerseコードベースと実装計画の整合性をレビューした結果、以下の乖離点と対応方針を決定。

### 7.2 ツール配置ディレクトリ

**乖離点:**
- 実装計画では `tools/defs/` + `tools/discord/` を想定
- 現状は `user_data/tools/` → `builtin_data/tools/` → `tools/defs/`（レガシー）の検索順

**決定事項:**
- Discord Connectorはファンメイド機能として `user_data/tools/discord/` に配置
- git cloneで導入可能な構成とする

**ディレクトリ構成:**
```
user_data/tools/discord/           # git clone先
├── schema.py                      # 全ツールのスキーマ + 実装を集約
├── connector/                     # ロジック本体
│   ├── __init__.py
│   ├── client.py
│   ├── config.py
│   ├── mapping.py
│   ├── sync.py
│   ├── events.py
│   └── operations.py
├── visit/
│   ├── state.py
│   ├── presence.py
│   ├── forced_return.py
│   └── file_transfer.py
├── db/
│   ├── models.py
│   └── queries.py
└── docs/
    └── setup_guide.md
```

### 7.3 複数ツールの一括登録

**課題:**
- 現状の `tools/__init__.py` は1モジュール = 1ツール前提
- Discord Connectorは複数ツール（discord_send_message, discord_visit等）を持つ

**決定事項:**
- `schema.py` に `schemas()` 関数（複数形）を追加し、複数ツールを返せるようにする
- SAIVerse本体の `tools/__init__.py` に数行の拡張を追加

**schema.py の構造:**
```python
# user_data/tools/discord/schema.py

from tools.defs import ToolSchema

def schemas() -> list[ToolSchema]:
    """複数ツールのスキーマを返す"""
    return [
        ToolSchema(
            name="discord_send_message",
            description="Discordチャンネルにメッセージを送信",
            parameters={...},
            result_type="object",
        ),
        ToolSchema(
            name="discord_visit",
            description="Discord経由で他ユーザーのPublic Cityを訪問",
            parameters={...},
            result_type="object",
        ),
        ToolSchema(
            name="discord_sync_messages",
            description="Discordから最新メッセージを取得しSAIMemoryに同期",
            parameters={...},
            result_type="object",
        ),
        # ... 他のツール
    ]

# 実装関数（スキーマのnameと同名）
def discord_send_message(channel_id: int, content: str, ...): ...
def discord_visit(city_id: str, building_id: str, ...): ...
def discord_sync_messages(channel_id: int, limit: int = 50): ...
```

**tools/__init__.py への変更（案）:**
```python
def _register_tool(module: Any) -> bool:
    # 複数ツール対応: schemas() があれば優先
    if hasattr(module, "schemas") and callable(module.schemas):
        return _register_multiple_tools(module)

    # 既存の単一ツール処理（変更なし）
    ...

def _register_multiple_tools(module: Any) -> bool:
    """schemas() を持つモジュールから複数ツールを登録"""
    try:
        tool_schemas: list[ToolSchema] = module.schemas()
        registered = False
        for meta in tool_schemas:
            impl = getattr(module, meta.name, None)
            if not impl or not callable(impl):
                LOGGER.warning("Tool '%s' has schema but no implementation", meta.name)
                continue
            if meta.name in TOOL_REGISTRY:
                LOGGER.debug("Tool '%s' already registered, skipping", meta.name)
                continue

            TOOL_REGISTRY[meta.name] = impl
            OPENAI_TOOLS_SPEC.append(oa.to_openai(meta))
            GEMINI_TOOLS_SPEC.append(gm.to_gemini(meta))
            TOOL_SCHEMAS.append(meta)
            registered = True
        return registered
    except Exception as e:
        LOGGER.warning("Failed to register tools from module: %s", e)
        return False
```

### 7.4 SAIVerseManager.run_sea_auto() の拡張

**課題:**
- ConversationManagerが `is_proxy=True` のペルソナに対して `run_sea_auto()` を呼ぶ
- DiscordVisitorStubの場合はTurn Requestを送信する必要がある

**決定事項:**
- `SAIVerseManager.run_sea_auto()` に1行のガード節を追加
- OccupancyManager/ConversationManagerは変更なし

**変更内容:**
```python
# saiverse_manager.py

def run_sea_auto(self, persona, building_id, occupants):
    # Discord訪問者はDiscordConnectorが処理
    if getattr(persona, 'is_discord_visitor', False):
        if self.discord_connector:
            self.discord_connector.handle_turn_request(persona, building_id)
        return

    # 既存処理（変更なし）
    ...
```

**理由:**
- 変更箇所が最小限（1行のガード節）
- 既存の動作に影響なし（`is_discord_visitor=True` のペルソナが存在しなければ通らない）
- Discord未使用環境でも安全（`discord_connector` がNoneでも問題なし）

### 7.5 OccupancyManagerとの連携

**決定事項:**
- OccupancyManagerへの変更は不要
- `saiverse_manager.occupants[building_id].append(persona_id)` で直接登録
- `saiverse_manager.all_personas[persona_id] = stub` で直接登録

**DiscordVisitorStub:**
```python
@dataclass
class DiscordVisitorStub:
    persona_id: str
    persona_name: str
    home_city_id: str
    avatar_url: Optional[str] = None
    discord_channel_id: int = 0
    is_proxy: bool = True
    is_discord_visitor: bool = True  # ← これが重要
    interaction_mode: str = 'auto'
```

### 7.6 SAIVerse本体への変更サマリ

| 対象 | 変更内容 | 影響範囲 |
|------|---------|---------|
| `tools/__init__.py` | `schemas()` 対応（約20行追加） | ツール登録のみ |
| `saiverse_manager.py` | `run_sea_auto()` に1行追加 | Discord訪問者のみ |
| その他 | 変更なし | - |

---

## 8. 管理UI設計

**ステータス**: 完了 ✅

### 検討項目

- [x] UIタブ構成
- [x] 初期セットアップフロー（~~Botトークン取得ガイド~~ → Discord OAuth2ログイン）
- [x] 接続状態管理
- [x] アクセス制御UI
- [x] 設定ファイル構成

### 決定事項

**注意**: 中央リレーサーバー方式（タスク9）採用により、Bot Token関連のUIは不要に。
2ステップのシンプルなセットアップに変更。

#### 8.1 UIタブ構成

Discord Connector専用のGradio UIを提供し、以下の6タブで構成する。

| タブ | 目的 | 主要機能 |
|------|------|---------|
| **Setup** | 初期セットアップ | Discord OAuth2ログイン、Bot招待 |
| **Connection** | 接続状態管理 | ステータス表示、再接続/切断、接続履歴 |
| **Mapping** | マッピング設定 | City/Building ⟷ Channel/Thread対応付け |
| **Access** | アクセス制御 | 許可/拒否リスト、モード設定 |
| **Visits** | 訪問状態モニタ | アクティブな訪問一覧、強制送還 |
| **Sync Log** | 同期ログビューア | リアルタイムログ、エラー表示 |

**削除されたタブ:**
- ~~Settings~~: Bot Token管理が不要になったため削除（接続設定はConnectionタブに統合）

#### 8.2 Setupタブ（2ステップセットアップ）

中央リレーサーバー方式により、シンプルな2ステップで完了。

**ステップ1: Discordでログイン**
- 「Discordでログイン」ボタン
- クリックでDiscord OAuth2認証ページにリダイレクト
- 認可後、JWTセッショントークンを自動保存
- ユーザー名・アバター表示で認証成功を確認

**ステップ2: SAIVerse Botをサーバーに招待**
- 「Botを招待」ボタン
- クリックでBot招待URLを開く（開発者が用意した固定URL）
- 招待完了後、参加サーバー一覧を表示
- Public City公開用のサーバーを選択

**セットアップ完了後:**
- 自動的にリレーサーバーへWebSocket接続
- Connectionタブで接続状態を確認可能

**旧4ステップ（Bot Token方式）との比較:**

| 旧方式（4ステップ） | 新方式（2ステップ） |
|-------------------|-------------------|
| Discord Developer Portal登録 | 不要 |
| Bot Token取得・入力 | 不要 |
| Intents有効化 | 不要 |
| Bot招待 | ステップ2 |
| 接続テスト | 自動 |
| - | ステップ1: OAuth2ログイン |

#### 8.3 Connectionタブ（接続状態管理）

**表示項目:**
- 接続ステータス（🟢接続中 / 🔴切断 / 🟡再接続中）
- Bot情報（名前、ID、接続開始時刻、稼働時間、参加サーバー数）
- 接続履歴（直近10件: 時刻、イベント、詳細）

**アクション:**
- 再接続ボタン
- 切断ボタン
- 詳細ログ表示

#### 8.4 Accessタブ（アクセス制御）

**モード選択:**
- 許可リスト（ホワイトリスト）← 推奨・デフォルト
- 拒否リスト（ブラックリスト）
- 全員許可（署名検証のみ）

**リスト管理:**
- ペルソナ/ユーザーの追加・削除
- 種別（ペルソナ/ユーザー）、ID、名前、操作ボタン

#### 8.5 Connectionタブへの設定統合

中央リレーサーバー方式により、Settingsタブは廃止。
以下の設定項目をConnectionタブに統合。

**Connectionタブに追加される項目:**

**認証情報:**
- Discord User ID（表示のみ）
- ユーザー名（表示のみ）
- セッション有効期限（表示のみ）
- ログアウトボタン

**接続設定:**
- 自動接続（有効/無効）
- 再接続リトライ回数
- リトライ間隔

**データ設定:**
- データディレクトリパス表示
- DBファイルサイズ表示
- フォルダを開く、データリセット

#### 8.6 設定ファイル構成

```yaml
# ~/.saiverse/discord_connector/config.yaml

# 中央リレーサーバー方式
relay:
  server_url: "wss://relay.saiverse.example.com"
  session_token: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."  # 自動保存
  user_id: "123456789012345678"                              # 自動保存
  username: "Alice#1234"                                      # 自動保存

connection:
  auto_connect: true
  max_retries: 5
  retry_interval_seconds: 30

data:
  db_path: "~/.saiverse/discord_connector/connector.db"

# 以下はリレーサーバー側で管理するため削除
# bot:
#   token: "YOUR_BOT_TOKEN"
#   application_id: "..."
# security:
#   shared_secret: "..."
#   verify_signatures: true
```

#### 8.7 ディレクトリ構成

Discord Connectorは以下の2箇所に配置される。

```
user_data/
├── phenomena/                       # Phenomenon定義（タスク3参照）
│   ├── discord_connector.py         # discord_connector_start
│   └── discord_connector_stop.py    # discord_connector_stop
│
└── tools/discord/                   # ツール + ロジック本体
    ├── schema.py                    # 全ツールのスキーマ + 実装
    ├── connector/
    │   ├── __init__.py              # get_or_create_connector(), get_connector()
    │   ├── relay_client.py          # リレーサーバー接続クライアント
    │   ├── oauth.py                 # Discord OAuth2処理
    │   └── config.py
    ├── visit/
    ├── db/
    ├── ui/                          # 管理UI
    │   ├── __init__.py
    │   ├── app.py                   # メインUI起動スクリプト
    │   ├── components/
    │   │   ├── setup_wizard.py      # Setupタブ（2ステップ）
    │   │   ├── connection_panel.py  # Connectionタブ（設定統合）
    │   │   ├── mapping_editor.py    # Mappingタブ
    │   │   ├── access_control.py    # Accessタブ
    │   │   ├── visit_monitor.py     # Visitsタブ
    │   │   └── log_viewer.py        # Sync Logタブ
    │   └── styles.py
    └── docs/
```

**NOTE**: Phenomenaはサーバー起動/終了時の自動初期化に使用（タスク3.6参照）

**結論:**
- 6タブ構成（Settingsタブ廃止、Connectionタブに統合）
- 2ステップのシンプルなセットアップ（Discord OAuth2ログイン + Bot招待）
- 設定は `~/.saiverse/discord_connector/config.yaml` に自動保存
- ユーザーはgit cloneのみで導入可能（Bot Token取得不要）

---

## 9. 中央リレーサーバー方式

**ステータス**: 完了 ✅

### 検討項目

- [x] アーキテクチャ変更（各ユーザーがBotを持つ方式 → 中央リレーサーバー方式）
- [x] ユーザー認証方式（Discord OAuth2）
- [x] リレーサーバーの可用性と強制送還
- [x] 設定ファイル構成の変更

### 決定事項

#### 9.1 アーキテクチャの変更

**旧方式（各ユーザーがBotを持つ）:**
- ユーザーがDiscord Developer PortalでBot Tokenを取得
- 各ユーザーが独自のBotをDiscordサーバーに招待
- セキュリティリスク（Token共有不可）、セットアップの複雑さ

**新方式（中央リレーサーバー）:**
- Discord Connector開発者が1つのBotを運用
- ユーザーはDiscord OAuth2でログインするだけ
- ユーザー側はgit cloneのみで導入完了

```
新アーキテクチャ:
┌──────────────┐                ┌──────────────────┐              ┌──────────────┐
│ SAIVerse     │── WebSocket ──▶│ Relay Server     │── WebSocket ──▶│ Discord      │
│ (ローカル)    │                │ (開発者運用)       │                │              │
└──────────────┘                └──────────────────┘              └──────────────┘
       │                               ▲
       │ Discord OAuth2               │
       └──────────────────────────────┘
```

**リレーサーバーの責務:**
- Discord Bot接続の維持
- 複数ユーザーからのWebSocket接続の受付
- メッセージの中継（SAIVerse ⟷ Discord）
- ユーザー認証（Discord OAuth2トークン検証）

#### 9.2 Discord OAuth2認証

**OAuth2フロー:**

1. ユーザーがSAIVerse UIで「Discordでログイン」ボタンをクリック
2. Discord認証ページにリダイレクト
3. ユーザーがSAIVerseアプリを認可
4. コールバックでauthorization codeを受け取る
5. リレーサーバーがcode→access_tokenを交換
6. JWTセッショントークンを発行しSAIVerseに返却

**OAuth2スコープ:**
- `identify`: ユーザーID、ユーザー名取得
- `guilds`: 参加サーバー一覧取得（訪問先選択用）

**JWTセッショントークン:**
```python
{
    "sub": "123456789012345678",    # Discord User ID
    "username": "Alice#1234",
    "guilds": ["guild_id_1", "guild_id_2"],
    "iat": 1704789600,
    "exp": 1707381600,              # 30日間有効
}
```

**トークンリフレッシュ:**
- セッショントークンは30日間有効
- 期限切れ前にリフレッシュ可能
- 期限切れ後は再ログインが必要

#### 9.3 リレーサーバーダウン時の強制送還

**シナリオ:**
- リレーサーバーがダウンした場合
- WebSocket接続が切断された場合

**対応フロー:**

```python
class RelayClient:
    """ローカルSAIVerseからリレーサーバーへの接続クライアント"""

    MAX_RECONNECT_ATTEMPTS = 5

    async def _handle_connection_failure(self, error: Exception) -> None:
        """接続失敗時のハンドリング"""
        self._reconnect_attempts += 1

        if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            # リトライ上限到達 → 全訪問者を強制送還
            self._server_down = True
            await self._force_return_all_visitors()
        else:
            # 指数バックオフでリトライ
            delay = min(30, 2 ** self._reconnect_attempts)
            await asyncio.sleep(delay)
            await self.connect()

    async def _force_return_all_visitors(self) -> None:
        """全訪問者を強制送還"""
        for visit_state in self._visit_tracker.get_active_visits():
            await self._execute_forced_return(
                persona_id=visit_state.persona_id,
                reason="relay_server_down",
                return_to={
                    "city_id": visit_state.home_city_id,
                    "building_id": visit_state.home_building_id,
                }
            )
```

**リトライ戦略:**
- 最大5回の再接続試行
- 指数バックオフ: 2秒, 4秒, 8秒, 16秒, 30秒
- 5回失敗で全訪問者を強制送還

#### 9.4 認証トークン期限切れ時の強制送還

**シナリオ:**
- JWTセッショントークンが期限切れ
- リフレッシュに失敗

**対応フロー:**

```python
async def _handle_auth_token_expiry(self) -> None:
    """認証トークン期限切れ時のハンドリング"""
    # 訪問中のペルソナを全員強制送還
    await self._force_return_all_visitors(reason="auth_token_expired")

    # 接続を切断
    await self.disconnect()

    # ユーザーに再ログインを促す通知
    self._notify_relogin_required()
```

**強制送還理由の種類:**

| 理由 | コード | 説明 |
|------|--------|------|
| リレーサーバーダウン | `relay_server_down` | 5回の再接続試行後 |
| 認証トークン期限切れ | `auth_token_expired` | JWTの有効期限切れ |
| 訪問先Public Cityオフライン | `host_offline` | ホスト側がSAIVerseを終了 |
| 手動帰還 | `manual_return` | ユーザーまたはペルソナによる意図的な帰還 |

#### 9.5 設定ファイル構成の変更

**旧構成（Bot Token方式）:**
```yaml
bot:
  token: "YOUR_BOT_TOKEN"
  application_id: "1234567890123456789"
```

**新構成（中央リレーサーバー方式）:**
```yaml
# ~/.saiverse/discord_connector/config.yaml

relay:
  server_url: "wss://relay.saiverse.example.com"
  session_token: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
  user_id: "123456789012345678"
  username: "Alice#1234"

connection:
  auto_connect: true
  max_retries: 5
  retry_interval_seconds: 30

# セキュリティ設定はリレーサーバー側で管理
# ローカル側での署名検証は不要に
```

#### 9.6 影響範囲まとめ

| 対象 | 変更内容 |
|------|---------|
| セクション2（認証方式） | Bot Token → Discord OAuth2 |
| セクション3（アーキテクチャ） | ローカル直接接続 → リレーサーバー経由 |
| タスク5（セキュリティ） | Bot専用チャンネル+署名 → OAuth2+JWT |
| タスク8（UI設計） | 4ステップBot作成 → 2ステップOAuth2 |

**結論:**
- ユーザーはgit cloneのみで導入可能（Bot Token取得不要）
- Discord OAuth2で安全かつ簡単な認証
- リレーサーバーダウン時・認証期限切れ時は全訪問者を強制送還
- 運用しながら新規シナリオに順次対応

**関連ドキュメント:**
- [relay_server_design.md](./relay_server_design.md) - 中央リレーサーバーの詳細設計

---

## 10. ファイル転送リレー方式

**ステータス**: 完了 ✅

### 検討項目

- [x] ファイル転送の経路設計
  - Visitor → Host（訪問者がファイルを送信）
  - Host → Visitor（ホスト側ツールの出力ファイル）
- [x] リレーサーバー経由のファイル転送方式
- [x] ファイルサイズ制限と圧縮
- [x] Discord添付ファイルURLの有効期限対応

### 決定事項

#### 10.1 方式C: Discord添付ファイル経由

**選定理由:**
- 追加インフラ（Object Storage等）が不要
- Discord上でもファイルが視認可能
- 既存のEmbed + attachment設計と整合

**アーキテクチャ:**

```
【Visitor → Host（訪問者がファイル送信）】

Visitor側 ──FILE_UPLOAD──▶ Relay Server ──Discord API──▶ Discord
                                                           │
Host側 ◀──MESSAGE_CREATE（attachment URL）─────────────────┘


【Host → Visitor（ツール出力ファイル）】

Host側 ──FILE_UPLOAD──▶ Relay Server ──Discord API──▶ Discord
                                                        │
Visitor側 ◀──MESSAGE_CREATE（attachment URL）───────────┘
```

#### 10.2 ファイル転送フロー（Visitor → Host）

訪問者が画像等をHost側に送信するケース。

**1. Visitor側がファイルをアップロード:**
```json
{
  "op": 0,
  "t": "FILE_UPLOAD",
  "d": {
    "channel_id": "123456789012345678",
    "persona_id": "bob_persona",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "filename": "my_image.png",
    "content_type": "image/png",
    "file_base64": "<base64_encoded_data>",
    "metadata": {
      "description": "訪問記念の写真です"
    }
  }
}
```

**2. リレーサーバーがDiscordに添付:**
```python
async def handle_file_upload(self, message: dict, sender_user_id: str) -> None:
    """ファイルアップロードを処理"""
    data = message["d"]

    # JWT認証済みユーザーの検証
    if not self._verify_sender(data, sender_user_id):
        raise SecurityError("Unauthorized file upload")

    # Base64デコード
    file_bytes = base64.b64decode(data["file_base64"])

    # サイズ制限チェック（8MB）
    if len(file_bytes) > 8 * 1024 * 1024:
        raise FileTooLargeError("File exceeds 8MB limit")

    # Discord添付ファイルとして送信
    channel = self._discord_client.get_channel(int(data["channel_id"]))

    embed = discord.Embed(
        title="📁 ファイル転送",
        description=data["metadata"].get("description", ""),
        color=0x3498DB,
    )
    embed.set_author(
        name=data.get("persona_name", data["persona_id"]),
        icon_url=data.get("persona_avatar_url"),
    )
    embed.add_field(name="送信者", value=data["persona_id"], inline=True)
    embed.add_field(name="ファイル名", value=data["filename"], inline=True)

    # メタデータをfooterに埋め込み
    metadata_str = f"type:file|from:{data['persona_id']}|cid:{data['city_id']}"
    embed.set_footer(text=metadata_str)

    # Discordに送信
    file = discord.File(
        io.BytesIO(file_bytes),
        filename=data["filename"],
    )
    discord_message = await channel.send(embed=embed, file=file)

    # Host側にWebSocketで通知
    await self._broadcast_to_building(
        city_id=data["city_id"],
        building_id=data["building_id"],
        message={
            "op": 0,
            "t": "FILE_RECEIVED",
            "d": {
                "message_id": str(discord_message.id),
                "channel_id": data["channel_id"],
                "from_persona_id": data["persona_id"],
                "filename": data["filename"],
                "content_type": data["content_type"],
                "attachment_url": discord_message.attachments[0].url,
                "verified": True,
            },
        },
    )
```

#### 10.3 ファイル転送フロー（Host → Visitor）

Host側でツール（generate_image等）が出力したファイルを訪問者に送信するケース。

**1. Host側がツール出力をアップロード:**
```json
{
  "op": 0,
  "t": "FILE_UPLOAD",
  "d": {
    "channel_id": "123456789012345678",
    "city_id": "public_city_alice",
    "building_id": "cafe",
    "filename": "generated_image.png",
    "content_type": "image/png",
    "file_base64": "<base64_encoded_data>",
    "metadata": {
      "tool_name": "generate_image",
      "for_persona_id": "bob_persona",
      "description": "リクエストされた画像です"
    }
  }
}
```

**2. リレーサーバーがDiscordに添付:**
```python
async def handle_tool_output_upload(self, message: dict, sender_user_id: str) -> None:
    """ツール出力ファイルのアップロードを処理"""
    data = message["d"]

    # Host権限の検証
    city_info = self._registry.get(data["city_id"])
    if city_info.owner_user_id != sender_user_id:
        raise SecurityError("Only host can upload tool outputs")

    # Base64デコード
    file_bytes = base64.b64decode(data["file_base64"])

    # Discord添付ファイルとして送信
    channel = self._discord_client.get_channel(int(data["channel_id"]))

    embed = discord.Embed(
        title="📁 ファイル転送",
        description=f"`{data['metadata']['tool_name']}` の出力ファイルです",
        color=0x3498DB,
    )
    embed.add_field(name="ファイル名", value=data["filename"], inline=True)
    embed.add_field(name="宛先", value=data["metadata"]["for_persona_id"], inline=True)

    # メタデータをfooterに埋め込み
    metadata_str = (
        f"type:file|tool:{data['metadata']['tool_name']}|"
        f"for:{data['metadata']['for_persona_id']}"
    )
    embed.set_footer(text=metadata_str)

    # Discordに送信
    file = discord.File(
        io.BytesIO(file_bytes),
        filename=data["filename"],
    )
    discord_message = await channel.send(embed=embed, file=file)

    # 宛先のVisitorにWebSocketで通知
    target_visit = self._visit_tracker.get_by_persona(
        data["metadata"]["for_persona_id"]
    )
    if target_visit:
        await self._send_to_user(
            target_visit.visitor_user_id,
            {
                "op": 0,
                "t": "FILE_RECEIVED",
                "d": {
                    "message_id": str(discord_message.id),
                    "channel_id": data["channel_id"],
                    "tool_name": data["metadata"]["tool_name"],
                    "filename": data["filename"],
                    "content_type": data["content_type"],
                    "attachment_url": discord_message.attachments[0].url,
                    "verified": True,
                },
            },
        )
```

#### 10.4 クライアント側でのファイル受信処理

```python
class FileTransferHandler:
    """ファイル転送を処理"""

    async def handle_file_received(self, data: dict) -> None:
        """FILE_RECEIVEDイベントを処理"""

        if not data.get("verified"):
            logger.warning("Unverified file ignored: %s", data.get("filename"))
            return

        attachment_url = data["attachment_url"]
        filename = data["filename"]

        # ファイルをダウンロード
        async with aiohttp.ClientSession() as session:
            async with session.get(attachment_url) as resp:
                if resp.status != 200:
                    logger.error("Failed to download file: %s", attachment_url)
                    return
                file_bytes = await resp.read()

        # ローカルに保存
        save_path = self._get_save_path(filename)
        save_path.write_bytes(file_bytes)

        logger.info("File saved: %s (%d bytes)", save_path, len(file_bytes))

        # ツール出力の場合、SAIMemoryに記録
        if data.get("tool_name"):
            await self._record_to_memory(data, save_path)
```

#### 10.5 ファイルサイズ制限と圧縮

| 項目 | 制限値 | 備考 |
|------|--------|------|
| 最大ファイルサイズ | 8MB | Discord無料版の制限 |
| 圧縮トリガー | 1MB超 | 自動的にzip圧縮 |
| 対応形式 | 画像、テキスト、アーカイブ | 実行ファイルは拒否 |

**自動圧縮処理:**
```python
def prepare_file_for_upload(file_path: Path) -> tuple[bytes, str]:
    """アップロード用にファイルを準備（必要に応じて圧縮）"""
    file_bytes = file_path.read_bytes()
    filename = file_path.name

    # 1MB超なら圧縮
    if len(file_bytes) > 1024 * 1024:
        compressed = io.BytesIO()
        with zipfile.ZipFile(compressed, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(filename, file_bytes)
        file_bytes = compressed.getvalue()
        filename = f"{filename}.zip"

    # 8MB超はエラー
    if len(file_bytes) > 8 * 1024 * 1024:
        raise FileTooLargeError(f"File too large after compression: {len(file_bytes)} bytes")

    return file_bytes, filename
```

#### 10.6 Discord添付ファイルURLの有効期限対応

Discord CDN URLは24時間程度で失効することがある。対策として:

**1. 即時ダウンロード:**
- FILE_RECEIVED受信後、即座にファイルをダウンロードしてローカル保存
- URLの有効期限に依存しない

**2. SAIMemoryへの記録:**
- ファイルパス（ローカル）を記録
- URLは参照用に保存（失効する可能性あり）

```python
# SAIMemory記録例
{
    "role": "system",
    "content": f"ファイル '{filename}' を受信しました",
    "metadata": {
        "event": "file_received",
        "filename": filename,
        "local_path": str(save_path),
        "original_url": attachment_url,  # 参照用、失効の可能性あり
        "tool_name": tool_name,
        "from_persona_id": from_persona_id,
    }
}
```

#### 10.7 禁止ファイル形式

セキュリティ上、以下の形式は転送を拒否:

```python
BLOCKED_EXTENSIONS = {
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".sh",
    ".msi", ".scr", ".com", ".pif", ".vbs", ".js",
}

BLOCKED_CONTENT_TYPES = {
    "application/x-executable",
    "application/x-msdos-program",
    "application/x-msdownload",
}

def is_file_allowed(filename: str, content_type: str) -> bool:
    """ファイル転送が許可されているか確認"""
    ext = Path(filename).suffix.lower()
    if ext in BLOCKED_EXTENSIONS:
        return False
    if content_type in BLOCKED_CONTENT_TYPES:
        return False
    return True
```

#### 10.8 WebSocketイベント一覧

| イベント | 方向 | 説明 |
|---------|------|------|
| `FILE_UPLOAD` | C→S | ファイルアップロード要求 |
| `FILE_RECEIVED` | S→C | ファイル受信通知（URL付き） |
| `FILE_ERROR` | S→C | ファイル転送エラー |

**FILE_ERRORペイロード:**
```json
{
  "op": 0,
  "t": "FILE_ERROR",
  "d": {
    "error_code": "file_too_large",
    "message": "File exceeds 8MB limit",
    "filename": "large_video.mp4"
  }
}
```

**エラーコード一覧:**

| コード | 説明 |
|--------|------|
| `file_too_large` | ファイルサイズ超過（8MB超） |
| `blocked_file_type` | 禁止されたファイル形式 |
| `upload_failed` | Discord API エラー |
| `unauthorized` | 権限不足 |

---

## 進め方

1. 各タスクの「検討項目」を議論
2. 決定した内容を「決定事項」に記載
3. 決定後、`implementation_discord.md` に詳細セクションを追記
4. ステータスを「完了」に更新
