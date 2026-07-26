# 環境変数

`.env`（リポジトリルート）で設定する。ここは**よく使う変数の抜粋**で、正の一覧は [`.env.example`](../../.env.example) を参照（コード内には他にも多数の `os.getenv` 参照がある）。

## LLM API キー / 接続

| 変数 | 説明 |
|---|---|
| `GEMINI_API_KEY` | Google Gemini（有料枠。推奨） |
| `GEMINI_FREE_API_KEY` | Gemini 無料枠用 |
| `OPENAI_API_KEY` | OpenAI |
| `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` | Anthropic Claude |
| `NVIDIA_API_KEY` | NVIDIA NIM |
| `XAI_API_KEY` | xAI (Grok) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `MOONSHOT_API_KEY` | Moonshot (Kimi) |
| `OLLAMA_BASE_URL` | ローカル Ollama サーバー URL |

プロバイダごとの `api_key_env` は [プロバイダ一覧](providers.md) を参照。

## SAIMemory / 記憶

| 変数 | 既定 | 説明 |
|---|---|---|
| `SAIMEMORY_EMBED_MODEL` | `intfloat/multilingual-e5-small` | 埋め込みモデル |
| `SAIMEMORY_EMBED_MODEL_PATH` | - | ローカル埋め込みモデルのパス |
| `SAIMEMORY_EMBED_CUDA` | 自動 | `1`=GPU 強制 / `0`=CPU 強制 / 未設定=自動 |
| `SAIMEMORY_MEMORY` | `on` | 記憶機能の ON/OFF |
| `SAIMEMORY_MEMORY_LAST_MESSAGES` | `40` | 直近何件を文脈に載せるか |
| `SAIMEMORY_MEMORY_SEMANTIC_RECALL` | `true` | セマンティック想起 |
| `SAIMEMORY_MEMORY_TOPK` | `5` | 想起の上位件数 |
| `SAIMEMORY_MEMORY_RANGE_BEFORE` / `_AFTER` | `2` | 想起ヒット前後の取得件数 |
| `SAIMEMORY_MEMORY_CHUNK_MIN_CHARS` / `_MAX_CHARS` | `120` / `480` | チャンク文字数 |
| `SAIVERSE_RECALL_SNIPPET_MAX_CHARS` 他 | `8000` | 想起スニペットの最大文字数（通常/stream/pulse） |
| `MEMORY_WEAVE_MODEL` | `gemini-3.1-flash-lite-preview` | Chronicle/Memopedia 生成・編纂 (curation) モデル。ペルソナ別 `MEMORY_WEAVE_MODEL` (DB) が優先 (`saiverse/memory_weave_llm.py`) |
| `MEMORY_WEAVE_BATCH_SIZE` | `20` | **廃止 (W4)** — 生成は episode 整列 + サイズ束ねに世代交代 (`SAIVERSE_CHRONICLE_BAND_BUDGET` 系へ)。設定しても無視される |
| `MEMORY_WEAVE_CONSOLIDATION_SIZE` | `10` | **廃止 (W4)** — 統合は列のあふれ束ねに世代交代。設定しても無視される |
| `MEMORY_WEAVE_MAINTAIN_INTERVAL` | `200` | メンテ間隔 |
| `SAIVERSE_CHRONICLE_BAND_BUDGET` | `10000` | 一次あらすじチャンクの標準被覆字数 U (体験の構造 §4-6・§11-8 のモック検証値)。整列計画・退場計画の基準単位。**束ねの発火には使われない** (2026-07-27 世代交代 — 発火は `SAIVERSE_CHRONICLE_CHAR_BUDGET` の 1/4、[chronicle_consolidation](../intent/chronicle_consolidation.md) §3) |
| `SAIVERSE_CHRONICLE_BAND_BASE` | `10` | 次数の底 B (幾何級数)。次数 k の標準被覆 = U×B^(k-1)。**束ねの発火・相手選びには使われない** (同上 — 現在はコスト概算のみが参照) |
| `SAIVERSE_CHRONICLE_MIN_DIGEST_CHARS` | `1000` | LLM 圧縮する最小被覆字数。未満は恒等圧縮 (生のまま一次あらすじに置く、LLM なし) |
| `SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN` | `3` | 1 回の Metabolism で実行する束ね+治療の LLM コール上限 (LLM コスト暴走防止の安全弁) |
| `SAIVERSE_CHRONICLE_CHAR_BUDGET` | `20000` | weave の General Chronicle 読み込みの文字数予算。超過時は年表を粗いレベルへ畳んで全期間をカバーする（最古を落とさない）。**この 1/4 が束ねの発火閾値 X を兼ねる** ([chronicle_consolidation](../intent/chronicle_consolidation.md) §3 — 発火と提示を同じノブに連動させる)。記憶アーキv2 §6.2 |
| `SAIVERSE_GOLD_PANNING_ENABLED` | `1` | 砂金採り（Metabolism 時のコア記憶採取）の全体トグル。`0` で無効（defer-to-hot ごと従来挙動に戻る）。intent `gold_panning.md` |
| `SAIVERSE_GOLD_PANNING_PENDING_CAP` | `1.5` | defer-to-hot 圧力弁。ウィンドウが high watermark のこの倍率を超えたらキャッシュが冷たくても Metabolism を実行する |
| `SAIVERSE_GOLD_PANNING_CLOSE_MIN_MESSAGES` | `10` | セッションクローズ時採取のスキップ下限（新規メッセージがこれ未満なら採取しない。Phase 3） |
| `SAIVERSE_MEDIA_RECALL_ENABLED` | `false` | 添付メディア（画像/音声/動画）の概要を自動想起の検索クエリに使うか。ON 時は添付があると概要生成を同期実行するため数秒待ちが発生する。UI（グローバル設定 > 環境）からも切替可 |

