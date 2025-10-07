# sai_memory

Mastraのワークフローを参考にしつつ、自前で組み直した長期記憶エージェント実装です。
- 環境変数でOpenAI/Geminiを切替（Google公式SDK google.genai 使用）
- システムプロンプト（環境変数/ファイル）
- SQLite永続メモリ＋FastEmbedによるセマンティック想起
- スレッド概要（要約）生成・503リトライ
- ログインポート（JSON/JSONL）・安全な再インデックス（スロットリング）
- 診断・デバッグロギング（JSON）

## クイックスタート

1) Python 3.10 以上

2) 依存インストール:
```
pip install -r sai_memory/requirements.txt
```

3) .env を用意:
```
cp sai_memory/.env.example .env
```

4) スレッドに対して1回のプロンプトを実行:
```
python -m sai_memory.cli --thread t1 --resource demo --input "計画の要点をまとめて"
```

5) ログのインポート（JSON/JSONL。配列 or 1行1JSON）：
```
python sai_memory/scripts/import_logs.py path/to/logs.jsonl --resource demo
```
オブジェクト形式: {thread_id, role, content, resource_id?, created_at?}

6) 埋め込みの再インデックス（スロットリングあり）:
```
python sai_memory/scripts/reindex_embeddings.py --chunk 100 --sleep-ms 200
```

7) 設定とDBの診断:
```
python sai_memory/scripts/diag.py
```

## 環境変数（主要）

- プロバイダ/モデル
  - `LLM_PROVIDER`: `openai` もしくは `google`
  - `LLM_MODEL`: 例 `gpt-5`, `gemini-2.0-flash`
  - `OPENAI_API_KEY`, `GEMINI_API_KEY`（`GOOGLE_API_KEY`も可）
  - Gemini利用時のPython SDK: `google-genai`（import: `from google import genai`）

- 振る舞い
  - `SAIMEMORY_TEMPERATURE`: 生成温度
  - `SAIMEMORY_SYSTEM_PROMPT` または `SAIMEMORY_SYSTEM_PROMPT_FILE`
  - `SAIMEMORY_RESOURCE_ID`: 既定のリソースID

- メモリ/想起
  - `SAIMEMORY_DB_PATH`: 既定は `memory.db`（実行CWDに作成）
  - `SAIMEMORY_MEMORY`: `true|false` メモリ機能のマスターON/OFF
  - `SAIMEMORY_MEMORY_LAST_MESSAGES`: 直近N件を常に含める（既定 8）
  - `SAIMEMORY_MEMORY_SEMANTIC_RECALL`: セマンティック想起ON/OFF
  - `SAIMEMORY_MEMORY_TOPK`: 想起上位K件（既定 5）
  - `SAIMEMORY_MEMORY_RANGE_BEFORE`/`SAIMEMORY_MEMORY_RANGE_AFTER`: 文脈展開（各既定 1）
  - `SAIMEMORY_MEMORY_SCOPE`: `thread|resource`（既定 `resource`）

- 概要（要約）
  - `SAIMEMORY_SUMMARY`: 概要生成を有効化
  - `SAIMEMORY_SUMMARY_USE_LLM`: LLMを実際に呼び出して概要作成
  - `SAIMEMORY_SUMMARY_PRERUN`: 応答前に事前生成
  - `SAIMEMORY_SUMMARY_MAX_CHARS`: 概要入力に使う最大文字数（既定 1200）

- デバッグ
  - `SAIMEMORY_DEBUG`: `true|false`。有効時にJSONログをstderrへ出力

## 補足
- ツール/関数呼び出し（function calling）は未実装。必要に応じて拡張可能。
- 埋め込みは `fastembed` を使用。類似度はPython側で計算（可搬性重視）。
- `.env` の配置場所に関わらず、`SAIMEMORY_DB_PATH` 未指定時は実行ディレクトリ（CWD）に `memory.db` が作られます。
