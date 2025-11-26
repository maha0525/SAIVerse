# 🧩 SAIVerse

SAIVerse は、建物 (Building) と都市 (City) の概念で構成された仮想世界に複数の AI ペルソナを配置し、ユーザー／他都市との対話や自律行動を観察・開発できるフルスタック環境です。Gradio 製 UI、マルチ LLM・ツール連携、永続的な記憶層（SAIMemory + MemoryCore）、都市間ディレクトリサービス (SDS) などをひとつのリポジトリで扱えます。

## Highlights

- **Multi-city orchestration** – `saiverse_manager.py` が SQLite に格納された建物・ペルソナ情報を読み込み、`conversation_manager.py` でパルス駆動の自律会話、`occupancy_manager.py` で移動／定員制御、`manager/*.py` で SDS・履歴・訪問者ワークフローを担います。
- **統合 UI** – `ui/app.py` が World View、Autonomous Log、DB Manager、Task Manager、Memory Settings、World Editor を 1 つの Gradio アプリにまとめ、ユーザーは Building 移動・召喚・自律制御・DB 編集・タスク確認・記憶インポートを行えます。
- **LLM + tools ハブ** – `llm_clients/` が OpenAI (GPT-5/4.1/4o), Anthropic (Claude 4.x, thinking extensions), Google Gemini 2.5/2.0, Ollama を抽象化し、`llm_router.py` が Gemini 2.0 Flash (google-genai) でツール呼び出し是非を判定、`tools/defs/` が計算・画像生成・アイテム操作・タスク管理などの Function Calling を提供します。
- **長期記憶とトピック** – `saiverse_memory/adapter.py` と `sai_memory/` がペルソナ単位のログ／記憶 DB を `~/.saiverse/personas/<id>/` に保持し、`memory_core/` が SBERT + Qdrant によるトピック化・再想起・再編成を行います。各種スクリプトで ChatGPT/TXT ログのインポートやバックアップを自動化できます。
- **Inter-city travel & remote proxies** – `database/api_server.py` の FastAPI エンドポイント + `sds_server.py` の Directory Service により都市間でペルソナを派遣。`VisitingAI` / `RemotePersonaProxy` / `ThinkingRequest` を介してリモート都市でも自律思考を継続し、帰還時に記憶差分を同期します。
- **Discord gateway (任意)** – `discord_gateway/` ランタイムを有効化すると、Discord 上の会話と SAIVerse の建物を WebSocket で連結し、訪問者やユーザーの発言をリアルタイムで世界に反映できます。
- **充実した保守スクリプト群** – `scripts/` 以下に SAIMemory バックアップ・トピック整形・タスク生成・Qdrant 管理・Discord テストなどの CLI を収録。

## Component map

| Layer | 主なモジュール | 役割 |
| --- | --- | --- |
| Entry & UI | `main.py`, `ui/app.py`, `assets/css/chat.css` | マネージャ起動、API サーバ spawn、Gradio UI 構築、CSS テーマ |
| World orchestration | `saiverse_manager.py`, `conversation_manager.py`, `occupancy_manager.py`, `manager/*.py`, `buildings.py` | Building ロード、占有管理、パルス実行、SDS/訪問者/履歴/ブループリント/管理系サービス |
| Persona runtime | `persona/core.py`, `action_handler.py`, `emotion_module.py`, `ai_sessions/*`, `persona/tasks/*` | PersonaCore 実装、`::act` 解析、感情パラメータ制御、タスクストレージ |
| Memory stack | `saiverse_memory/adapter.py`, `sai_memory/*`, `memory_core/*`, `scripts/memory_*.py` | ログ→SQLite→Qdrant 連携、トピック割当/再編成、バックアップ・可視化・再学習 |
| LLM & tools | `llm_clients/*`, `llm_router.py`, `model_configs.py`, `tools/`, `tools/context.py`, `action_priority.json` | モデル選択・フォールバック、Gemini ルーター、Function Calling schema、ツール実体 |
| Data & network | `database/models.py`, `database/api_server.py`, `database/seed.py`, `sds_server.py`, `remote_persona_proxy.py`, `discord_gateway/*` | SQLite schema、API サーバ、初期データ、SDS、訪問者プロキシ、Discord ブリッジ |
| Utilities & tests | `scripts/*`, `docs/*`, `tests/*`, `current_task.md` | ドキュメント、CLI、ユニットテスト、進行中タスク共有 |

