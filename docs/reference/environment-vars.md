# 環境変数

SAIVerseの環境変数一覧です。`.env` ファイルで設定します。

## LLM APIキー

| 変数名 | 必須 | 説明 |
|--------|:----:|------|
| `GEMINI_API_KEY` | 推奨 | Google Gemini API（有料枠） |
| `GEMINI_FREE_API_KEY` | 任意 | Gemini無料枠用 |
| `OPENAI_API_KEY` | 任意 | OpenAI GPT系モデル |
| `CLAUDE_API_KEY` | 任意 | Anthropic Claude |
| `OLLAMA_BASE_URL` | 任意 | ローカルOllamaサーバーURL |

## SAIMemory

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SAIMEMORY_EMBED_MODEL` | `intfloat/multilingual-e5-base` | 埋め込みモデル |
| `SAIMEMORY_EMBED_MODEL_PATH` | - | ローカルモデルのパス |
| `SAIMEMORY_EMBED_MODEL_DIM` | 768 | 埋め込み次元数 |
| `SAIMEMORY_LAST_MESSAGES` | 20 | 想起時の最大メッセージ数 |
| `SAIMEMORY_BACKUP_ON_START` | false | 起動時に自動バックアップ |
| `SAIMEMORY_RDIFF_PATH` | - | rdiff-backupバイナリのパス |

## ネットワーク

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SDS_URL` | `http://127.0.0.1:8080` | ディレクトリサービスURL |

## ログ

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SAIVERSE_LOG_LEVEL` | `INFO` | ログレベル |
| `SAIVERSE_CHAT_HISTORY_LIMIT` | 120 | チャット履歴保持ターン数 |

## Discord Gateway

| 変数名 | 説明 |
|--------|------|
| `SAIVERSE_GATEWAY_WS_URL` | Gateway WebSocket URL |
| `SAIVERSE_GATEWAY_TOKEN` | ハンドシェイクトークン |

## 例

```env
# LLM（少なくとも1つ設定）
GEMINI_API_KEY=AIzaXXXXXXXX
OPENAI_API_KEY=sk-XXXXXXXX
CLAUDE_API_KEY=sk-ant-XXXXXXXX

# SAIMemory
SAIMEMORY_EMBED_MODEL=intfloat/multilingual-e5-base
SAIMEMORY_EMBED_MODEL_PATH=/path/to/model

# ネットワーク
SDS_URL=http://127.0.0.1:8080

# ログ
SAIVERSE_LOG_LEVEL=DEBUG

# Discord（オプション）
SAIVERSE_GATEWAY_WS_URL=ws://127.0.0.1:8787/ws
SAIVERSE_GATEWAY_TOKEN=secret-token
```
