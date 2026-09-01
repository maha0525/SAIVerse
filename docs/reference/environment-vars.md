# 環境変数

`.env`（リポジトリルート）で設定する。ここは**よく使う変数の抜粋**で、書き方の見本は [`.env.example`](../../.env.example)（コード内には他にも多数の `os.getenv` 参照がある）。

**「既定」の欄はコードが未設定時に使う値**（`.env.example` が同梱している値とは一致しないことがある。差がある行にはその旨を書いてある）。**読み手のいない変数はこの表に載せない** — 設定しても何も起きない変数を一覧に残すと、効かない設定を書いて原因を探す時間が生まれるため。⚠ `.env.example` 側には読み手を失ったキーがまだ残っている（2026-08-22 時点で `SAIVERSE_LLM_CONTEXT_DUMP` / `MEMORY_WEAVE_MAINTAIN_INTERVAL` など）ので、**「.env.example に書いてある＝効く」ではない**。効くかどうかの正はこの表とコードの `os.getenv`。

## LLM API キー / 接続

| 変数 | 説明 |
|---|---|
| `GEMINI_API_KEY` | Google Gemini（有料枠。推奨） |
| `GEMINI_FREE_API_KEY` | Gemini 無料枠用 |
| `OPENAI_API_KEY` | OpenAI |
| `CLAUDE_API_KEY` | Anthropic Claude。**キーを読むのはこの名前だけ**（`builtin_data/providers/anthropic.json` の `api_key_env` と `llm_clients/anthropic.py` の両方）。`ANTHROPIC_API_KEY` は初期セットアップウィザードの「キーが入っているか」の判定（`api/routes/tutorial.py`）でしか見られないので、これだけ設定しても実際の呼び出しは失敗する |
| `NVIDIA_API_KEY` | NVIDIA NIM |
| `XAI_API_KEY` | xAI (Grok) |
| `OPENROUTER_API_KEY` | OpenRouter |
| `OLLAMA_BASE_URL` | ローカル Ollama サーバー URL（未設定なら `OLLAMA_HOST` を見る） |

プロバイダごとの `api_key_env` は [プロバイダ一覧](providers.md) を参照。Kimi (Moonshot) のようにユーザーが自分で追加するプロバイダのキー名は、そのプロバイダ設定の `api_key_env` に書いた名前がそのまま使われる（組み込みの決め打ちは無い。手順は [custom_providers.md](../custom_providers.md) ケース 3）。

## SAIMemory / 記憶

