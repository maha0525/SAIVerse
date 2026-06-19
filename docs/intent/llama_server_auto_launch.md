# llama.cpp サーバー自動起動

## 目的

llama.cpp をサーバーモード (`openai_compat` プロトコル) で使う場合、サーバーの手動起動が必要だった。モデル設定に起動情報を持たせることで、サーバーが立っていなければ自動で起動し、使い終わったら SAIVerse 終了時に自動で停止する。

## 2つの起動モード

### コマンドモード

既存の起動スクリプト (.bat / .sh) をそのまま使う。JSON には `command` だけ書けばいい。

```json
"llama_server": {
    "command": "C:/path/to/start_model.bat"
}
```

- `llama_server_binary`, `model_path`, `extra_args` 等は全て無視される
- .bat ファイルの場合、末尾の `pause` は自動除外して実行される
- バイナリやモデルの変更はスクリプト側で行う

### ビルドアップモード

スクリプトがない場合、個別フィールドからコマンドラインを組み立てる。

```json
"llama_server": {
    "model_path": "~/models/model.gguf",
    "n_gpu_layers": -1,
    "parallel": 2,
    "extra_args": ["--mmproj", "~/models/mmproj.gguf", "--jinja"]
}
```

## 設計判断

### なぜプロバイダ + モデルの二層設定か

- **バイナリパス** (`llama_server_binary`) はプロバイダレベル。同じ llama-server バイナリを複数モデルで共有するため
- **起動パラメータ** (`llama_server`) はモデルレベル。GGUF パス・GPU レイヤー数・並列スロット数はモデル固有

`_INHERITABLE_FIELDS` により、プロバイダの `llama_server_binary` がモデル設定に自動継承される。ビルドアップモードのみ使用。

### なぜ管理下/管理外を区別するか

ポートに既にサーバーが応答している場合、それがユーザーが手動で立てたものかもしれない。管理下プロセス (自分が起動したもの) だけ再起動対象とし、管理外プロセスには触れない。

### 設定変更の検知

管理下プロセスの identity (コマンドモード: スクリプトパス、ビルドアップモード: model_path) を保持し、設定と一致しなければ再起動する。

## 不変条件

1. `llama_server` 設定がないモデルには一切影響しない (回帰なし)
2. 管理外プロセスを kill / restart しない
3. SAIVerse 終了時に管理下プロセスを全て停止する
4. ヘルスチェック待ちは最大 120 秒 (大型モデルのロード時間を考慮)
5. サーバーログはセッションログディレクトリに `llama_server_{port}.log` として出力
6. `command` 指定時、ビルドアップ用フィールドは全て無視 (排他)

## 設定例

### コマンドモード (既存スクリプト活用)

```json
{
  "model": "gemma4-31b",
  "provider_ref": "llama_cpp_server",
  "context_length": 128000,
  "llama_server": {
    "command": "C:/Users/user/llama/start_gemma4_31B.bat"
  }
}
```

### ビルドアップモード (フルスペック指定)

プロバイダ:
```json
{
  "id": "llama_cpp_server",
  "display_name": "llama.cpp Server",
  "protocol": "openai_compat",
  "base_url": "http://127.0.0.1:8080/v1",
  "llama_server_binary": "/path/to/llama-server"
}
```

モデル:
```json
{
  "model": "qwen2.5-72b-q8",
  "display_name": "Qwen 2.5 72B Q8 (llama.cpp)",
  "provider_ref": "llama_cpp_server",
  "context_length": 32768,
  "llama_server": {
    "model_path": "~/models/qwen2.5-72b-q8.gguf",
    "n_gpu_layers": -1,
    "parallel": 2,
    "extra_args": ["--slot-save-path", "~/.saiverse/llama_cache"]
  }
}
```

## バイナリ解決順 (ビルドアップモードのみ)

1. `llama_server.binary` (モデル設定内)
2. `llama_server_binary` (プロバイダから継承)
3. 環境変数 `LLAMA_SERVER_BINARY`
4. PATH 上の `llama-server`

## 関連ファイル

- `llm_clients/llama_server.py` — `LlamaServerManager` 実装
- `llm_clients/factory.py` — `openai_compat` ブランチでのフック
- `saiverse/model_configs.py` — `_INHERITABLE_FIELDS` への追加
- `main.py` — shutdown フック
- `llm_clients/llama_cache.py` — KV スロットキャッシュ (自動起動と直交、併用可能)