## Repository layout

```text
SAIVerse/
├── main.py                     # エントリーポイント (Gradio + manager 起動)
├── saiverse_manager.py         # 世界のオーケストレーター
├── buildings.py                # Building モデルとローダー
├── action_handler.py           # ::act JSON の実行
├── llm_router.py               # Gemini 2.0 Flash を用いたツールルーター
├── assets/                     # CSS・アイコン・アバター
├── database/                   # SQLite モデル、API サーバ、seed/migrate、data/
├── manager/                    # SDS, history, blueprint, admin, visitor などの mixin
├── persona/                    # PersonaCore 実体・ミックスイン・タスク管理
├── ui/                         # Gradio UI (world view, editors, memory, tasks)
├── tools/                      # Tool registry・計算/画像/タスク/アイテム/スレッド操作ツール
├── llm_clients/                # OpenAI / Anthropic / Gemini / Ollama クライアント
├── saiverse_memory/, sai_memory/, memory_core/  # 長期記憶スタック
├── scripts/                    # SAIMemory やタスク関連 CLI
├── docs/                       # アーキテクチャ / DB 設計 / テスト / リリースマニュアル
├── discord_gateway/            # Discord 連携ランタイム
├── system_prompts/, prompts/   # 共通・建物・感情プロンプト資産
├── ai_sessions/                # ペルソナごとの初期セッション定義
├── tests/                      # unittest ベースの自動テスト
├── generate_image/             # 画像生成ツールの出力先
└── *.py / *.json / logs        # 各種補助スクリプト・ログ
```

> **保存先メモ**: 実行時の永続データは `database/data/saiverse.db` と `~/.saiverse/`（ペルソナログ・記憶・タスク・添付ファイル）に保存されます。画像生成記録は `generate_image/`、LLM 生ログは `raw_llm_responses.txt`、一般ログは `saiverse_log.txt` に追記されます。

## Requirements & dependencies

- Python 3.11+
- pip / venv
- `pip install -r requirements.txt`（FastAPI, google-genai 1.26+, gradio 5.38, openai 1.97, qdrant-client, sentence-transformers, fastembed, torch, rdiff-backup など）
- **Embeddings**: `sbert/` 配下に SBERT スナップショット（例: `intfloat/multilingual-e5-base`）を置くとオフライン利用が高速になります
- **Qdrant**: embedded モード (default: `~/.saiverse/qdrant`) か外部 Qdrant サーバ (`QDRANT_URL`) を用意
- **rdiff-backup**: `scripts/backup_saimemory.py` で差分バックアップを取る場合に必要
- **Discord gateway (任意)**: `pip install -r discord_gateway/requirements-dev.txt` を追加実行
- **psutil (任意)**: UI ポート占有プロセス検出 (`main.py`) に利用

## Setup