| 変数 | 既定 | 説明 |
|---|---|---|
| `SAIMEMORY_EMBED_MODEL` | `intfloat/multilingual-e5-small` | 埋め込みモデル |
| `SAIMEMORY_EMBED_MODEL_PATH` | - | ローカル埋め込みモデルのパス |
| `SAIMEMORY_EMBED_CUDA` | 自動 | `1`=GPU 強制 / `0`=CPU 強制 / 未設定=自動 |
| `SAIMEMORY_MEMORY` | `on` | 記憶機能の ON/OFF |
| `SAIMEMORY_MEMORY_LAST_MESSAGES` | `8` | 直近何件を文脈に載せるか（`.env.example` は `40` を同梱） |
| `SAIMEMORY_MEMORY_SEMANTIC_RECALL` | `true` | セマンティック想起 |
| `SAIMEMORY_MEMORY_TOPK` | `5` | 想起の上位件数 |
| `SAIMEMORY_MEMORY_RANGE_BEFORE` / `_AFTER` | `1` | 想起ヒット前後の取得件数（`.env.example` は各 `2` を同梱） |
| `SAIMEMORY_MEMORY_CHUNK_MIN_CHARS` / `_MAX_CHARS` | `120` / `480` | チャンク文字数 |
| `SAIVERSE_RECALL_SNIPPET_MAX_CHARS` | `8000` | 想起スニペットの最大文字数（通常）。stream / pulse は別名で `SAIVERSE_RECALL_SNIPPET_STREAM_MAX_CHARS`（`800`）/ `SAIVERSE_RECALL_SNIPPET_PULSE_MAX_CHARS`（`1200`） |
| `MEMORY_WEAVE_MODEL` | `gemini-3.1-flash-lite-preview` | Chronicle/Memopedia 生成・編纂 (curation) モデル。ペルソナ別 `MEMORY_WEAVE_MODEL` (DB) が優先 (`saiverse/memory_weave_llm.py`) |
| `MEMORY_WEAVE_BATCH_SIZE` | `20` | **本体では廃止 (W4)** — 生成は整列 + サイズ束ねに世代交代 (`SAIVERSE_CHRONICLE_BAND_BUDGET` 系へ)。本番経路では無視され、読むのは一括生成スクリプト `scripts/arasuji/build_arasuji_core.py` だけ |
| `MEMORY_WEAVE_CONSOLIDATION_SIZE` | `10` | **本体では廃止 (W4)** — 統合は列のあふれ束ねに世代交代。読み手は上と同じくスクリプトのみ |
| `SAIVERSE_CHRONICLE_BAND_BUDGET` | `10000` | 一次あらすじチャンクの標準被覆字数 U (体験の構造 §4-6・§11-8 のモック検証値)。整列計画・退場計画の基準単位 (`sai_memory/arasuji/alignment.py`)。**束ねの発火には使われない** (2026-07-27 世代交代 — 発火は `SAIVERSE_CHRONICLE_CHAR_BUDGET` の 1/4、[chronicle_consolidation](../intent/chronicle_consolidation.md) §3) |
| `SAIVERSE_CHRONICLE_MAX_BAND_CONSOLIDATIONS_PER_RUN` | `3` | 1 回の Metabolism で実行する束ね+治療の LLM コール上限 (LLM コスト暴走防止の安全弁、`sai_memory/arasuji/bands.py`) |
| `SAIVERSE_CHRONICLE_CHAR_BUDGET` | `20000` | weave の General Chronicle 読み込みの文字数予算。超過時は年表を粗いレベルへ畳んで全期間をカバーする（最古を落とさない）。**この 1/4 が束ねの発火閾値 X を兼ねる** ([chronicle_consolidation](../intent/chronicle_consolidation.md) §3 — 発火と提示を同じノブに連動させる)。記憶アーキv2 §6.2 |
| `SAIVERSE_SLUICE_ENABLED` | `1` | スルース（Metabolism 時のコア記憶・手帳メモ・約束の採取。旧 gold_panning）の全体トグル。`0` で無効（defer-to-hot ごと従来挙動に戻る。無効時は採取なしで退場が進む）。intent `gold_panning.md`（旧名のまま）+ `autonomous_behavior_v3.md` §13 |
| `SAIVERSE_SLUICE_PENDING_CAP` | `1.5` | defer-to-hot 圧力弁。ウィンドウが high watermark のこの倍率を超えたらキャッシュが冷たくても Metabolism を実行する |
| （旧 `SAIVERSE_GOLD_PANNING_*`） | — | **非推奨**（2026-08-19 の sluice 改名で置換）。上 2 つと同名対応（`ENABLED` / `PENDING_CAP`）の旧キーは、新キー未設定のときだけフォールバックとして読まれ、使用時に WARNING が出る（旧 `ENABLED=0` の環境が更新後に黙って採取を再開しないための設定移行）。優先順は 新キー > 旧キー > 既定。`SAIVERSE_SLUICE_*` へ移行すること |
| `SAIVERSE_MEDIA_RECALL_ENABLED` | `false` | 添付メディア（画像/音声/動画）の概要を自動想起の検索クエリに使うか。ON 時は添付があると概要生成を同期実行するため数秒待ちが発生する。UI（グローバル設定 > 環境）からも切替可 |

## バックアップ

| 変数 | 既定 | 説明 |
|---|---|---|
| `SAIVERSE_DB_BACKUP_ON_START` | `true` | 起動時に saiverse.db をバックアップ（推奨） |
| `SAIVERSE_DB_BACKUP_KEEP` | `10` | 保持するバックアップ数 |
| `SAIMEMORY_BACKUP_ON_START` | `true` | 起動時に persona memory.db を rdiff-backup |

