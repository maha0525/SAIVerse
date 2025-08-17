# Repository Guidelines

## プロジェクト構成 / モジュール
- ルート: `main.py`（Gradio UI）、中核ロジック（`saiverse_manager.py`、`llm_clients.py`、`memory_core/`）。
- データ/API: `database/`（SQLAlchemy、APIサーバ、マイグレーション、シード、SQLite）。
- ツール: `tools/`（Function Calling 定義）、プロンプトは `system_prompts/`。
- スクリプト/テスト: `scripts/`（メモリ関連ユーティリティ）、`tests/`（unittest）。
- アセット/ログ: `assets/`、`raw_llm_responses.txt`、`saiverse_log.txt`。

## ビルド・実行・テスト
- 依存関係: `pip install -r requirements.txt`
- UI 起動: `python main.py`
- ディレクトリサービス(SDS): `python sds_server.py`
- DB API 起動: `python database/api_server.py --port 8001`
- DB 初期化（再作成）: `python database/seed.py`
- DB マイグレーション: `python database/migrate.py --db database/saiverse.db`
- テスト一括: `python -m unittest discover tests`
- 単体テスト: `python -m unittest tests/test_llm_clients.py`

## コーディング規約 / 命名
- Python 3.11+、PEP8、インデント4スペース。公開関数に簡潔なdocstring。
- 命名: 関数/変数は `snake_case`、クラスは `CamelCase`、定数は `UPPER_CASE`。
- テスト命名: `tests/test_*.py`。ドメイン毎にディレクトリを分割（例: `memory_core/`, `database/`）。
- フォーマット: 本リポジトリに強制ツールなし。インポート整列、1行≤100桁を目安。
- 注意: プロンプトは `str.format()` を使用。リテラルの `{}` は `{{ }}` でエスケープ。

## テスト指針
- フレームワーク: `unittest`。ネットワーク/LLMはモック化（例: `tests/test_llm_clients.py`）。
- 新規機能は近接するテストを追加し、決定論的に。カバレッジは主要分岐を意識。

## コミット / PR ガイド
- コミットは簡潔に（日本語/英語可、命令形推奨）。Conventional Commitsは必須ではありません。
- 例: `fix: topic名の正規化` / `refactor(db): migrate schema`。
- PR には目的、変更概要、テスト方法、起動手順を記載。UI変更はスクリーンショット歓迎。
- 影響範囲のタグ付け: DB（`database/*`）、API（`sds_server.py`, `database/api_server.py`）。

## セキュリティ / 設定
- 秘密情報は `.env` に設定（`OPENAI_API_KEY`、`GEMINI_API_KEY` / `GEMINI_FREE_API_KEY`）。コミット禁止。
- ローカルDB: `database/saiverse.db`。マイグレーション時にバックアップ自動作成。
- LLM接続: 環境により OpenAI/Gemini/Ollama を選択。開発では Free キー優先でフォールバック。

## 言語方針
- 既定の応答・ドキュメントは日本語で記述します（このファイルを含む）。
- 変数名/コード/コメントは英語で問題ありません。コミットメッセージは日英いずれも可。