## バックアップ

| 変数 | 既定 | 説明 |
|---|---|---|
| `SAIVERSE_DB_BACKUP_ON_START` | `true` | 起動時に saiverse.db をバックアップ（推奨） |
| `SAIVERSE_DB_BACKUP_KEEP` | `10` | 保持するバックアップ数 |
| `SAIMEMORY_BACKUP_ON_START` | `true` | 起動時に persona memory.db を rdiff-backup |

## モデル / ランタイム

| 変数 | 説明 |
|---|---|
| `SAIVERSE_DEFAULT_MODEL` | 既定モデル（persona 未設定時のフォールバック） |
| `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` | 既定の軽量モデル |
| `GEMINI_TIMEOUT_SECONDS` | Gemini タイムアウト（既定 180） |
| `SAIVERSE_ATTACHMENT_LIMIT` | 添付上限（既定 4） |
| `SAIVERSE_DISABLE_GEMINI_STREAMING` / `_SSE_PATCH` | Gemini ストリーミング関連のフォールバック制御 |

## ログ / デバッグ

| 変数 | 説明 |
|---|---|
| `SAIVERSE_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `SAIVERSE_SEA_TRACE` | `1` で SEA Playbook 実行トレースを `sea_trace.log` に詳細出力 |
| `SAIVERSE_SUBLINE_SCHEDULER_ENABLED` | SubLineScheduler の有効/無効（既定有効） |
| `SAIVERSE_LLM_CONTEXT_DUMP` | LLM コンテキストダンプ先ファイル |

## パス / テスト

| 変数 | 説明 |
|---|---|
| `SAIVERSE_HOME` | `~/.saiverse` の上書き先 |
| `SAIVERSE_USER_DATA_DIR` | user_data ディレクトリの上書き（テスト用） |
| `SDS_URL` | SDS の URL（既定 `http://127.0.0.1:8080`） |

## Discord ゲートウェイ

| 変数 | 説明 |
|---|---|
| `SAIVERSE_GATEWAY_ENABLED` | `1` で有効 |
| `SAIVERSE_GATEWAY_WS_URL` / `SAIVERSE_GATEWAY_TOKEN` | 接続先とトークン |
| `SAIVERSE_GATEWAY_CHANNEL_MAP` | チャンネルマッピング（JSON） |

## メール送信（SMTP）

| 変数 | 説明 |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` | SMTP サーバー（既定 `smtp.gmail.com` / `587`） |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | 認証 |
| `SMTP_FROM` / `SMTP_USE_TLS` | 送信元 / TLS |