## RSS フィード取り込み

詳細: [`docs/intent/rss_feed_intake.md`](../intent/rss_feed_intake.md)

| 変数 | 既定 | 説明 |
|---|---|---|
| `SAIVERSE_FEED_FETCH_INTERVAL_SEC` | `1800` | フィード定期取得の間隔（秒）。起動時にも一度取得する |
| `SAIVERSE_FEED_FETCH_TIMEOUT` | `15` | 取得 1 リクエストのタイムアウト（秒）。不正値は既定へ |
| `SAIVERSE_FEED_CYCLE_BUDGET_SEC` | `300` | 取得サイクル全体（全購読の逐次取得）の壁時計予算（秒）。超過で残り購読の取得を打ち切る（取得済みぶんの表示更新・配送・剪定は実行）。0 以下で無制限 |
| `SAIVERSE_FEED_MAX_BYTES` | `10485760` | フィード応答の最大サイズ（バイト）。超過は取得失敗 |
| `SAIVERSE_FEED_MAX_ITEMS_PER_PUSH` | `3` | 1 回の配送でペルソナの知覚に積む新着記事数の上限。0 以下で配送無効 |
| `SAIVERSE_FEED_MAX_PENDING` | `10` | ペルソナの知覚バッファに未消化のまま溜められるフィード記事数の上限。到達中は配送を見送る |
| `SAIVERSE_FEED_ITEM_KEEP` | `200` | 購読ごとに保存する記事数の上限（古い側から剪定）。0 以下で剪定無効 |
| `SAIVERSE_FEED_MAX_SUBSCRIPTIONS_PER_FIXTURE` | `10` | 施設 1 つが持てる購読数の上限 |
| `SAIVERSE_FEED_ALLOW_PRIVATE` | 未設定 | `1` でローカル/プライベート宛フィード URL の取得を許可（自宅サーバー等の上級者向け。既定は SSRF 防止のため拒否） |

## モデル / ランタイム

| 変数 | 説明 |
|---|---|
| `SAIVERSE_DEFAULT_MODEL` | 既定モデル（persona 未設定時のフォールバック） |
| `SAIVERSE_DEFAULT_LIGHTWEIGHT_MODEL` | 既定の軽量モデル |
| `GEMINI_TIMEOUT_SECONDS` | Gemini タイムアウト（既定 180） |
| `SAIVERSE_ATTACHMENT_LIMIT` | 添付上限（既定 4） |
| `SAIVERSE_DISABLE_GEMINI_STREAMING` / `SAIVERSE_DISABLE_GEMINI_SSE_PATCH` | Gemini ストリーミング関連のフォールバック制御（`llm_clients/gemini.py`） |
| `SAIVERSE_GEMINI_AUTO_CACHE` | Gemini 自動キャッシュ（実験的、既定 `0`）。`1` にすると全ての Gemini 呼び出しで explicit cache を自動作成し、入力トークンをキャッシュ価格にする。UI（グローバル設定 > 環境）からも切替可で、切替は再起動なしで反映される |
| `SAIVERSE_GEMINI_AUTO_CACHE_KEEP_SECONDS` | 自動キャッシュを応答後に何秒残すか（既定 `0`、範囲 0〜3600）。`0` は応答直後に削除（従来挙動。保険 TTL 300 秒で作成し、削除に失敗しても 5 分で消える）。`1` 以上はその秒数を TTL にして作成し、手動削除せず Gemini 側の失効に任せる（残っているあいだキャッシュ保存料金がかかる）。⚠️ TTL 内でのキャッシュ再利用は未実装のため、現状 `1` 以上は保存料金を払うだけで得がない（[issue](../issues/gemini_auto_cache_no_reuse_within_ttl.md)） |

## ログ / デバッグ

| 変数 | 説明 |
|---|---|
| `SAIVERSE_LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `SAIVERSE_SEA_TRACE` | `1` で SEA Playbook 実行トレースを `sea_trace.log` に詳細出力 |

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
