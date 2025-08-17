# Scripts Guide

このディレクトリは、SAIVerseの開発・検証用ユーティリティをまとめています。アプリ本体とは独立して実行できます。

## 前提
- 依存関係: `pip install -r requirements.txt`
- APIキー（必要に応じて）:
  - Gemini: `GEMINI_FREE_API_KEY` または `GEMINI_API_KEY`
  - Ollama HTTP: `OLLAMA_BASE_URL`（任意）
- 既定のベクタDBは Qdrant（ローカル埋め込み）。場所は `~/.saiverse/qdrant`（変更は `--location-base`）。

## ingest_persona_log.py
- 目的: `~/.saiverse/personas/<id>/log.json`（または `--file` 指定）を per-persona メモリDBへ取り込み、トピック割当を行う。
- 主な引数:
  - `persona_id`: ペルソナID（DB分離に使用）
  - `--assign-llm`: `dummy|ollama_http|ollama_cli|gemini|none`
  - `--file`: 明示パスを指定（既定はホーム配下）
  - `--start`: 1始まりの開始位置（既定: 1）
  - `--limit`: 取り込み件数（未指定で末尾まで）
  - `--location-base`: DB格納先（例: `~/.saiverse/qdrant`）
  - `--collection-prefix`: コレクション接頭辞（`<prefix>_<persona>` で分離）
  - `--conv-id`: 既定は `persona:<id>`
- 例:
  - 最初の100件だけ（Gemini割当）: `python scripts/ingest_persona_log.py eris --assign-llm gemini --start 1 --limit 100`
  - 続き（101〜200件）: `python scripts/ingest_persona_log.py eris --assign-llm gemini --start 101 --limit 100`
  - 任意ファイルから: `python scripts/ingest_persona_log.py eris --file ~/export/log.json --assign-llm gemini`
  - 割当無効（ヒューリスティック）: `--assign-llm none`

環境変数のヒント:
- 汎用: `SAIVERSE_ASSIGN_LLM_BACKEND`, `SAIVERSE_ASSIGN_LLM_MODEL`
- Gemini専用モデル指定: `SAIVERSE_ASSIGN_GEMINI_MODEL`（例: `gemini-2.0-flash`）

## recall_persona_memory.py
- 目的: per-persona DB からキーワードで想起を確認。
- 主な引数: `persona_id` `query` `--topk` `--location-base` `--collection-prefix` `--json`
- 例: `python scripts/recall_persona_memory.py eris "旅行 温泉" --topk 8 --json`

## rename_generic_topics.py
- 目的: 「新しい話題」など汎用/空タイトルのトピックを一括リネーム。
- 例: プレビュー `python scripts/rename_generic_topics.py eris --dry-run`、本適用はフラグなし。

## reassign_fallback_entries.py
- 目的: ダミーLLM（`fallback_dummy`）で記憶されたエントリを抽出し、指定のアサインLLMで再割当して置き換えリンクを付ける。
- 主な引数:
  - `persona_id`: ターゲットのペルソナID
  - `--conv-id`: 特定の会話スレッドのみに限定
  - `--assign-llm`: `gemini` 推奨（`ollama_http`/`ollama_cli` も可）
  - `--limit`: 再処理件数の上限
  - `--dry-run`: 変更せず計画のみ表示
  - `--location-base`, `--collection-prefix`: DB指定
- 例: `python scripts/reassign_fallback_entries.py eris --assign-llm gemini --limit 50`

## memory_topics_ui.py
- 目的: ブラウザで per-persona メモリーのトピック全体像を閲覧。
- 起動: `python scripts/memory_topics_ui.py`
- 入力: Persona ID、Location Base（例 `~/.saiverse/qdrant`）、Collection Prefix（例 `saiverse`）

## ヒント
- Qdrantの場所は `--location-base` で切替可。複数環境を使い分けたい場合に便利です。
- Gemini割当使用時はキー必須。未設定時は安全にダミー割当へフォールバックします。