1. **Clone & venv**
   ```bash
   git clone https://github.com/maha/SAIVerse.git
   cd SAIVerse
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```
2. **Install Python deps**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   # Discord gateway を使う場合
   pip install -r discord_gateway/requirements-dev.txt
   ```
3. **Prepare `.env`** – 下記「Environment quick reference」を参照して API キーやログ設定を記述（`python-dotenv` により自動読込）
4. **Seed database (初回またはリセット時)**  
   `python database/seed.py` を実行すると `database/data/saiverse.db` が `cities.json` に基づいて再生成されます
5. **(推奨) SBERT モデル配置** – `sbert/` に推論済みモデルを展開（例: `sbert/intfloat/multilingual-e5-base/`）
6. **(任意) SDS や Qdrant を別プロセスで起動** – 詳細は後述

### Environment quick reference

| Key | 必須 | 説明 |
| --- | :---: | --- |
| `OPENAI_API_KEY` | 任意 | GPT-5/4o/4.1, o3 など OpenAI モデル用 |
| `GEMINI_API_KEY` | 推奨 | Gemini 2.5 Pro/Flash, 2.0 Flash, 1.5 Flash（有料枠） |
| `GEMINI_FREE_API_KEY` | 任意 | 無料枠 (rate limit 高め) 用 API キー |
| `CLAUDE_API_KEY` | 任意 | Claude 4.5 Sonnet / Opus 4 など |
| `OLLAMA_BASE_URL` | 任意 | ローカル Ollama サーバ (無指定で自動プローブ) |
| `SDS_URL` | 任意 | Directory Service の URL (default `http://127.0.0.1:8080`) |
| `SAIVERSE_LOG_LEVEL` | 任意 | `DEBUG / INFO / ...` (default INFO) |
| `SAIVERSE_CHAT_HISTORY_LIMIT` | 任意 | Gradio チャットの保持ターン数 (default 120) |
| `SAIMEMORY_BACKUP_ON_START` | 任意 | true の場合、起動時に rdiff-backup を自動実行 |
| `SAIMEMORY_EMBED_MODEL(_PATH/_DIM)` | 任意 | fastembed / SBERT モデル設定 |
| `QDRANT_LOCATION` or `QDRANT_URL` | 任意 | MemoryCore 用 Qdrant の保存先 (embedded) もしくはサーバ URL |
| `SAIMEMORY_RDIFF_PATH` | 任意 | `rdiff-backup` バイナリのフルパス |
| `SAIVERSE_GATEWAY_WS_URL` / `SAIVERSE_GATEWAY_TOKEN` | 任意 | Discord ゲートウェイ接続先とハンドシェイクトークン |

Example `.env`:

```env
OPENAI_API_KEY=sk-xxxx
GEMINI_API_KEY=AIzaPaidKey
GEMINI_FREE_API_KEY=AIzaFreeKey
CLAUDE_API_KEY=sk-ant-xxxx
OLLAMA_BASE_URL=http://127.0.0.1:11434
SDS_URL=http://127.0.0.1:8080
SAIVERSE_LOG_LEVEL=DEBUG
SAIMEMORY_BACKUP_ON_START=true
SAIMEMORY_EMBED_MODEL=intfloat/multilingual-e5-base
SAIMEMORY_EMBED_MODEL_PATH=/home/user/models/multilingual-e5-base
QDRANT_LOCATION=~/.saiverse/qdrant
SAIMEMORY_RDIFF_PATH=/usr/bin/rdiff-backup
SAIVERSE_GATEWAY_WS_URL=ws://127.0.0.1:8787/ws
SAIVERSE_GATEWAY_TOKEN=super-secret-token
```

### Database & city presets

- `cities.json` で都市ごとの UI/ API ポート・DB ファイル名を管理（default: `city_a` UI=8000/API=8001, `city_b` UI=9000/API=9001）
- `python database/seed.py` は `database/data/saiverse.db` を再生成し、ユーザー (USERID=1)、都市、建物 (user_room, deep_think_room, 創造の祭壇, private rooms)、初期ペルソナ (air, eris, genesis...) を登録します
- 既存 DB を保ちたい場合は `--db-file` で別パスを `main.py` に渡すか、`database/data/saiverse.db` をバックアップしてから seed を実行してください

### Model registry (`models.json`)

`models.json` は UI で選択可能なモデル一覧を定義します。各エントリは `provider` (`openai` / `anthropic` / `gemini` / `ollama`)、`context_length`、画像サポート、Anthropic thinking (`thinking_type`, `thinking_budget`) などを指定できます。追加モデルを使いたい場合はここに追記し、必要に応じてローカル推論環境 (Ollama など) を整えてください。

## Running SAIVerse

