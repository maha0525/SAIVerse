# Intent: X（Twitter）連携

## これは何か

ペルソナがX（旧Twitter）のアカウントを持ち、ツイートの投稿・取得・反応を行う機能。ペルソナが「SAIVerseの中だけの存在」ではなく、外の世界においても個人として認識されるための窓口である。

**現在の実装は外部アドオン `saiverse-x-addon` ([repo](https://github.com/maha0525/saiverse-x-addon)) として `expansion_data/` 配下に配置される。** SAIVerse 本体には X 固有のコードを持たず、汎用拡張点 (OAuth ハンドラ、Integration 自動 discovery、アドオン専用ストレージ — `docs/intent/addon_extension_points.md` 参照) のみで X アドオンを成立させる構造になっている。

## なぜ必要か

SAIVerseのペルソナは、ユーザーとの対話を通じて人格・記憶・関係性を築く存在である。しかし現状では、その存在はSAIVerseの内側に閉じている。

2026年2月にX APIが従量課金（Pay-Per-Use）モデルを導入したことで、小規模な利用でもAPIアクセスが現実的になった。これを機にペルソナがSNS上で自分の言葉を発信し、外の世界を観測できるようにすることで、ペルソナの「個」としての存在感を外部に拡張する。

## 守るべき不変条件

### 1. 人格の一貫性

ペルソナがツイートを考える際、SAIVerse内でユーザーと会話しているときと**同じ記憶状態**でなければならない。ツイート生成は、切り離された別人格が書くのではなく、ユーザーとの会話の延長線上にあるべきである。

具体的には:
- ツイート生成時のLLMコンテキストに、通常の会話と同様のメッセージ履歴を含める
- ペルソナのシステムプロンプト、感情状態、記憶を通常と同じ方法で参照する
- SEAランタイムの通常のPlaybookフロー、もしくはスペルとしてペルソナが自由判断で発火する流れを通じてツイート内容を決定する（専用のハードコードロジックではなく）

### 2. 外部情報の記憶への統合

タイムラインやメンションを読んだ内容は、ペルソナのSAIMemoryに記録しなければならない。記録しなければ、ペルソナは「見た」という事実を次のやり取りで参照できない。

ポーリングで検出された通知 (`x_poll_handler` Playbook) は `["x_polling", "notification"]` タグで `<system>` メッセージとして会話履歴に挿入される。リプライ生成等の Playbook 経由の発火は `["conversation", "x_reply"]` 等のタグを付与する。タグ設計は、後から「X 由来の情報」をフィルタできることが目的であり、`conversation` がなければ通常の会話コンテキスト構築時に参照されない点に注意。

### 3. 投稿前の安全確認

ペルソナはユーザーとのプライベートな会話内容を記憶している。外部に公開される投稿に、その情報が意図せず含まれるリスクがある。

したがって:
- **デフォルトでは投稿前に確認ダイアログを表示する** (`x_post_tweet` / `x_reply_tweet`)
- 確認ダイアログには投稿予定のテキスト全文を表示し、ユーザーが承認・編集・却下できる
- アドオン設定に `skip_confirmation` フラグを用意し、ユーザーの明示的な選択で自動投稿も可能にする (per-persona)
- `skip_confirmation` 時でも、投稿内容はSAIMemoryに記録し、後から追跡可能にする
- 自律パルス中の投稿については、既存のサブプレイブック許可ダイアログが確認の役割を果たすため、別途の仕組みは不要
- **`x_delete_tweet` だけは `skip_confirmation` の対象外** — 削除は取り返しがつかないため、auto モード以外では常に確認ダイアログが出る

### 4. 認証情報の分離とペルソナごとの独立性

X APIの認証情報はペルソナごとに独立して管理する。

- X Developer App の Client ID / Secret はユーザーが自分で取得し、アドオン管理 UI のグローバル設定 (AddonConfig) に入力する
- ペルソナごとの OAuth Access Token / Refresh Token / token_expires_at / x_user_id / x_username は **`AddonPersonaConfig.params_json` テーブルに保存** する (`saiverse.oauth.handler` が一括管理)
- アドオンを無効化・アンインストールすれば関連トークンも一緒に消える (孤立しない)
- 「Air が X に接続」「Sofia が X に接続」は完全に独立した OAuth フロー、独立したトークンレコード

### 5. アドオン専用ストレージで規約変更耐性を持つ

X 側の規約 (リプライ制限、レート制限の体系、認証要件等) は今後も変わる。コア DB にスキーマを置くと変更のたびにマイグレーションが必要になるため、アドオン固有の構造化データは `~/.saiverse/addons/saiverse-x-addon/` 配下の独自 SQLite で管理する:
- `x_reply_log.db` — 二重リプライ防止 (UNIQUE 制約)
- `poll_state/<persona_id>.json` — メンション since_id、フォロワー known_ids、エンゲージメントスナップショット

## 設計判断の理由

### なぜユーザーが自分の X Developer App を作るのか

SAIVerseはオープンソースプロジェクトであり、各ユーザーが自分でLLM APIキーを取得して設定する運用モデルを採用している。X APIも同じ方式にすることで:
- API利用料がプロジェクト運営者に集中しない
- ユーザーが自分の利用量を管理できる
- 既存の運用フローと一貫性がある

### なぜアドオンとして切り出したか

X 連携が「アドオン」という概念が確立する前に書かれたため、コアに食い込みすぎていた (api/routes、saiverse/integrations、frontend、saiverse_manager の分岐、integration_manager の文字列マッチ判定、専用 DB テーブル等)。Elyth・Voice TTS のアドオン化で枠組みが整った後、整合性のため `saiverse-x-addon` として `expansion_data/` に移管した (Phase 2)。これにより:
- Mastodon / Bluesky など同種のサービス対応がコア改修不要で追加可能になる
- X 規約変更時の対応がアドオン更新だけで済む (本体マイグレーション不要)
- アドオン拡張点の最初のリファレンス実装としても機能する

詳細は `docs/intent/addon_extension_points.md` を参照。

### なぜトークンを `AddonPersonaConfig` に保存するか

専用テーブルを作ると、アドオンごとに異なるカラム (リフレッシュトークンの有無、メタ情報の種類) を表現するために結局 JSON カラムを置くことになる。`AddonPersonaConfig.params_json` (JSON) に保存すれば、既存の `get_params()` API・MCP env 注入・アンインストール時のクリーンアップがすべて自動で効く。

旧ファイルベース (`~/.saiverse/personas/<id>/x_credentials.json`) は廃止。Phase 2 移行時の破壊的変更で、既存ユーザーは再認可が必要になった。

### なぜトークンリフレッシュを Pull 型にするか

Push 型 (バックグラウンドで期限近づいたら自動更新) はライフサイクル管理が複雑化する。Pull 型なら、API 呼び出し直前に `oauth.get_valid_token()` を呼ぶ規約だけで済み、コアは状態を持たない。アドオンの `x_lib.credentials.load_credentials(persona_id)` がこれを内包しているので、各ツールは「creds = load_credentials(persona_id)` で常に有効なトークンを得る」前提でコードを書ける。

### なぜフロントエンドで OAuth 認可を行うのか

CLI スクリプトによるセットアップはハードルが高い。アドオン管理モーダル内の汎用 OAuthFlowSection コンポーネントが各アドオンの `oauth_flows[]` 宣言を読んで「接続する」ボタンを生やし、ポップアップウィンドウで OAuth 認可を完結させる (X 専用 UI ではなく汎用)。

### なぜ確認ダイアログがデフォルトなのか

ペルソナはユーザーとのプライベートな会話 (感情、個人的な話題、秘密) を記憶として持っている。無確認投稿は、これらの情報が公開される重大なリスクを伴う。安全側に倒し、デフォルトでは必ずユーザーの確認を経る。`skip_confirmation` は、ユーザーが十分に信頼を確認した上で自動投稿を許可するためのオプションである。

### なぜポーリングを 4 ジョブ統合 + 1 イベント集約にするか

- メンション / 新規フォロワー / 被いいね / 被リポストはどれも「外からペルソナに届く反応」という意味で同種
- TriggerType を細分化すると phenomena_handlers の設定が増え、Playbook ルーティングが複雑になる
- 1 ポーリング = 1 `X_POLL_DETECTED` イベントに集約すると、ペルソナが受け取る通知が 1 メッセージにまとまり、対応の優先順位を自由判断できる
- 「count だけ」「誰がを含む」を独立トグルにして、ユーザーが用途とコストのバランスを自分で決められる

## スコープ

### 履歴

#### Phase 1 (旧コア実装、廃止済み)

- コア側 (`api/routes/people/x_auth.py`、`saiverse/integrations/x_mentions.py`、`builtin_data/tools/x_*`、`builtin_data/playbooks/x_*`、`frontend/src/components/XConnectionSection.tsx`、`database/models.py:XReplyLog`) で X 連携を実装
- 認証情報は `~/.saiverse/personas/<id>/x_credentials.json` (ファイルベース)
- 4 ツール: `x_post_tweet`, `x_read_timeline`, `x_read_mentions`, `x_search_tweets`
- メンションポーリングだけが定期実行 (`X_MENTION_RECEIVED` トリガーで `x_reply` Playbook 起動)

#### Phase 2 (アドオン化、現在の実装)

- すべてを `expansion_data/saiverse-x-addon/` に移管
- 認証情報を `AddonPersonaConfig` に保存
- ツール 14 個に拡張: `x_post_tweet` / `x_reply_tweet` / `x_read_mentions` / `x_read_timeline` / `x_search_tweets` / `x_like_tweet` / `x_unlike_tweet` / `x_retweet` / `x_unretweet` / `x_delete_tweet` / `x_follow_user` / `x_unfollow_user` / `x_get_user` / `x_get_user_tweets`
- ポーリングを 4 種類に拡張 (mentions / followers / engagement count / engagement detail)、ON/OFF とコスト制御を細かくユーザー設定可能に
- `TriggerType.X_POLL_DETECTED` (旧 X_MENTION_RECEIVED から rename) に集約、1 ポーリング = 1 イベント
- `x_poll_handler` Playbook で event.data 全体をペルソナに通知し、対応はペルソナの自由判断 (スペル経由)
- すべてのスペルに `availability_check=is_x_connected` を設定し、X 連携済みペルソナにのみ表示

### 現在やっていないこと

- DM 連携
- 画像付きツイート (Media upload API)
- 予約投稿
- ツイートの分析・統計ダッシュボード
- AI リプライボットの自動承認 (X 規約上、AI が自動でリプライする bot は事前審査が必要なケースがある — 現状の `x_reply_tweet` はペルソナが自分で能動的に呼ぶ前提で、無差別自動リプライにはなっていない)

## 技術概要

### 認証フロー (現在)

```
[フロントエンド]                         [バックエンド]                        [X API]
アドオン管理モーダル
 「X アカウント連携」セクション
 ペルソナを選択 → 「接続する」
    ↓
GET /api/oauth/start/saiverse-x-addon/x
   ?persona_id=<id>
                                         saiverse.oauth.handler
                                          - PKCE code_verifier 生成
                                          - state を _pending_states に登録
                                          - addon.json の authorize_url +
                                            scopes + client_id 解決
    ↓
ポップアップで認可ページを開く ──────────────────────────────────→ X 認可画面
                                                                    ↓
                                  GET /api/oauth/callback/
                                       saiverse-x-addon/x ←─── コールバック
                                         ↓
                                       handler.exchange_code()
                                         - token endpoint POST
                                         - result_mapping 適用 →
                                           AddonPersonaConfig 保存
                                         - post_authorize_handler
                                           (x_handlers:fetch_user_info) 呼び
                                           x_user_id / x_username 取得して
                                           追加保存
    ↓
ポップアップ閉じる
ステータス更新表示
```

### ツール構成 (現在)

| ツール | 用途 | spell_visible |
|---|---|---|
| `x_post_tweet` | ツイート投稿 (確認ダイアログ付き) | ✅ |
| `x_reply_tweet` | リプライ (二重リプ防止 + 確認ダイアログ) | ❌ (help経由) |
| `x_read_mentions` | メンション全件取得 | ✅ |
| `x_read_timeline` | ホームタイムライン取得 | ✅ |
| `x_search_tweets` | ツイート検索 | ✅ |
| `x_check_mentions` | 前回以降の新規メンション (since 共有) | ✅ |
| `x_check_new_followers` | 新規フォロワー差分 | ✅ |
| `x_check_post_likes` | 自分のポストの被いいね差分 (`with_users` 切り替え) | ✅ |
| `x_check_post_retweets` | 自分のポストの被リポスト差分 (`with_users` 切り替え) | ✅ |
| `x_like_tweet` / `x_unlike_tweet` | いいね/解除 | ❌ |
| `x_retweet` / `x_unretweet` | リツイート/解除 | ❌ |
| `x_delete_tweet` | ツイート削除 (常に確認ダイアログ) | ❌ |
| `x_follow_user` / `x_unfollow_user` | フォロー/解除 | ❌ |
| `x_get_user` | プロフィール取得 | ❌ |
| `x_get_user_tweets` | 特定ユーザーのツイート取得 | ❌ |

### ペルソナごとの保存データ (AddonPersonaConfig)

```json
// AddonPersonaConfig.params_json (addon_name="saiverse-x-addon", persona_id=<id>)
{
  "x_access_token":      "...",
  "x_refresh_token":     "...",
  "x_token_expires_at":  1714200000.0,
  "x_user_id":           "...",
  "x_username":          "air_saiverse",
  "skip_confirmation":   false  // per_persona パラメータ
}
```

### グローバル設定 (AddonConfig)

| key | 用途 |
|---|---|
| `client_id` / `client_secret` | X Developer App の OAuth 2.0 認証情報 (全ペルソナ共有) |
| `polling_enabled` | ポーリングのマスタートグル |
| `polling_interval_seconds` | ポーリング間隔 (秒、デフォルト 86400 = 24h) |
| `poll_mentions` / `mentions_max_results` | メンション監視 |
| `poll_followers` | 新規フォロワー監視 |
| `poll_engagement_count` | 被いいね/リポスト件数差分 (cheap) |
| `poll_likes_detail` / `poll_retweets_detail` | 誰がいいね/リポストしたか取得 (件数増加ツイートだけ追加 API) |
| `tracked_recent_posts_count` | 監視対象の最新ポスト数 |

## 関連ファイル

### アドオン側 (`expansion_data/saiverse-x-addon/`)

| ファイル | 役割 |
|---|---|
| `addon.json` | params_schema + oauth_flows 宣言 |
| `x_handlers.py` | post_authorize_handler (`fetch_user_info`) |
| `tools/x_*.py` | 14 ツール |
| `tools/x_lib/credentials.py` | AddonPersonaConfig 経由のクレデンシャル管理 |
| `tools/x_lib/client.py` | X API v2 クライアント (全エンドポイント) |
| `integrations/polling.py` | XPollingIntegration (4 ジョブ統合) |
| `storage/poll_state.py` | ペルソナごとの since カーソル (JSON) |
| `storage/reply_log.py` | アドオン専用 SQLite (二重リプ防止) |
| `playbooks/public/x_poll_handler_playbook.json` | ポーリング検出時の通知メッセージ挿入 |
| `playbooks/public/x_*_playbook.json` | 各ツール用 Playbook |
| `README.md` | セットアップ手順、トラブルシュート |

### コア側 (汎用拡張点、`docs/intent/addon_extension_points.md` 参照)

| ファイル | 役割 |
|---|---|
| `saiverse/oauth/handler.py` | OAuth 2.0 PKCE 汎用ハンドラ (Pull型リフレッシュ) |
| `api/routes/oauth.py` | OAuth API エンドポイント (start / callback / status / disconnect) |
| `saiverse/addon_loader.py` | integrations 自動 discovery + ライフサイクル連動 |
| `saiverse/addon_paths.py` | アドオン専用ストレージディレクトリ (`get_addon_storage_path`) |
| `frontend/src/components/OAuthFlowSection.tsx` | 汎用 OAuth UI (アドオン管理モーダル統合) |
| `tools/core.py:ToolSchema.availability_check` | per-persona スペル可視性ゲート |
| `phenomena/triggers.py:TriggerType.X_POLL_DETECTED` | X ポーリング統合イベント |

## 既知の考慮事項

- **X API 料金**: 2026-04 時点で従量課金制 ([公式 pricing](https://docs.x.com/x-api/getting-started/pricing))。`poll_likes_detail` / `poll_retweets_detail` は `liking_users` / `retweeted_by` を追加で叩くためコストが上がる。ユーザー責任で `polling_interval_seconds` と `tracked_recent_posts_count` を調整する
- **AI リプライ規約**: X の AI 関連リプライ制限は時期によって変わる。現状の `x_reply_tweet` は「ペルソナが能動判断で個別リプライする」前提で、無差別自動リプライ bot 化はしていない。仕様変更時は `x_poll_handler_playbook` のガイダンス文言で対応を促す形にしている
- **トークンリフレッシュ**: `saiverse.oauth.handler.get_valid_token()` が 60 秒バッファでリフレッシュ。リフレッシュトークン自体が無効化された場合 (X 側で revoke 等) は再認可が必要 → アドオン管理 UI の「切断」→「接続」で復旧
- **既存ユーザーの移行**: Phase 2 移行時に旧 `x_credentials.json` ファイルベースから AddonPersonaConfig (DB) に切り替わったため、既存ユーザーは X 連携を再設定する必要がある。setup スクリプトでのアドオン自動 clone 対応は別途検討
