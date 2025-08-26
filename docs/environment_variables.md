# 環境変数一覧

このドキュメントは、SAIVerse で利用されている主な環境変数をカテゴリー別に整理したものです。重複や意味が近い変数も多いため、将来的な統合を見据えて役割を明確に記載します。

## LLM プロバイダ・API キー
| 変数 | 用途 | 主な参照 |
| ---- | ---- | ---- |
| `LLM_PROVIDER` | 既定のLLMプロバイダ選択 (`gemini`, `openai`, `ollama` など) | `integrations/cognee_memory.py`, 各種スクリプト |
| `SAIVERSE_KW_LLM_PROVIDER` | キーワード経由でのLLMプロバイダ指定。`LLM_PROVIDER` と役割が重複 | `integrations/cognee_memory.py` |
| `GEMINI_API_KEY` | Gemini 有料APIキー | 多数のモジュール |
| `GEMINI_FREE_API_KEY` | Gemini 無料APIキー | 多数のモジュール |
| `OPENAI_API_KEY` | OpenAI APIキー | `integrations/cognee_memory.py` など |
| `SAIVERSE_GEMINI_KEY_PREF`, `SAIVERSE_GEMINI_KEY_PREFERENCE` | Geminiキーの優先順位。名称が二種類存在 | `integrations/cognee_memory.py` |
| `SAIVERSE_ASSIGN_LLM_BACKEND` | ペルソナ毎に利用するLLMバックエンドを指定 | `memory_core/config.py` など |
| `SAIVERSE_ASSIGN_LLM_MODEL` | ペルソナ毎のLLMモデル名 | `memory_core/config.py` など |
| `SAIVERSE_ASSIGN_GEMINI_MODEL` | Gemini利用時のモデル指定 | `memory_core/config.py` など |
| `SAIVERSE_ASSIGN_LLM_K` | Assign LLM の近傍数など補助パラメータ | `memory_core/config.py` |
| `OLLAMA_BASE_URL`, `OLLAMA_HOST` | Ollama サーバ URL | `llm_clients.py` ほか |
| `SAIVERSE_RAW_LLM_LOG` | LLM 生レスポンスのログ出力先 | `llm_clients.py` |

## 埋め込みモデル設定
| 変数 | 用途 | 主な参照 |
| ---- | ---- | ---- |
| `SAIVERSE_EMBED_PROVIDER` | MemoryCore の埋め込みプロバイダ | `memory_core/config.py` |
| `SAIVERSE_EMBED_MODEL` | MemoryCore の埋め込みモデル | `memory_core/config.py` |
| `SAIVERSE_EMBED_DIM` | 埋め込み次元数 | `memory_core/config.py` |
| `SAIVERSE_EMBED_DEVICE` | 埋め込み計算に用いるデバイス | `memory_core/config.py` |
| `SAIVERSE_EMBED_NORMALIZE` | 埋め込みの正規化指定 | `memory_core/config.py` |
| `SAIVERSE_EMBED_MAX_BATCH` | Cognee 埋め込み時のバッチ上限 | `integrations/cognee_memory.py` |
| `SAIVERSE_EMBED_BATCH_SLEEP_MS` | バッチ間スリープ(ms) | `integrations/cognee_memory.py` |
| `SAIVERSE_EMBED_EMPTY_PLACEHOLDER` | 空テキスト時のプレースホルダ | `integrations/cognee_memory.py` |
| `EMBEDDING_DIMENSIONS` | Cognee側で参照される汎用次元指定 | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_GEMINI_EMBED_MODEL` | Gemini埋め込みモデル | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_GEMINI_EMBED_DIM` | Gemini埋め込み次元 | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_OPENAI_EMBED_MODEL` | OpenAI埋め込みモデル | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_OPENAI_EMBED_DIM` | OpenAI埋め込み次元 | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_HF_EMBED_MODEL` | HuggingFace埋め込みモデル | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_HF_EMBED_DIM` | HuggingFace埋め込み次元 | `integrations/cognee_memory.py` |
| `HUGGINGFACE_TOKENIZER` | Cognee用トークナイザ指定 | `integrations/cognee_memory.py` |

> **整理案**: `SAIVERSE_COGNEE_*_EMBED_MODEL`/`*_EMBED_DIM` はプロバイダに依存するため、`EMBEDDING_PROVIDER` と汎用の `SAIVERSE_COGNEE_EMBED_MODEL` / `SAIVERSE_COGNEE_EMBED_DIM` へ統合すると管理が簡潔になる。

## Cognee 動作制御
| 変数 | 用途 | 主な参照 |
| ---- | ---- | ---- |
| `SAIVERSE_COGNEE_GEMINI_MODEL` | Cogneeで利用するGemini LLM | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_OPENAI_MODEL` | Cogneeで利用するOpenAI LLM | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_AUTOCG` | Cogneeの自動Graph生成をON/OFF | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_SKIP_EDGE_INDEX` | Edge Index生成をスキップ | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_PROFILE_TASKS` | タスク処理のプロファイル出力 | `integrations/cognee_memory.py` |
| `SAIVERSE_COGNEE_PROFILE_LLM` | LLM呼び出しのプロファイル出力 | `integrations/cognee_memory.py` |
| `SAIVERSE_DISABLE_LANCEDB_FILTER` | LanceDBフィルタリング無効化 | `integrations/cognee_memory.py` |
| `DATA_ROOT_DIRECTORY`, `SYSTEM_ROOT_DIRECTORY` | Cogneeデータ/システムルート | `integrations/cognee_memory.py` |

## Qdrant・メモリ関連
| 変数 | 用途 | 主な参照 |
| ---- | ---- | ---- |
| `SAIVERSE_MEMORY_BACKEND` | MemoryCore のバックエンド指定 | `memory_core/config.py` |
| `QDRANT_URL`, `QDRANT_HOST` | Qdrant サービスのURL/ホスト | `memory_core/config.py` |
| `QDRANT_API_KEY` | Qdrant APIキー | `memory_core/config.py` |
| `QDRANT_LOCATION` | Qdrantロケーション | `memory_core/config.py` |
| `QDRANT_COLLECTION_PREFIX` | Qdrantコレクションの接頭辞 | `memory_core/config.py`, `scripts/memory_smoke.py` |
| `SMOKE_PREFIX` | テスト用コレクション名 | `scripts/memory_smoke.py` |

## サービスURL・その他
| 変数 | 用途 | 主な参照 |
| ---- | ---- | ---- |
| `SDS_URL` | Directory Service のURL | `main.py`, `saiverse_manager.py` |
| `SAIVERSE_THIRDPARTY_LOG_LEVEL` | 外部ライブラリのログレベル | `integrations/cognee_memory.py`, `scripts/check_cognee_env.py` |
| `SAIVERSE_LOG_PATH` | 一般ログファイルの出力先 | `tools/defs/calculator.py` |

---

上記以外にもスクリプトやテスト専用の変数が存在しますが、コア機能で利用する主なものは上記の通りです。重複している変数（例: `SAIVERSE_GEMINI_KEY_PREF` と `SAIVERSE_GEMINI_KEY_PREFERENCE`）や役割が近い変数は、上記の整理案に沿って統合を検討してください。
