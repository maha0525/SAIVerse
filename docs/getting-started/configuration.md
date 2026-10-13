# 設定

SAIVerseの設定オプションを説明します。

## 環境変数

`.env` ファイルで設定します。よく使うものを抜粋。完全な一覧は [reference/environment-vars.md](../reference/environment-vars.md) を参照。

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
| `SAIMEMORY_MEMORY_LAST_MESSAGES` | 40 | 文脈に載せる直近メッセージ数 |

想起に使う埋め込みモデル (EmbeddingGemma 2、CPU で動作) は固定で、設定項目はありません。セットアップ時 (失敗した場合は初回起動時) に `sbert/embeddinggemma-2-q4/` へ自動でダウンロードされます。

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
| `<city_id>` | 起動する City（位置引数、既定 `city_a`） |
| `--db-file PATH` | データベースファイルのパス |
| `--sds-url URL` | ディレクトリサービスのURL |

> ポートは `cities.json` で City ごとに設定する（既定は `city_a`=8000 の1つ。別 City を使うには `cities.json` / DB に追加が必要）。個別ポート指定の起動引数はない。

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
- `supports_sampling_parameters`: `temperature` / `top_p` / `top_k` の送信対応（Gemini 3.6 Flash / 3.5 Flash-Lite 以降の非対応モデルでは `false`）
- `supports_assistant_prefill`: Anthropic の assistant 発話で終わるコンテキストへの対応（Haiku 5.5 では `false`）。非対応時は履歴を書き換えず、送信前に `invalid_request` で停止
- `supports_model_prefill`: 非空のモデル発話で終わるコンテキストへの対応（同モデル以降では `false`）
- `timeout`: 1 回の API 呼び出しの待ち時間の上限（秒）。OpenAI 互換・OpenAI Codex・Gemini で効く（NVIDIA NIM は `request_kwargs.timeout`）。Gemini では環境変数 `GEMINI_TIMEOUT_SECONDS` より優先
- `cache.explicit_supported`: Gemini の明示キャッシュ（`caches.create`）に対応するか（省略時 `true`）。`false` のモデル（Gemini 4 Argon）では自動キャッシュ・継続キャッシュを選んでいてもキャッシュを作らず、Google 側の暗黙キャッシュだけで動く
- `cache.persist_realtime_and_recall`: リアルタイム情報（現在時刻・事前実行スペルの結果）と自動想起（ふと浮かんだ記憶）を、送った時点でペルソナの記録に残し、次の応答以降も同じ位置に同じ文面で送るか（省略時 `false`）。`true` にすると送る内容が毎回後ろに足されるだけになり、前回の送信が今回の送信の先頭とそのまま一致するので、キャッシュの境目をこちらから指定できないモデルでもキャッシュが当たる。組み込みでは Gemini 4 Argon だけが `true`。境目を指定できるモデル（Claude、明示キャッシュの Gemini）では得が無く、残した分だけ記憶の整理が早まるので `false` のままにする。`true` のモデルでは、送る画像の枚数の上限（`SAIVERSE_ATTACHMENT_LIMIT` やモデル設定の `max_image_embeds`）を使わず、会話の画像をすべて画像のまま送る（上限で古い画像が文字に置き換わると、前回の送信の中身が書き換わるため）。設計は `docs/intent/persist_realtime_and_recall.md`
- `base_url`: カスタムエンドポイント（互換API用）
- `api_key_env`: APIキーの環境変数名
- `parameters`: 温度・top_pなどのパラメータ制約

`supports_sampling_parameters: false` のモデルでは、`parameters` に sampling 項目を載せず、上位処理から値が渡された場合もプロバイダが API request から除外します。`supports_model_prefill: false` のモデルに対して末尾が非空の model role となった場合は、role や本文を自動改変せず、送信前に `invalid_request` として停止します。

> 新しいモデルは、接続情報を `provider_ref` でプロバイダ定義から参照する形が推奨（→ [reference/providers.md](../reference/providers.md)）。追加・編集は グローバル設定 > モデル管理 タブからも行える（→ [グローバル設定](../user-guide/global-settings.md)）。

## 次のステップ

- [基本概念](../concepts/README.md) - システムの仕組み