1. **(Optional) Start SDS**
   ```bash
   python sds_server.py
   ```
   - デフォルト: `http://127.0.0.1:8080`
   - 他都市と LAN/WAN 越しに連携する場合は公開サーバに配置し、`SDS_URL` または `python main.py --sds-url ...` で指定

2. **Launch a city instance**
   ```bash
   python main.py city_a
   # 例: データベースを指定する場合
   python main.py city_a --db-file database/data/saiverse.db --sds-url http://127.0.0.1:8080
   ```
   - `main.py` は `SAIVerseManager` と `database/api_server.py` を起動し、UI ポート (City.UI_PORT) で Gradio を立ち上げます
   - デフォルト seed の場合: `city_a` → http://127.0.0.1:8000、`city_b` → http://127.0.0.1:9000
   - API サーバ (City.API_PORT) はリモート都市からの `/inter-city` / `/persona-proxy` リクエストを受け付けます
   - 終了は Ctrl+C。`main.py` が API サーバと SDS ハートビートスレッドをクリーンアップします

3. **First-run artifacts**
   - `~/.saiverse/` 以下に `personas/<id>/log.json`, `memory.db`, `tasks.db`, `attachments`, `cities/<city>/buildings/<building>/log.json` などが生成されます
   - `generate_image/` に画像ファイル、`saiverse_log.txt` / `raw_llm_responses.txt` にログが追記されます

## UI tour

- **World View (チャット + Building 移動)** – 現在地の履歴を表示し、テキスト or 画像添付で発話。`move_user_radio_ui` / `move_user_ui` で Building を切り替え、召喚/帰宅ドロップダウンでペルソナを呼び出し or 帰還させます
- **Autonomous conversation log** – `ConversationManager` が一定間隔でペルソナの `run_pulse` を呼び出し、そのログを Sidebar から確認（開始/停止ボタン付き）
- **Network mode** – Online/Offline 切り替えで SDS 心拍を制御 (`manager.switch_to_online_mode/offline_mode`)
- **DB Manager** (`database/db_manager.py`) – 任意テーブルの参照・追加・更新・削除、外部キー選択
- **Task Manager** (`ui/task_manager.py`) – ペルソナごとの `tasks.db` (TaskStorage) を DataFrame 表示（タスク/ステップ/履歴）
- **Memory Settings UI** (`tools/utilities/memory_settings_ui.py`) – SAIMemory のスレッド一覧・メッセージ、ChatGPT エクスポートのインポート、再埋め込み、タグ編集を GUI 上で実行
- **World Editor** (`ui/world_editor.py`) – City/Building/AI/Tool の CRUD、定員やシステムプロンプト編集、AI 移動、ツールリンク、アバターアップロード、オンライン/オフライン切替
- **Task tools** – Sidebar から `call_persona_ui` などを通じて、UI から直接ツール呼び出しが行われます

## Memory, tasks, and logs

### SAIMemory & persona folders

- すべての会話ログは `~/.saiverse/personas/<persona_id>/` に書き出されます (`log.json`, `conscious_log.json`, `memory.db`, `tasks.db`, 添付ファイル, `task_requests.jsonl`, etc.)
- 代表的なスクリプト:
  - `scripts/import_persona_logs_to_saimemory.py` – JSON ログ群を SAIMemory SQLite に移行
  - `scripts/export_saimemory_to_json.py` – 任意期間を JSON 出力
  - `scripts/backup_saimemory.py` – `rdiff-backup` で差分バックアップ (`--full` でスナップショット)
  - `scripts/prune_sai_memory.py`, `scripts/tag_conversation_messages.py` – 古いエントリ整理・タグ付け

### MemoryCore & Qdrant

