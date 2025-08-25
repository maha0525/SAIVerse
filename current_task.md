# コンテキスト復旧メモ（2025-08-23）

以下は、直前の作業状態および次アクション候補の復元メモです。

## ブランチ / 変更状況
- ブランチ: `memory_update`（remote: `origin/memory_update`）
- 未コミット変更（modified）:
  - `persona_core.py`: MemoryCore → CogneeMemory 差し替え、会話→記憶→リコール連携（CHUNKS優先）
  - `saiverse_manager.py`: グローバルモデル解除時の処理整備（DBデフォルト復元）。
  - `requirements.txt`: `cognee>=0.2.3` 追加。
- 未追跡（untracked）:
  - `integrations/cognee_memory.py`（Cogneeアダプタ実装）
  - `.qdrant/`, `ai_sessions/test_core/`, `integrations/`, `sbert/` 等

## 直近のログ/痕跡
- `raw_llm_responses.txt`: 2025-08-23 00:07 までの会話ログあり（長期記憶まわりへの言及多数）。
- `saiverse_log.txt`: 定常ログのみ（電卓・Gemini呼び出しの履歴など）。
- `database/saiverse.db`: タイムスタンプ 2025-08-23 00:07 に更新（UI/会話実行の痕跡）。

## 実装中タスクの推定
- 長期記憶統合（Cognee）
  - `persona_core` から `self.memory_core.remember/recall()` を呼び出し、
    - remember: 逐次インジェスト＋cognifyはバックグラウンド実行（`LLM_API_KEY` がある場合のみ）。
    - recall: `SearchType.CHUNKS` でLLM不要検索を優先、結果をプロンプト追補情報として埋め込み。
  - Cognee未導入でも安全にスキップ（ログ出力＋空結果で運用継続）。

## テスト状況の参考（ローカル試行）
- 一部テストは通過、ネットワーク依存箇所は環境制約で失敗（想定内）。
- `memory_core` 既存のユニットは `qdrant` ロック競合により一部エラーの可能性（InMemoryStorageフォールバック痕跡あり）。

## 次アクション候補
1) 依存関係の整備
   - `pip install -r requirements.txt`（`cognee` 導入）。
   - LLM利用時は `.env` に `LLM_API_KEY`（および `GEMINI_API_KEY`/`GEMINI_FREE_API_KEY`）を設定。
2) 動作確認
   - UI: `python main.py`
   - SDS: `python sds_server.py`
   - DB API: `python database/api_server.py --port 8001`
3) 仕様微調整（任意）
   - `integrations/` に `__init__.py` を追加してパッケージ解決を明示（現状でも名前空間パッケージで動作可）。
4) コミット
   - 変更点をまとめてコミット（メッセージ例: `feat(memory): Cognee統合の初期実装`）。

---
不整合/クラッシュの疑い

## Cognee（LiteLLM）× Gemini 運用メモ（追加）
- 背景: Cognee は `.env` を pydantic-settings で直接読み、LLM と Embedding を別々に構成します。既定は OpenAI（`gpt-4o-mini`, `text-embedding-3-large`）。
- 本リポのアダプタは、呼出し直前のスレッドで環境パッチを当て、Cognee の設定キャッシュをクリアして再読込させる実装に変更済み。
- Gemini で動かす最小構成:
  - `LLM_PROVIDER=gemini`
  - `GEMINI_FREE_API_KEY` または `GEMINI_API_KEY`
  - 省略時の既定は以下（必要なら上書き）
    - `SAIVERSE_COGNEE_GEMINI_MODEL=gemini/gemini-2.0-flash`
    - `SAIVERSE_COGNEE_GEMINI_EMBED_MODEL=gemini/text-embedding-004`
    - `SAIVERSE_COGNEE_GEMINI_EMBED_DIM=768`
- OpenAI で動かす場合（参考）:
  - `OPENAI_API_KEY`
  - （任意）`SAIVERSE_COGNEE_OPENAI_MODEL=openai/gpt-4o-mini`
  - （任意）`SAIVERSE_COGNEE_OPENAI_EMBED_MODEL=openai/text-embedding-3-large`
- 注意点:
  - LiteLLM で Vertex AI 誤解釈を避けるため、モデル名は `gemini/...` / `openai/...` のプリフィクス付きで明示。
  - 初回はデータセット未作成で `DatasetNotFoundError` が出ることがありますが、`add/cognify` 通過後に解消します。

## ロギング抑制（追加）
- Cognee/LiteLLM の冗長ログを抑えるため、アダプタ初期化で以下の設定を適用:
  - `litellm`, `litellm.litellm_core_utils.litellm_logging`, `httpx`, `httpcore` を WARNING にし、`propagate=False`。
  - `LANGFUSE_SDK_DISABLED=1` を既定で付与（Langfuse 初期化メッセージの抑止）。
- これにより、長いスタックトレース（例: `apscheduler` 未導入時の内部ログ）でログが埋まるのを軽減します。
- `persona_core.py` は現在整合しており、関数境界の途切れは見当たりません。
- Cognee未導入環境でも例外を握りつぶし→スキップする設計のため、Cognee未導入が直接クラッシュ要因にはなりにくい想定です。
- 直前クラッシュが再現する場合は、再現ログ（トレースバック）を共有ください。該当箇所を特定して恒久対応します。
