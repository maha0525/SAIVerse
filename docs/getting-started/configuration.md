# 設定

SAIVerseの設定オプションを説明します。

## 環境変数

`.env` ファイルで設定します。

### LLM APIキー

| 変数名 | 必須 | 説明 |
|--------|:----:|------|
| `GEMINI_API_KEY` | 推奨 | Google Gemini API（有料枠） |
| `GEMINI_FREE_API_KEY` | 任意 | Gemini無料枠用 |
| `OPENAI_API_KEY` | 任意 | OpenAI GPT-5/4o/o3など |
| `CLAUDE_API_KEY` | 任意 | Anthropic Claude |
| `OLLAMA_BASE_URL` | 任意 | ローカルOllamaサーバー |

> **ヒント**: 少なくとも1つのAPIキーが必要です。Geminiを推奨します。

### SAIMemory関連

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SAIMEMORY_EMBED_MODEL` | `intfloat/multilingual-e5-base` | 埋め込みモデル |
| `SAIMEMORY_EMBED_MODEL_PATH` | - | ローカルモデルのパス |
| `SAIMEMORY_LAST_MESSAGES` | 20 | 想起時の最大メッセージ数 |

### ネットワーク

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SDS_URL` | `http://127.0.0.1:8080` | ディレクトリサービスのURL |

### ログ

| 変数名 | デフォルト | 説明 |
|--------|-----------|------|
| `SAIVERSE_LOG_LEVEL` | `INFO` | ログレベル（DEBUG/INFO/WARNING/ERROR） |
| `SAIVERSE_CHAT_HISTORY_LIMIT` | 120 | チャット履歴の保持ターン数 |

### Discord連携（オプション）

| 変数名 | 説明 |
|--------|------|
| `SAIVERSE_GATEWAY_WS_URL` | Discord Gatewayの接続先 |
| `SAIVERSE_GATEWAY_TOKEN` | ハンドシェイクトークン |

## コマンドライン引数

`main.py` の起動オプション：

```bash
python main.py <city_id> [オプション]
```

| オプション | 説明 |
|-----------|------|
| `--db-file PATH` | データベースファイルのパス |
| `--ui-port PORT` | フロントエンド用ポート |
| `--api-port PORT` | APIサーバーのポート |
| `--sds-url URL` | ディレクトリサービスのURL |

## モデル設定

`builtin_data/models/` ディレクトリ内の個別JSONファイルでLLMモデルを定義します。ユーザーカスタムモデルは `~/.saiverse/user_data/models/` に配置すると、組み込み設定より優先されます。

```json
{
  "model": "gemini-2.5-flash-preview-05-20",
  "display_name": "Gemini 2.5 Flash",
  "provider": "gemini",
  "context_length": 1000000,
  "supports_images": true,
  "parameters": {
    "temperature": { "default": 1.0, "min": 0, "max": 2.0 }
  }
}
```

各エントリで指定可能なフィールド：
- `model`: API呼び出し時のモデルID（必須）
- `display_name`: UIドロップダウンに表示する名前
- `provider`: `openai` / `anthropic` / `gemini` / `ollama`
- `context_length`: コンテキスト長
- `supports_images`: 画像入力対応
- `base_url`: カスタムエンドポイント（互換API用）
- `api_key_env`: APIキーの環境変数名
- `parameters`: 温度・top_pなどのパラメータ制約

## 次のステップ

- [アーキテクチャ](../concepts/architecture.md) - システムの仕組み