- `memory_core/` は SBERT (fastembed/sentence-transformers) と Qdrant を使って `entries` / `topics` コレクションを構築します
- Embedded Qdrant を使う場合は `QDRANT_LOCATION` に保存先を指定（default: `~/.saiverse/qdrant`）。サーバモードの場合は `QDRANT_URL` / `QDRANT_API_KEY`
- 主なユーティリティ:
  - `scripts/ingest_persona_log.py <persona>` – 既存ログを per-persona Qdrant コレクションへ投入
  - `scripts/recall_persona_memory.py <persona> "query"` – semantic recall の CLI
  - `scripts/rename_generic_topics.py`, `scripts/memory_topics.py`, `scripts/memory_topics_ui.py` – トピック名整理、可視化、ブラウザ UI
  - `scripts/reembed_memory.py` – 埋め込み再計算

### Task storage

- `persona/tasks/storage.py` が SQLite (`tasks.db`) をラップし、Tool API (`task_request_creation`, `task_change_active`, `task_update_step`, `task_close`) や `ui/task_manager.py` から利用されます
- `scripts/process_task_requests.py` でペルソナごとの `task_requests.jsonl` をバッチ処理し、Gemini などでタスク生成が可能

### Logs

- `saiverse_log.txt` – ハイレベルなシステムログ（Building 移動、会話トリガー、SDS ステータスなど）
- `raw_llm_responses.txt` – LLM への送受信内容（デバッグ用）
- `log_*.txt`, `documents/` – 任意の追加ログ
- `generate_image/*.png` – `tools/defs/image_generator.py` で生成した画像

## Tooling & LLM stack

### Model providers & fallback

- `llm_clients/factory.py` が `models.json` の provider に応じてクライアントを作成
- OpenAI: `openai==1.97` を使用。`thinking_type` が設定された Claude モデルは自動で thinking 拡張を付与
- OpenAI 互換エンドポイント (NVIDIA NIM など) は `models.json` の各エントリに `base_url` と任意で `api_key_env` を指定するだけで接続可能
- `models.json` の `parameters` で `temperature` や `reasoning_effort`、`max_completion_tokens` などの許容範囲と既定値を宣言でき、チャット UI のモデル選択欄に対応スライダー／ドロップダウンが自動表示される（`temperature` は 0〜2、`top_p` は 0〜1、`reasoning_effort` は none/minimal/low/medium/high。`verbosity` は OpenAI Responses API 専用のため、現状の chat.completions ルートでは自動的に非表示）。citeturn1view0turn0search9
- Gemini: `google-genai` の `GeminiClient` (2.5 Pro/Flash, 2.0 Flash, 1.5 Flash) をラップ。free→paid の自動リトライに対応
- Ollama: ローカルサーバを `OLLAMA_BASE_URL` / 既知ホストへプローブし、到達不可なら Gemini 2.0 Flash へフォールバック (`llm_clients/ollama.py`)
- 画像生成: `tools/defs/image_generator.py` が `gemini-2.5-flash-image` を利用（有料キー必須）

### Router & action handler

- `llm_router.route(user_message, tools)` は Gemini 2.0 Flash (free→paid 自動切替) で JSON (`{"call":"yes/no","tool":"...","args":{...}}`) を生成します
- `action_handler.py` は LLM 応答に含まれる `::act ... ::end` セクションを解析し、`move`, `pickup_item`, `create_persona`, `summon`, `dispatch_persona`, `use_item` などを実行
- `tools/context.persona_context()` でツール実行時のペルソナ情報・マネージャ参照を ContextVar にセット

### Built-in tool catalog (抜粋)

| Tool | Module | 内容 |
| --- | --- | --- |
| `calculate_expression` | `tools/defs/calculator.py` | 加減乗除・累乗・階乗をサポートする AST ベース計算機 |
| `generate_image` | `tools/defs/image_generator.py` | Gemini-2.5-Flash-Image で画像生成し `generate_image/` に保存 |
| `item_pickup` / `item_place` / `item_use` | `tools/defs/item_*.py` | Building と `item` テーブルを操作し、インベントリ移動を行う |
| `task_request_creation` | `tools/defs/task_request_creation.py` | タスク生成リクエストを `task_requests.jsonl` に記録 & 即時処理を試行 |
| `task_change_active` / `task_update_step` / `task_close` | `tools/defs/task_*.py` | TaskStorage API を介したステータス更新 |
| `switch_active_thread` | `tools/defs/thread_switch.py` | SAIMemory のアクティブスレッドを切替え、リンク情報を挿入 |
| `task_request_creation` |  | Gemini 等でのバッチ処理前にリクエストをキューに追加 |

