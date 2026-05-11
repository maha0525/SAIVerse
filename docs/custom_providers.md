# カスタムプロバイダの追加ガイド

SAIVerse では、UI から OpenAI 互換 / Ollama 互換 のプロバイダを追加して、
任意の LLM サーバー / API に接続できます。

このガイドでは代表的なケースとして **LM Studio**, **llama.cpp サーバー**,
**Kimi (Moonshot AI)** の追加手順を説明します。

## 前提

- SAIVerse 本体が起動していること
- 接続先サーバー / API が起動 / 利用可能であること

## 共通手順

1. グローバル設定モーダルを開く（左下のギアアイコン）
2. サイドバーから **「モデル管理」** タブを選択
3. サブタブ **「プロバイダ」** を選択
4. 右上の **「+ 新規追加」** ボタンを押す
5. プロトコル / Base URL / API キー環境変数名を入力
6. **保存** を押す
7. 次に **「モデル」** サブタブで、このプロバイダを使う個別モデルを追加

API キーが必要な場合は、グローバル設定 > 環境タブ > サーバー環境変数で
API キーの値そのものを設定します（プロバイダ JSON には環境変数名のみ書く）。

---

## ケース 1: LM Studio（ローカル）

LM Studio はデスクトップアプリで、ローカルの GGUF モデルを OpenAI 互換 API
として配信します。デフォルトでは `http://localhost:1234/v1` で動作。

### プロバイダ追加

| フィールド | 値 |
|-----------|------|
| ID | `lmstudio` |
| 表示名 | `LM Studio (local)` |
| プロトコル | `OpenAI 互換` |
| Base URL | `http://localhost:1234/v1` |
| API キー環境変数名 | （空欄で OK、認証不要のため） |

保存後、「接続テスト」を押すと LM Studio で公開中のモデル一覧が表示されます。

### モデル追加

「モデル」サブタブ → 「+ 新規追加」→ JSON エディタに以下を入力:

```json
{
  "model": "qwen2.5-7b-instruct-q4_k_m",
  "display_name": "Qwen 2.5 7B (LM Studio)",
  "provider_ref": "lmstudio",
  "context_length": 32768,
  "parameters": {
    "temperature": { "type": "slider", "min": 0, "max": 2, "step": 0.1, "default": 0.7, "label": "温度" }
  }
}
```

`model` の値は LM Studio 側の正確なモデル ID を入れます（接続テスト時に
取得した一覧からコピーすると確実）。

キーは任意の英数字で、ファイル名（`~/.saiverse/user_data/models/<key>.json`）
として使われます。

---

## ケース 2: llama.cpp サーバー

`llama-server`（llama.cpp 同梱）は OpenAI 互換 API を提供します。
デフォルトでは `http://127.0.0.1:8080/v1`。

### プロバイダ追加

| フィールド | 値 |
|-----------|------|
| ID | `llama-cpp-server` |
| 表示名 | `llama.cpp Server` |
| プロトコル | `OpenAI 互換` |
| Base URL | `http://127.0.0.1:8080/v1` |
| API キー環境変数名 | （空欄） |

### モデル追加

llama-server はサーバー起動時にロードしたモデル 1 個だけを公開するので、
`model` フィールドは何でも構いません（llama-server 側で無視される）。

```json
{
  "model": "loaded-model",
  "display_name": "Llama 3 70B (llama.cpp)",
  "provider_ref": "llama-cpp-server",
  "context_length": 8192
}
```

---

## ケース 3: Kimi (Moonshot AI)

Kimi (Moonshot AI) はクラウド API で、OpenAI 互換のエンドポイントを
公開しています。

### 1. API キーを環境変数に設定

グローバル設定 > **環境** タブ > サーバー環境変数 (.env) のセクションを開く。

`KIMI_API_KEY` を新規追加して値（Moonshot 発行の API キー）を入力 → 保存
→ サーバーを再起動。

### 2. プロバイダ追加

| フィールド | 値 |
|-----------|------|
| ID | `kimi` |
| 表示名 | `Kimi (Moonshot AI)` |
| プロトコル | `OpenAI 互換` |
| Base URL | `https://api.moonshot.cn/v1` |
| API キー環境変数名 | `KIMI_API_KEY` |

### 3. モデル追加

```json
{
  "model": "moonshot-v1-128k",
  "display_name": "Kimi v1 128k",
  "provider_ref": "kimi",
  "context_length": 131072,
  "parameters": {
    "temperature": { "type": "slider", "min": 0, "max": 1, "step": 0.05, "default": 0.3 }
  }
}
```

`model` の正確な ID は [Moonshot 公式ドキュメント](https://platform.moonshot.cn/docs)
で確認してください。

---

## トラブルシュート

### 接続テストで「Connection failed」

- Base URL が間違っている可能性。末尾の `/v1` の有無を確認
- ローカルサーバーが起動しているか確認
- ファイアウォール / ウイルス対策ソフトがブロックしていないか確認
- リモートサーバーの場合、ポート開放 / SAIVerse から到達可能か確認

### 接続テストで「HTTP 401 Unauthorized」

- API キー環境変数が設定されていない、または間違っている
- 環境変数の値を更新したら、SAIVerse サーバーを再起動

### 「Connection test not supported for protocol」

- builtin のネイティブプロトコル（anthropic_native, gemini_native など）は
  接続テスト対象外。これらは `/api/config/models` で「利用可能モデル」として
  表示されることで動作確認

### モデルが UI のドロップダウンに出てこない

- API キー環境変数が設定されているか確認（`is_model_available` でフィルタされる）
- `POST /api/config/reload-models` を呼ぶか、SAIVerse を再起動
- ブラウザのキャッシュクリア / リロード

### プロバイダの設定を変更したら全モデルに反映されない

- プロバイダ更新後、`POST /api/providers/reload` で provider 設定を再読み込み
- model_configs はサーバー起動時 / `POST /api/config/reload-models` で再評価
  されるため、プロバイダ変更後はモデルもリロードする

---

## さらに詳しく

- 設計の意図と不変条件: `docs/intent/model_provider_management.md`
- API リファレンス: `api/routes/providers.py`, `api/routes/config.py`
- builtin プロバイダの例: `builtin_data/providers/`
- builtin モデルの例: `builtin_data/models/`
