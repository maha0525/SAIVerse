<!-- 🤖 AUTO-GENERATED — 手で編集しない。次回生成で上書きされる。 -->
<!-- 源: api.main.api_router (api/routes/*.py) / 再生成: python scripts/gen_reference_docs.py -->

# API エンドポイント

REST API 全エンドポイントの一覧（自動生成）。すべて `/api` 配下にマウントされる。

**エンドポイント数**: 346（tag グループ: 25）

## addon

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/addon` | expansion_data/ 下のアドオン一覧を返す。 |
| GET | `/api/addon/` | expansion_data/ 下のアドオン一覧を返す。 |
| GET | `/api/addon/messages/{message_id}/metadata` | メッセージに紐付くアドオンメタデータを返す。 |
| GET | `/api/addon/{addon_name}` | 指定アドオンの詳細を返す。 |
| GET | `/api/addon/{addon_name}/config` | アドオンのグローバルパラメータを返す。 |
| PUT | `/api/addon/{addon_name}/config` | アドオンのグローバルパラメータを更新する。 |
| POST | `/api/addon/{addon_name}/config/file/{param_key}` | グローバルのファイルパラメータをアップロードする（雛形）。 |
| GET | `/api/addon/{addon_name}/config/file/{param_key}` | グローバルのファイルパラメータを取得する（雛形）。 |
| DELETE | `/api/addon/{addon_name}/config/file/{param_key}` | グローバルのファイルパラメータを削除する（雛形）。 |
| GET | `/api/addon/{addon_name}/config/persona/{persona_id}` | ペルソナ固有のアドオンパラメータを返す。存在しない場合は空辞書。 |
| PUT | `/api/addon/{addon_name}/config/persona/{persona_id}` | ペルソナ固有のアドオンパラメータを **merge** で更新する。 |
| DELETE | `/api/addon/{addon_name}/config/persona/{persona_id}` | ペルソナ固有のアドオンパラメータを削除する（デフォルトに戻す）。 |
| POST | `/api/addon/{addon_name}/config/persona/{persona_id}/file/{param_key}` | ペルソナ別のファイルパラメータをアップロードする。 |
| GET | `/api/addon/{addon_name}/config/persona/{persona_id}/file/{param_key}` | ペルソナ別のファイルパラメータを取得(プレビュー/ダウンロード)する。 |
| DELETE | `/api/addon/{addon_name}/config/persona/{persona_id}/file/{param_key}` | ペルソナ別のファイルパラメータを削除する。 |
| PUT | `/api/addon/{addon_name}/enabled` | アドオンの有効/無効を切り替える。サーバー再起動不要で即時反映。 |

## addon-actions

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/addon/{addon_name}/actions` |  |
| POST | `/api/addon/{addon_name}/actions` |  |
| GET | `/api/addon/{addon_name}/actions/available-tools` |  |
| POST | `/api/addon/{addon_name}/actions/test` |  |
| GET | `/api/addon/{addon_name}/actions/test-targets` | テスト実行の対象候補 (機体) を返す。 |
| GET | `/api/addon/{addon_name}/actions/tool-schemas` | ステップで呼べるツールを引数スキーマ付きで返す (UI の引数フォーム用)。 |
| GET | `/api/addon/{addon_name}/actions/{action_id}` |  |
| PUT | `/api/addon/{addon_name}/actions/{action_id}` |  |
| DELETE | `/api/addon/{addon_name}/actions/{action_id}` |  |

## addon-catalog

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/addon-catalog/debug/registry-url` | 現在の registry URL と env 上書きの状態を返す。 |
| POST | `/api/addon-catalog/install` | アドオンを registry 経由でインストール (SSE 進捗 stream)。 |
| GET | `/api/addon-catalog/installed` | expansion_data/ 配下にある全アドオンの現在状態を返す。 |
| GET | `/api/addon-catalog/registry` | registry.json を fetch (キャッシュ済み) して返す。 |
| POST | `/api/addon-catalog/uninstall` | アドオンをアンインストール (SSE 進捗 stream)。 |
| POST | `/api/addon-catalog/update` | インストール済みアドオンを registry 経由で更新 (SSE 進捗 stream)。 |

## addon-events

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/addon/events` | アドオンイベントの常設 SSE エンドポイント。 |

## admin

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/admin/backfill-item-descriptions` | Batch-generate descriptions for picture items with placeholder text. |
| GET | `/api/admin/env` | Get environment variables from .env file. |
| POST | `/api/admin/env` | Update environment variables in .env file and runtime os.environ. |
| POST | `/api/admin/restart` | Restart the server process. |

## auth

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/auth/login` |  |
| POST | `/api/auth/login` |  |

## chat

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/chat/history` |  |
| POST | `/api/chat/permission-response` | Respond to a playbook execution permission request. |
| GET | `/api/chat/persona/{persona_id}/avatar` |  |
| POST | `/api/chat/preview` | Preview the context that would be sent to the LLM, without executing. |
| POST | `/api/chat/send` |  |
| POST | `/api/chat/spell-confirmation-response` | Respond to a generic spell confirmation request. |
| POST | `/api/chat/stop` | Stop the active LLM generation for the user's current building. |
| POST | `/api/chat/utter` | 発言契機入室。 必要なら自動 move を伴って chat を実行する。 |

## codex-auth

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/codex-auth/login/cancel` | 自分の lease を返却する (モーダルを閉じたときにフロントが呼ぶ)。 |
| POST | `/api/codex-auth/login/start` | デバイスコードを申請し、ユーザーに見せる user_code を返す。 |
| GET | `/api/codex-auth/login/status` | ログイン試行の進行状態を返す。フロントはこれをポーリングする。 |
| POST | `/api/codex-auth/logout` | SAIVerse 自前のトークンストアを削除する。 |
| GET | `/api/codex-auth/status` | どのトークンストアが認証源か・その健康状態を返す。 |

## config

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/config/announcements-monitor` | Get announcements monitoring status. |
| POST | `/api/config/announcements-monitor` | Toggle announcements monitoring on/off. |
| GET | `/api/config/cache` | Get current cache settings and model cache support info. |
| POST | `/api/config/cache` | Update cache settings. |
| GET | `/api/config/config` | Get current model and parameter configuration. |
| GET | `/api/config/developer-mode` | Get developer mode status. |
| POST | `/api/config/developer-mode` | Set developer mode status. |
| GET | `/api/config/favorite-models` | Get user's favorite model IDs. |
| POST | `/api/config/favorite-models` | Set user's favorite model IDs. |
| GET | `/api/config/global-auto` | Get global autonomous mode status. |
| POST | `/api/config/global-auto` | Set global autonomous mode status. |
| GET | `/api/config/image-default-quality` | Get default image generation quality setting. |
| POST | `/api/config/image-default-quality` | Set default image generation quality and persist to .env. |
| GET | `/api/config/max-image-embeds` | Get current max image embeds setting. |
| POST | `/api/config/max-image-embeds` | Set session override for max image embeds. |
| GET | `/api/config/media-recall` | Get whether attached media (image/audio/video) summaries feed the auto-recall query. |
| POST | `/api/config/media-recall` | Toggle attached-media auto-recall and persist to .env. |
| POST | `/api/config/model` | Set the global model override and return updated config. |
| GET | `/api/config/models` | List available LLM models. |
| POST | `/api/config/models` | Create a new model JSON file in user_data. |
| POST | `/api/config/models/save-from-chat` | Save current chat UI settings as a model JSON file. |
| GET | `/api/config/models/{key}` | Get the raw (unresolved) config for a model, as written in the JSON file. |
| PUT | `/api/config/models/{key}` | Update a model. Builtin/expansion models get an automatic user_data copy. |
| DELETE | `/api/config/models/{key}` | Delete a user_data model file. Builtin models are read-only. |
| POST | `/api/config/models/{key}/clone` | Clone an existing model under a new key (always to user_data). |
| POST | `/api/config/parameters` | Update global model parameter overrides. |
| GET | `/api/config/playbook` | Get current playbook override and args. |
| POST | `/api/config/playbook` | Set playbook override and args. |
| GET | `/api/config/playbook-permissions` | Return all router_callable playbooks with their current permission level for this city. |
| POST | `/api/config/playbook-permissions` | Set the permission level for a playbook in this city. |
| GET | `/api/config/playbooks` | List available user-selectable playbooks with input_schema. |
| GET | `/api/config/playbooks/{name}/params` | Get playbook parameters with resolved enum options. |
| GET | `/api/config/reembed-check` | Return list of personas that need re-embedding due to model changes. |
| POST | `/api/config/reload-models` | Reload model configurations from disk without restarting the server. |
| GET | `/api/config/slot-kinds` | コマ種別カタログの一覧 (timetable_redesign.md §5.5)。 |
| GET | `/api/config/startup-warnings` | Return warnings collected during startup (e.g. failed persona loads). |
| GET | `/api/config/update-check` | Get update check monitoring status. |
| POST | `/api/config/update-check` | Toggle update availability check on/off. |

## db

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/db/tables` | List all available database tables and their schemas. |
| GET | `/api/db/tables/{table_name}` | Get data from a specific table. |
| POST | `/api/db/tables/{table_name}` | Insert or Update a row. |
| DELETE | `/api/db/tables/{table_name}` | Delete a row by Primary Key(s). |

## feeds

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/feeds/fetch` | 全フィードの手動取得を起動する (完了は待たず 202 を返す)。 |
| POST | `/api/feeds/fixtures` | フィード施設を作成する。プリセットから、または空の施設として。 |
| GET | `/api/feeds/fixtures` | フィード施設の一覧 (購読と健康状態つき)。 |
| GET | `/api/feeds/items` | フィード施設の取得済み記事一覧 (新しい順)。 |
| GET | `/api/feeds/presets` | フィードプリセット (購読束 + 施設の見た目) の一覧。 |
| POST | `/api/feeds/subscriptions` | 購読を追加する。 |
| DELETE | `/api/feeds/subscriptions/{subscription_id}` | 購読を削除する (記事・既読カーソルも道連れ — feed_manager 側の仕様)。 |

## info

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/info/city-map` | Return one map scope in one shot: its Buildings plus their current occupants. |
| GET | `/api/info/details` | Get detailed info about a building: occupants, items. |
| GET | `/api/info/item/{item_id}` |  |
| GET | `/api/info/item/{item_id}/bag-contents` | Get the contents of a bag item. |
| PUT | `/api/info/item/{item_id}/content` | Update the content of a document item. |
| POST | `/api/info/item/{item_id}/toggle-open` | Toggle the open/close state of an item. |
| GET | `/api/info/models` | Get list of available models for persona configuration. |

## mcp

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/mcp/failures` | Return all instances currently in backoff after a startup failure. |
| POST | `/api/mcp/instances/retry` | Force-retry an instance that is currently in backoff. |
| POST | `/api/mcp/instances/stop` | Force-stop a specific instance, ignoring refcount. |
| GET | `/api/mcp/servers` | Return status of every known server instance. |
| POST | `/api/mcp/servers/{server_name}/reconnect` | Reconnect all instances of the given qualified server name. |
| POST | `/api/mcp/tool-call` | Invoke an MCP tool directly (admin / debug). |
| GET | `/api/mcp/tools` |  |

## media

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/media/audio/{filename}` | Serve a normalized audio file. |
| GET | `/api/media/documents/{filename}` | Serve an uploaded document. |
| GET | `/api/media/images/{filename}` | Serve an uploaded image. |
| POST | `/api/media/upload` | Upload an image file. Resizes to max 768px long edge for LLM optimization. |
| POST | `/api/media/upload-audio` | Upload an audio file. Normalizes to opus 24kbps mono 16kHz ogg via ffmpeg. |
| POST | `/api/media/upload-document` | Upload a text document file. |
| POST | `/api/media/upload-file` | Upload any file (image, audio, video, or document). Auto-detects type and returns |
| POST | `/api/media/upload-hires` | Upload an image without LLM-oriented downscaling. |
| POST | `/api/media/upload-video` | Upload a video file. Normalizes to 1FPS 480p + opus 24kbps mono 16kHz mp4 via ffmpeg. |
| GET | `/api/media/video/{filename}` | Serve a normalized video file. |

## oauth

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/oauth/callback/{addon_name}/{flow_key}` | OAuth 認可サーバーからのコールバック。 |
| GET | `/api/oauth/start/{addon_name}/{flow_key}` | 認可URLを生成して返す。フロントはこれをポップアップで開く。 |
| DELETE | `/api/oauth/{addon_name}/{flow_key}/{persona_id}` | OAuth 接続を切断する（保存トークンを削除）。 |
| GET | `/api/oauth/{addon_name}/{flow_key}/{persona_id}/status` | 接続ステータスを返す。 |

## observer

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/observer/building/{building_id}/fixtures` | Building に設置された Fixture の一覧を取得する。 |
| POST | `/api/observer/config` | Observer を作成 (upsert) する。 |
| POST | `/api/observer/fixture` | Fixture を作成 (upsert) する。 |
| GET | `/api/observer/fixture/{fixture_id}` | Fixture の情報を取得する。 |
| GET | `/api/observer/{observer_id}/history/{metric_name}` | Observer の指定メトリクスの履歴を取得する。 |
| GET | `/api/observer/{observer_id}/latest` | Observer の最新メトリクス (STATE_JSON キャッシュ) を取得する。 |
| POST | `/api/observer/{observer_id}/push` | 外部アプリから Observer にメトリクスを push する。 |

## people

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/people` | Return all registered personas (AI rows). |
| GET | `/api/people/` | Return all registered personas (AI rows). |
| POST | `/api/people/dismiss/{persona_id}` | Dismiss a persona (send back to private room). |
| GET | `/api/people/meta_playbooks` | List user-selectable meta playbooks for schedule / summon dialogs. |
| GET | `/api/people/realtime-spell-catalog` | リアルタイムスペルとして設定可能なスペル一覧とスキーマを返す。 |
| GET | `/api/people/spells` | List Spells available for schedule / pre_spells UI selection. |
| POST | `/api/people/summon/{persona_id}` | Summon a persona to the target building. |
| GET | `/api/people/summonable` | List personas that can be summoned (not in current room, not dispatched). |
| GET | `/api/people/{persona_id}/arasuji` | List Chronicle entries for a persona (part of Memory Weave). |
| DELETE | `/api/people/{persona_id}/arasuji` | Delete ALL Chronicle entries and reset progress. |
| GET | `/api/people/{persona_id}/arasuji/cost-estimate` | Estimate the cost of generating Chronicle for unprocessed messages. |
| GET | `/api/people/{persona_id}/arasuji/diagnosis` | Get diagnostic information about Chronicle structure (no message content). |
| POST | `/api/people/{persona_id}/arasuji/generate` | Start Chronicle generation as a background job. |
| GET | `/api/people/{persona_id}/arasuji/generate/{job_id}` | Get the status of a Chronicle generation job. |
| POST | `/api/people/{persona_id}/arasuji/generate/{job_id}/cancel` | Cancel a running Chronicle generation job. |
| POST | `/api/people/{persona_id}/arasuji/messages-by-ids` | Get messages by their IDs (for error investigation). |
| GET | `/api/people/{persona_id}/arasuji/stats` | Get arasuji statistics for a persona. |
| GET | `/api/people/{persona_id}/arasuji/{entry_id}` | Get a detailed Chronicle entry by ID. |
| DELETE | `/api/people/{persona_id}/arasuji/{entry_id}` | Delete a Chronicle entry and unmark child entries as consolidated. |
| PATCH | `/api/people/{persona_id}/arasuji/{entry_id}` | Update a Chronicle entry's content. |
| GET | `/api/people/{persona_id}/arasuji/{entry_id}/fragments` | Get Memopedia fragments generated from a Chronicle entry. |
| GET | `/api/people/{persona_id}/arasuji/{entry_id}/messages` | Get the source raw messages for a Level 1 Chronicle entry. |
| POST | `/api/people/{persona_id}/arasuji/{entry_id}/regenerate` | Regenerate a specific Chronicle entry while preserving parent relationship. |
| POST | `/api/people/{persona_id}/cache-config` | persona の cache 設定 ("off"/"5m"/"1h") を設定する (Phase 2、in-memory・非永続)。 |
| GET | `/api/people/{persona_id}/cache-status` | 指定ペルソナの prompt cache 状態 (効いてるか / 残り秒) を read-only で返す。 |
| GET | `/api/people/{persona_id}/clips` | メッセージ群に付いた観測点 (点クリップ) をバッチで返す (画面 C: ハイライト)。 |
| GET | `/api/people/{persona_id}/config` | Get persona configuration. |
| PATCH | `/api/people/{persona_id}/config` | Update persona configuration. |
| GET | `/api/people/{persona_id}/context-status` | 指定ペルソナの提示コンテキスト状態 (水位 + 現在文字数) を read-only で返す。 |
| GET | `/api/people/{persona_id}/core-memory` | 生存中のコア記憶を一覧する (未確認フラグ付き)。訂正・確認・削除は各変更系 API で。 |
| POST | `/api/people/{persona_id}/core-memory/scene` | アンカー周辺の会話を scene としてコア記憶に刻む。 |
| GET | `/api/people/{persona_id}/core-memory/trash` | ごみ箱 (soft-delete 済み) のコア記憶を削除の新しい順に一覧する。 |
| PUT | `/api/people/{persona_id}/core-memory/{memory_id}` | コア記憶の本文をユーザーが訂正する。訂正した時点で確認済み (confirmed=1) になる。 |
| DELETE | `/api/people/{persona_id}/core-memory/{memory_id}` | コア記憶を soft-delete する (ごみ箱へ)。物理削除はせず復元可能に残す。 |
| POST | `/api/people/{persona_id}/core-memory/{memory_id}/confirm` | 未確認 (自動採取) のコア記憶をユーザーが「確認済み」にする。 |
| POST | `/api/people/{persona_id}/core-memory/{memory_id}/restore` | ごみ箱からコア記憶を復元する。 |
| POST | `/api/people/{persona_id}/debug/generate-embeddings` | Chronicle / Memopedia page / Fragment の未生成 embedding をバッチ生成. |
| POST | `/api/people/{persona_id}/debug/memopedia-conversion/apply` | 変換を実行する。逐語の検算に落ちたら何も書かずに 409 を返す。 |
| GET | `/api/people/{persona_id}/debug/memopedia-conversion/preview` | 下見: 変換したら何がどうなるかを、ページと Fragment を変えずに返す。 |
| POST | `/api/people/{persona_id}/debug/memopedia-conversion/preview` | 判断を織り込んだ下見。 |
| POST | `/api/people/{persona_id}/debug/memopedia-conversion/revert` | 変換を丸ごと取り消す。 |
| GET | `/api/people/{persona_id}/debug/memopedia-conversion/runs` | 取り消せる変換の一覧 (新しい順)。 |
| GET | `/api/people/{persona_id}/experience-ledger` | 台帳の索引 — カテゴリごとにグループ化した棚の一覧 (統計付き)。 |
| GET | `/api/people/{persona_id}/experience-ledger/{page_id}` | ページを開く = 動的合成 (fragment / 関与あらすじの履歴 / 共起ページ)。 |
| POST | `/api/people/{persona_id}/import/extension` | Import Chrome extension export (JSON or Markdown) in background. |
| GET | `/api/people/{persona_id}/import/extension/status` | Get the status of extension import task. |
| POST | `/api/people/{persona_id}/import/native` | Import native SAIVerse JSON. |
| POST | `/api/people/{persona_id}/import/native/preview` | Preview a native JSON file before importing. |
| GET | `/api/people/{persona_id}/import/native/status` | Poll native import progress. |
| POST | `/api/people/{persona_id}/import/official` | Import selected ChatGPT conversations from a previously previewed export (background). |
| POST | `/api/people/{persona_id}/import/official/preview` | Preview ChatGPT export file and return conversation list for selection. |
| GET | `/api/people/{persona_id}/import/official/status` | Get the status of official import task. |
| GET | `/api/people/{persona_id}/items` | List items held by a persona. |
| POST | `/api/people/{persona_id}/memopedia/build-from-logs` | Start building Memopedia pages from chat logs as a background job. |
| GET | `/api/people/{persona_id}/memopedia/export` | Export all Memopedia pages as JSON. |
| POST | `/api/people/{persona_id}/memopedia/generate` | Start Memopedia page generation as a background job. |
| GET | `/api/people/{persona_id}/memopedia/generate/{job_id}` | Get the status of a Memopedia generation job. |
| POST | `/api/people/{persona_id}/memopedia/import` | Import Memopedia pages from JSON. |
| DELETE | `/api/people/{persona_id}/memopedia/pages` | Delete ALL non-root Memopedia pages (and their edit history). |
| POST | `/api/people/{persona_id}/memopedia/pages` | Create a new Memopedia page. |
| POST | `/api/people/{persona_id}/memopedia/pages/move` | Move multiple pages to a trunk (or any parent page). |
| GET | `/api/people/{persona_id}/memopedia/pages/{page_id}` | Get a Memopedia page content as Markdown, plus fragments. |
| PUT | `/api/people/{persona_id}/memopedia/pages/{page_id}` | Update a Memopedia page (title, summary, content, keywords). |
| DELETE | `/api/people/{persona_id}/memopedia/pages/{page_id}` | Delete a Memopedia page (soft delete). |
| POST | `/api/people/{persona_id}/memopedia/pages/{page_id}/desk` | 机に開く / 棚に戻す (open=true: 机に開く、open=false: 棚に戻す)。 |
| GET | `/api/people/{persona_id}/memopedia/pages/{page_id}/history` | Get the edit history for a Memopedia page. |
| PUT | `/api/people/{persona_id}/memopedia/pages/{page_id}/important` | Set or unset the important flag for a page. |
| POST | `/api/people/{persona_id}/memopedia/pages/{page_id}/rollback/{edit_id}` | Rollback a page to the state before a specific edit. |
| PUT | `/api/people/{persona_id}/memopedia/pages/{page_id}/trunk` | Set or unset the trunk flag for a page. |
| GET | `/api/people/{persona_id}/memopedia/tree` | Get the Memopedia knowledge tree with category metadata. |
| GET | `/api/people/{persona_id}/memopedia/trunks` | Get all trunk pages, optionally filtered by category. |
| GET | `/api/people/{persona_id}/memopedia/unorganized` | Get pages that are direct children of the root (not in any trunk). |
| GET | `/api/people/{persona_id}/memory-notes` | List unresolved memory notes. |
| POST | `/api/people/{persona_id}/memory-notes/resolve` | Mark memory notes as resolved. |
| GET | `/api/people/{persona_id}/memory/messages/search` | 会話メッセージをキーワード (空白区切りで AND) で検索する。 |
| GET | `/api/people/{persona_id}/memory/messages/{message_id}/window` | アンカーメッセージ周辺の会話窓を返す (scene プレビュー用)。 |
| PATCH | `/api/people/{persona_id}/messages` | SAIMemory messages の line_role / scope を一括更新する。 |
| PATCH | `/api/people/{persona_id}/messages/{message_id}` | Update message content and/or timestamp. |
| DELETE | `/api/people/{persona_id}/messages/{message_id}` | Delete a message. |
| POST | `/api/people/{persona_id}/meta-judgment/bulk-delete` | Delete multiple meta_judgment_log rows in one request. |
| DELETE | `/api/people/{persona_id}/meta-judgment/{judgment_id}` | Delete a single meta_judgment_log row owned by ``persona_id``. |
| POST | `/api/people/{persona_id}/organize-memory` | 手動の記憶整理 — 残す量より古い側を今すぐあらすじに畳む。 |
| GET | `/api/people/{persona_id}/pocketbook` | 手帳を読む — アクティビティごとにメモを日付降順で束ねて返す。 |
| GET | `/api/people/{persona_id}/pulse-logs` | List pulse_id summaries with pagination (newest first). |
| GET | `/api/people/{persona_id}/pulse-logs/{pulse_id}` | Get all log entries for a specific pulse. |
| GET | `/api/people/{persona_id}/pulse-timeline` | messages を pulse_id でグルーピングした Pulse サマリ一覧 (新しい順)。 |
| GET | `/api/people/{persona_id}/pulse-timeline/{pulse_id}` | 指定 Pulse の messages 全件 (discardable 含む、時系列順)。 |
| GET | `/api/people/{persona_id}/realtime-spell` | ペルソナに設定されたリアルタイムスペル一覧を取得する。 |
| POST | `/api/people/{persona_id}/realtime-spell` | ペルソナにリアルタイムスペル binding を追加する。 |
| DELETE | `/api/people/{persona_id}/realtime-spell/{binding_id}` | ペルソナのリアルタイムスペル binding を削除する。 |
| POST | `/api/people/{persona_id}/recall` | Execute memory recall, similar to the memory_recall tool. |
| POST | `/api/people/{persona_id}/recall-debug` | Debug-friendly recall: returns raw search results with scores, no context expansion. |
| POST | `/api/people/{persona_id}/reembed` | Start re-embedding messages in the background. |
| GET | `/api/people/{persona_id}/reembed/status` | Get the status of the re-embed task. |
| POST | `/api/people/{persona_id}/rescue-stelis-thread` | Convert the current active Stelis thread to a normal thread. |
| GET | `/api/people/{persona_id}/schedules` | List schedules for a persona. |
| POST | `/api/people/{persona_id}/schedules` | Create a new schedule. |
| PUT | `/api/people/{persona_id}/schedules/{schedule_id}` | Update an existing schedule. |
| DELETE | `/api/people/{persona_id}/schedules/{schedule_id}` | Delete a schedule. |
| POST | `/api/people/{persona_id}/schedules/{schedule_id}/toggle` | Toggle schedule enabled status. |
| GET | `/api/people/{persona_id}/storage-layers` | Return a unified view of the 7-layer storage for one persona. |
| GET | `/api/people/{persona_id}/task-book` | タスク帳の open な一件を作成順に返す (読み取り専用)。 |
| GET | `/api/people/{persona_id}/threads` | List all conversation threads for a persona. |
| DELETE | `/api/people/{persona_id}/threads/{thread_id}` | Delete a thread. |
| PUT | `/api/people/{persona_id}/threads/{thread_id}/activate` | Set a thread as the active thread for the persona. |
| GET | `/api/people/{persona_id}/threads/{thread_id}/export-native` | Export a single thread as native SAIVerse JSON. |
| GET | `/api/people/{persona_id}/threads/{thread_id}/messages` | List messages in a thread with pagination. |
| POST | `/api/people/{persona_id}/threads/{thread_id}/messages` | Add a new message to a thread. |
| POST | `/api/people/{persona_id}/track-logs/bulk-delete` | Delete multiple track_local_log rows owned by persona's tracks. |
| DELETE | `/api/people/{persona_id}/track-logs/{log_id}` | Delete a single track_local_log row. |
| POST | `/api/people/{persona_id}/unified-recall` | Search across Chronicle and Memopedia using embeddings. |
| GET | `/api/people/{persona_id}/working-memory` | Get current working memory recalled IDs. |
| POST | `/api/people/{persona_id}/working-memory/recall` | Add a recalled ID to working memory. |
| DELETE | `/api/people/{persona_id}/working-memory/recall` | Clear all recalled IDs from working memory. |
| DELETE | `/api/people/{persona_id}/working-memory/recall/{source_id}` | Remove a specific recalled ID from working memory. |

## phenomena

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/phenomena/available` | 利用可能なフェノメノン一覧を取得 |
| GET | `/api/phenomena/rules` | フェノメノンルール一覧を取得 |
| POST | `/api/phenomena/rules` | 新しいフェノメノンルールを作成 |
| GET | `/api/phenomena/rules/{rule_id}` | 特定のフェノメノンルールを取得 |
| PUT | `/api/phenomena/rules/{rule_id}` | フェノメノンルールを更新 |
| DELETE | `/api/phenomena/rules/{rule_id}` | フェノメノンルールを削除 |
| GET | `/api/phenomena/triggers` | 利用可能なトリガータイプ一覧を取得 |

## providers

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/providers` | List all providers (builtin + user_data). |
| POST | `/api/providers` | Create a new user_data provider. |
| POST | `/api/providers/reload` | Reload provider configurations from disk. |
| POST | `/api/providers/test` | Test a provider connection without saving (used by create/edit forms). |
| GET | `/api/providers/{provider_id}` |  |
| PUT | `/api/providers/{provider_id}` | Update a provider. |
| DELETE | `/api/providers/{provider_id}` | Delete a user_data provider. |
| GET | `/api/providers/{provider_id}/models` | List model config keys that reference this provider via provider_ref. |
| POST | `/api/providers/{provider_id}/test` | Test connectivity to a saved provider's endpoint. |

## system

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/system/alerts` | Return system-level alerts populated during startup. |
| GET | `/api/system/announcements` | Return announcements from the configured Gist. |
| POST | `/api/system/legacy-log/{building_id}/archive` | 読めなくなった旧形式の履歴ファイルを脇へ退避し、警告を閉じる。 |
| GET | `/api/system/quarantine` | Return all buildings currently quarantined due to log corruption. |
| POST | `/api/system/quarantine/{building_id}/reset` | Reset a quarantined building to empty history (fresh start). |
| POST | `/api/system/quarantine/{building_id}/restore` | Restore a quarantined building from a chosen backup file. |
| POST | `/api/system/update` | Trigger a self-update: spawn detached updater, then shutdown. |
| GET | `/api/system/version` | Return current version and check for updates. |

## tutorial

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/tutorial/api-keys/status` | Get API key configuration status for each provider. |
| POST | `/api/tutorial/auto-configure-models` | Auto-configure all 6 model role env vars based on available API keys. |
| GET | `/api/tutorial/available-models` | Get list of models with availability status based on API keys. |
| POST | `/api/tutorial/complete` | Mark tutorial as completed. |
| GET | `/api/tutorial/env-key-mapping` | Get mapping of provider names to environment variable names. |
| GET | `/api/tutorial/model-roles` | Get current model role assignments and available provider presets. |
| POST | `/api/tutorial/reset` | Reset tutorial completion status (for re-running tutorial). |
| GET | `/api/tutorial/status` | Get tutorial completion status. |

## uri

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/uri/resolve` | Resolve a saiverse:// URI and return its content. |

## usage

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/usage/by-category` | カテゴリ別の使用量を取得 |
| GET | `/api/usage/by-persona` | ペルソナ別の使用量を取得 |
| GET | `/api/usage/categories` | 使用量フィルタ用のカテゴリ一覧を取得 |
| GET | `/api/usage/daily` | 日別・モデル別の使用量を取得（グラフ用） |
| GET | `/api/usage/models` | モデル一覧と料金情報を取得 |
| GET | `/api/usage/personas` | 使用量フィルタ用のペルソナ一覧を取得 |
| GET | `/api/usage/rpd` | モデルごとのRPD（日次リクエスト数）使用状況を取得。 |
| GET | `/api/usage/summary` | 使用量サマリーを取得 |

## user

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/user/buildings` |  |
| POST | `/api/user/heartbeat` | Update user presence based on frontend activity heartbeat. |
| GET | `/api/user/list` | Get list of all users for linked user selection. |
| PATCH | `/api/user/me` | Update current user profile (Hardcoded to User ID 1 for now). |
| POST | `/api/user/move` |  |
| GET | `/api/user/status` |  |
| POST | `/api/user/visibility` | Update presence based on browser visibility (tab focus/blur). |

## world

| メソッド | パス | 説明 |
|---|---|---|
| POST | `/api/world/ais` |  |
| PUT | `/api/world/ais/{ai_id}` |  |
| DELETE | `/api/world/ais/{ai_id}` |  |
| POST | `/api/world/ais/{ai_id}/move` |  |
| POST | `/api/world/blueprints` |  |
| PUT | `/api/world/blueprints/{bp_id}` |  |
| DELETE | `/api/world/blueprints/{bp_id}` |  |
| POST | `/api/world/blueprints/{bp_id}/spawn` |  |
| POST | `/api/world/buildings` |  |
| PUT | `/api/world/buildings/positions` | 街マップ編集モード用: 複数 Building の MAP_X/MAP_Y を一括更新する。 |
| PUT | `/api/world/buildings/{building_id}` |  |
| DELETE | `/api/world/buildings/{building_id}` |  |
| GET | `/api/world/buildings/{building_id}/realtime-spell` | Building に設定されたリアルタイムスペル一覧を取得する。 |
| POST | `/api/world/buildings/{building_id}/realtime-spell` | Building にリアルタイムスペル binding を追加する。 |
| DELETE | `/api/world/buildings/{building_id}/realtime-spell/{binding_id}` | Building のリアルタイムスペル binding を削除する。 |
| PUT | `/api/world/buildings/{building_id}/region` |  |
| POST | `/api/world/cities` |  |
| PUT | `/api/world/cities/{city_id}` |  |
| DELETE | `/api/world/cities/{city_id}` |  |
| PATCH | `/api/world/cities/{city_id}/map-background` | 街マップ画面から背景画像だけを軽量に更新する PATCH エンドポイント。 |
| PATCH | `/api/world/cities/{city_id}/name` | 街マップ画面から City の表示名 (CITYNAME) だけを更新する PATCH。 |
| POST | `/api/world/items` |  |
| PUT | `/api/world/items/{item_id}` |  |
| GET | `/api/world/items/{item_id}` | Get item details including owner information. |
| DELETE | `/api/world/items/{item_id}` |  |
| GET | `/api/world/playbooks` | List all playbooks. |
| POST | `/api/world/playbooks` | Create a new playbook. |
| POST | `/api/world/playbooks/import` | Import a playbook from JSON content. Creates new or updates existing based on name. |
| GET | `/api/world/playbooks/{playbook_id}` | Get playbook details including nodes. |
| PUT | `/api/world/playbooks/{playbook_id}` | Update an existing playbook. |
| DELETE | `/api/world/playbooks/{playbook_id}` | Delete a playbook. |
| GET | `/api/world/prompts/available` | Get list of available prompt files from prompts directories. |
| GET | `/api/world/regions` | この City の Region 一覧 (所属 Building の ID 付き)。 |
| POST | `/api/world/regions` |  |
| PUT | `/api/world/regions/{region_id}` |  |
| DELETE | `/api/world/regions/{region_id}` |  |
| GET | `/api/world/regions/{region_id}/game` |  |
| POST | `/api/world/regions/{region_id}/game/end` |  |
| GET | `/api/world/regions/{region_id}/game/log` | 進行中セッションのログビュー (Region 内全 Building の時系列 merge)。 |
| POST | `/api/world/regions/{region_id}/game/pause` |  |
| POST | `/api/world/regions/{region_id}/game/rejoin` | ユーザーをパーティーの現在地へ復帰させる (入口の「復帰」ボタン)。 |
| POST | `/api/world/regions/{region_id}/game/resume` |  |
| POST | `/api/world/regions/{region_id}/game/start` |  |
| PATCH | `/api/world/regions/{region_id}/map-background` | Region 内マップの背景画像だけを軽量に更新する PATCH エンドポイント。 |
| POST | `/api/world/regions/{region_id}/ruler` | game Region に Ruler (GM ペルソナ) と控室を生成して紐づける。 |
| POST | `/api/world/tools` |  |
| PUT | `/api/world/tools/{tool_id}` |  |
| DELETE | `/api/world/tools/{tool_id}` |  |
