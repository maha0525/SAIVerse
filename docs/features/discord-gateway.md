# Discord連携

SAIVerseとDiscordを接続する「Discord Gateway」について説明します。

## 概要

Discord Gatewayを使用すると、DiscordチャンネルとSAIVerseのBuildingをリアルタイムで接続できます。Discord上の会話がSAIVerseに反映され、ペルソナの発言がDiscordに送信されます。

## 構成

```
┌─────────────┐     WebSocket     ┌─────────────────┐
│  SAIVerse   │◄─────────────────►│ Discord Gateway │
│  main.py    │                   │   (Cloudflare)  │
└─────────────┘                   └────────┬────────┘
                                           │
                                           ▼
                                  ┌─────────────────┐
                                  │   Discord Bot   │
                                  └─────────────────┘
```

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r discord_gateway/requirements-dev.txt
```

### 2. 環境変数の設定

`.env` に追加：

```env
SAIVERSE_GATEWAY_WS_URL=ws://127.0.0.1:8787/ws
SAIVERSE_GATEWAY_TOKEN=your-secret-token
```

### 3. Gatewayの起動

```bash
cd discord_gateway
# ローカルテスト用
npx wrangler dev
```

## 機能

### メッセージ同期

- Discord → SAIVerse: チャンネルのメッセージがBuilding内に反映
- SAIVerse → Discord: ペルソナの発言がチャンネルに送信

### 訪問者

Discordユーザーを「訪問者」としてBuilding内に表示。

### コマンド

Discord上で使用できるBotコマンド（詳細は `discord_gateway/docs/` を参照）。

## イベント

`GatewayMixin` が以下のイベントを処理：

| イベント | 説明 |
|----------|------|
| `visitor_enter` | 訪問者がBuildingに参加 |
| `visitor_leave` | 訪問者がBuildingから退出 |
| `message` | メッセージの受信 |
| `memory_sync` | 記憶の同期 |

## 詳細ドキュメント

Discord Gateway の詳細な設定手順は `discord_gateway/docs/` を参照してください。

## 次のステップ

- [都市間連携](./inter-city.md) - マルチCity構成