各 Building の `TOOL_REGISTRY` への紐付けは `manager.update_building()` や World Editor で編集できます (`building_tool_link` テーブル)。

## Buildings, items, blueprints

- `buildings.py` はデータベースの Building レコードをロードし、`Building` オブジェクト (system prompt, entry/auto prompt, capacity, auto interval) を作成
- `manager/blueprints.py` は AI/Building のテンプレートを管理し、`create_persona` アクション時に利用
- `database.models.Item / ItemLocation` + `manager` のアイテム API で Building/Persona/World に属するオブジェクトを移動・使用できます
- `ai_sessions/` にはペルソナベースラインのプロンプトやメモリ初期化ファイルを配置

## Inter-city travel & remote visitors

1. **Dispatch request** (`VisitorMixin.dispatch_persona`) – 送信側都市が `VisitingAI` に `status='requested'` レコードを追加
2. **Destination intake** (`DatabasePollingMixin._check_for_visitors`) – 受信側都市がリクエストを検出し、`RemotePersonaProxy` を生成して Building に配置 (`profile_json` の `target_building_id` に従う)
3. **Thinking proxy** (`database/api_server.py` / `/persona-proxy/{persona}/think`) – Remote Persona は滞在先の会話をまとめて故郷都市へ問い合わせ、`ThinkingRequest` → `PersonaCore._generate()` で回答を取得
4. **SDS heartbeat** (`SDSMixin`, `sds_server.py`) – 都市は 30 秒ごとに SDS へハートビートを送り、他都市の `api_base_url` キャッシュを更新
5. **Return / completion** – 訪問終了時は `VisitingAI.status` を更新し、派遣元は `_finalize_dispatch` でローカル状態を更新。Discord 経由の訪問者も `GatewayMixin` で同じフローを共有

## Discord gateway (optional)

- `discord_gateway/` 内のサービスは WebSocket 経由で SAIVerse と Discord Bot を接続します
- 必要なもの:
  - `pip install -r discord_gateway/requirements-dev.txt`
  - `.env` に `SAIVERSE_GATEWAY_WS_URL`, `SAIVERSE_GATEWAY_TOKEN`, (必要に応じて) Discord Bot のトークン設定
  - `discord_gateway/docs/` に設定手順がまとまっています
- ゲートウェイ稼働時、Discord 上の訪問者登録・退室・メッセージ・メモリ同期イベントを `GatewayMixin` 経由で Hook できます

## Maintenance scripts (抜粋)

| Script | 用途 | 例 |
| --- | --- | --- |
| `scripts/backup_saimemory.py persona_a persona_b --output-dir ~/.saiverse/backups/saimemory` | SAIMemory SQLite を rdiff-backup でスナップショット化 | `python scripts/backup_saimemory.py air_city_a --verbose` |
| `scripts/import_persona_logs_to_saimemory.py --reset --default-start <ISO> --persona air_city_a` | 過去 JSON ログを SAIMemory に移行 | `python scripts/import_persona_logs_to_saimemory.py --include-archives --persona eris_city_a` |
| `scripts/export_saimemory_to_json.py <persona> --start 2025-07-01 --end 2025-10-05 --output air.json` | SAIMemory から期間指定でエクスポート | `python scripts/export_saimemory_to_json.py air_city_a --output -` |
| `scripts/ingest_persona_log.py <persona>` | Persona ログを per-persona Qdrant DB に取り込み | `python scripts/ingest_persona_log.py eris --location-base ~/.saiverse/qdrant --collection-prefix saiverse` |
| `scripts/recall_persona_memory.py <persona> "query"` | Qdrant から関連記憶を取得 | `python scripts/recall_persona_memory.py air "旅行 温泉" --json` |
| `scripts/rename_generic_topics.py <persona>` | トピック名の一括リネーム (dry-run あり) | `python scripts/rename_generic_topics.py eris --dry-run` |
| `scripts/memory_topics_ui.py` | ブラウザ UI でトピック全体を可視化 | `python scripts/memory_topics_ui.py` |
| `scripts/process_task_requests.py` | `task_requests.jsonl` を処理し新規タスク化 | `python scripts/process_task_requests.py --base ~/.saiverse/personas` |
| `scripts/reembed_memory.py` | SAIMemory / MemoryCore の埋め込みを再生成 | `python scripts/reembed_memory.py air` |
| `scripts/memory_smoke.py`, `scripts/memory_topics.py` | MemoryCore の疎通確認・トピック要約 |  |
| `scripts/run_discord_gateway_tests.py` | Discord gateway の自動テスト | `python scripts/run_discord_gateway_tests.py` |

> その他: `scripts/prune_sai_memory.py`, `scripts/tag_conversation_messages.py`, `scripts/migrate_memory_tags.py`, `scripts/memory_topics_ui.py`, `scripts/memory_topics.py` などが利用できます。

## Tests

- すべてのテストは `tests/` に配置されており `unittest` ベースです（`pytest` での実行も可）
- 代表テスト:
  - `tests/test_llm_clients.py`, `tests/test_llm_router.py` – LLM ルーターとクライアント工場
  - `tests/test_history_manager.py`, `tests/test_persona_mixins.py` – ペルソナ履歴・移動ロジック
  - `tests/test_memory_core.py`, `tests/test_sai_memory_storage.py`, `tests/test_sai_memory_chunking.py` – 記憶モジュール
  - `tests/test_task_storage.py`, `tests/test_task_tools.py`, `tests/test_pulse_task_summary.py` – タスク関連
  - `tests/test_image_generator.py`, `tests/test_chatgpt_importer.py`, `tests/test_thread_switch_tool.py`
- コマンド例:
  ```bash
  python -m pytest
  # もしくは
  python -m unittest discover tests
  ```

## Troubleshooting

- **UI が開かない / ポート競合**: `city` テーブルの `UI_PORT` を確認し、既存プロセスを停止。`main.py` が自動で PID を探して kill しますが OS 権限が不足すると失敗することがあります
- **SDS に接続できない**: UI Sidebar からオフラインモードへ切替 (`manager.switch_to_offline_mode`) し、ローカル DB の他都市設定で代替
- **Qdrant 関連のエラー**: `pip install qdrant-client` 済みか、`QDRANT_LOCATION` が存在するか、または `QDRANT_URL` が reachable か確認
- **Gemini 画像生成に失敗**: `GEMINI_API_KEY` (有料枠) が必要。free キーのみでは `gemini-2.5-flash-image` が利用できません
- **SAIMemory が巨大化する**: `scripts/backup_saimemory.py` や `scripts/prune_sai_memory.py` で定期的に整理し、`SAIMEMORY_LAST_MESSAGES` を調整
- **タスクが生成されない**: `scripts/process_task_requests.py` を定期実行するか、UI の Task Manager で `tasks.db` を確認
- **Discord gateway handshake 失敗**: `.env` の `SAIVERSE_GATEWAY_WS_URL` (ws/wss) と `SAIVERSE_GATEWAY_TOKEN` が一致しているか、サーバ側ログを参照

## Further reading

- `docs/architecture.md` – コンポーネント図と説明
- `docs/database_design.md` – SQLite schema と設計思想
- `docs/test_manual.md` – 手動テストシナリオ (World dive, persona genesis など)
- `docs/release_manual.md` – β リリース手順
- `docs/autonomy_task_refactor.md`, `docs/pulse_debug_retrospective.md`, `docs/roadmap.md` – 最近の開発メモ
- `current_task.md` – 進行中タスクのメモ
- `documents/` – 会話ログや追加資料

Happy hacking in SAIVerse! 🌌
