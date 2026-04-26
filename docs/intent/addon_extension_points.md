# Intent: アドオン拡張点の整備（OAuth・Integration）と X 連携の切り出し

**ステータス**: ドラフト（インタビュー反映済み、まはーレビュー待ち）

## これは何か

現状のアドオン基盤（`addon.json` + `AddonConfig` / `AddonPersonaConfig` + `mcp_servers.json` + `api_routes.py` 自動マウント）に、**OAuth認可フロー**・**ポーリング型インテグレーション**・**アドオン専用ストレージ**の3つの宣言的な拡張点を追加する。あわせて、これらの拡張点を使って既存のX連携を `saiverse-x-addon` として `expansion_data/` へ切り出し、コアからX固有のコードを撤去する。

## なぜ必要か

### 問題1: アドオンが OAuth 認可を表現できない

現状のアドオン基盤で表現できる「外部サービス接続」は、ユーザーがどこかで取得したAPIキーを `addon.json` の `params_schema` に手入力する形に限られる（Elythパターン）。一方で X / Mastodon / Bluesky / Google系サービスなど、認可がOAuthベースのサービスは:

- ユーザー入力ではトークンを得られない（ブラウザでの認可フローが必須）
- アクセストークンに有効期限があり、リフレッシュトークンでの自動更新が必要
- PKCE / scope / redirect URI の管理が必要

これらは「APIキー1個を貼るだけ」のparams_schemaでは表現できず、現状はXのために `api/routes/people/x_auth.py` と `frontend/src/components/XConnectionSection.tsx` をコア側に専用実装している。今後 Mastodon / Bluesky 等を追加するたびにコア改修が必要になるのは明らかに筋が悪い。

### 問題2: ポーリング型インテグレーションをアドオンから登録できない

`saiverse/integrations/base.py` の `BaseIntegration` と `saiverse/integration_manager.py` は既に汎用的な仕組みになっているが、登録経路が `saiverse_manager.py:339` でハードコード:

```python
from saiverse.integrations.x_mentions import XMentionIntegration
self.integration_manager.register(XMentionIntegration())
```

さらに `IntegrationManager._is_integration_enabled` (`integration_manager.py:124`) で `integration.name == "x_mentions"` の文字列マッチで有効性判定している。これでは新しいインテグレーションを追加するたびにコアにif分岐を増やす必要がある。

`docs/intent/external_event_integration.md` ですでに「ポーリング型基本、プッシュ型は将来」という方針が確定している以上、ポーリング型を**アドオンから素直に追加できる**ようにするのが本筋。

### 問題3: X連携がコアに食い込みすぎている

X連携の現状の広がり:

| 層 | ファイル | 配置先（移行後） |
|---|---|---|
| ツール | `builtin_data/tools/x_*.py` (5本) + `x_lib/` | `expansion_data/saiverse-x-addon/tools/` |
| Playbook | `builtin_data/playbooks/public/x_*_playbook.json` (5本) | `expansion_data/saiverse-x-addon/playbooks/public/` |
| OAuth API | `api/routes/people/x_auth.py` | `expansion_data/saiverse-x-addon/api_routes.py` |
| ポーリング | `saiverse/integrations/x_mentions.py` | `expansion_data/saiverse-x-addon/integrations/mentions.py` |
| 資格情報 | `~/.saiverse/personas/<id>/x_credentials.json` (XCredentials dataclass) | `AddonPersonaConfig` |
| フロントUI | `frontend/src/components/XConnectionSection.tsx` | 削除 → 汎用OAuthボタン |
| 起動ロジック | `saiverse_manager.py:339` (ハードコード登録) | 削除 → integrations自動discovery |
| 有効化判定 | `integration_manager.py:124` (`x_polling_enabled`文字列マッチ) | 削除 → アドオン有効状態を参照 |

これは「アドオン」という概念が確立する前に書かれたコードだが、Elythアドオンの実装で枠組みが整った今、整合性のために移行する。

## 守るべき不変条件

### 1. OAuth認可フローはアドオン宣言で完結する

`addon.json` に `oauth_flows` セクションを書けば、コアの汎用OAuthハンドラが認可URL生成・コールバック処理・トークン保存・リフレッシュを引き受ける。アドオン作者が `api_routes.py` にOAuthのボイラープレートを書く必要はない（書きたい場合は書ける、後方互換）。

### 2. OAuth トークンも他のparamsと同じ流儀で扱う

OAuth認可で得たアクセストークン・リフレッシュトークン・有効期限・関連メタ情報（X user_id, username等）は、専用の保管場所ではなく **`AddonPersonaConfig`** に保存する。`get_params(addon_name, persona_id=...)` で他のparamsと同じインターフェースで読める。

これにより:
- ペルソナ設定モーダルで「現在の接続ステータス」「切断ボタン」を汎用UIで表現できる
- `${persona.addon.<name>.access_token}` 構文でMCP env注入できる
- アドオンのアンインストールで関連トークンも一緒に消える（孤立しない）

### 3. ペルソナごとのアカウント分離

OAuth認可は **必ず persona_id をスコープに持つ**。「Air が X に接続」「Sofia が X に接続」は完全に独立した認可フローで、別々のトークンが別々のAddonPersonaConfigレコードに保存される。グローバルなOAuth接続は許さない（OAuthが必要なサービスは事実上 per-persona アカウントだから）。

### 4. ポーリング統合のライフサイクルはアドオン有効状態に従う

`expansion_data/<addon>/integrations/*.py` で `BaseIntegration` を継承したクラスは、アドオン有効化時に自動登録、無効化時に自動アンレジスタ。`IntegrationManager._is_integration_enabled` の文字列マッチ判定は撤去し、「登録されている = 有効」とする（無効化されたアドオンのintegrationは登録されない）。

### 5. X 連携の人格一貫性は保持する

`docs/intent/x_integration.md` で確定済みの不変条件（人格一貫性、確認ダイアログ、外部情報のSAIMemory統合）は移行後も維持する。アドオン化はあくまで配置の変更であり、振る舞いの変更ではない。

### 6. 移行は破壊的変更でよい（後方互換性なし）

X連携は実質まはー一人だけが使っている状態のため、後方互換性のための移行レイヤー（旧 `x_credentials.json` を読んで `AddonPersonaConfig` にコピーする等）は実装しない。既存トークンは無効化、ユーザーは再度OAuth認可をやり直す。

### 7. アドオンが独自スキーマのデータを持てる

X の `x_reply_log` のように、アドオン固有の構造化データ（重複防止ログ、キャッシュ、独自履歴等）を保存できる規約をコアが用意する。コアの `saiverse.db` には触らせず、アドオン専用のSQLiteファイル（または任意のファイル）を持てる場所を提供する。X側の規約が頻繁に変わる以上、コアのDBスキーマ変更を伴わずアドオン更新だけで対応可能にする。

## 設計

### A. addon.json の `oauth_flows` セクション

```json
{
  "name": "saiverse-x-addon",
  "display_name": "X (Twitter) 連携",
  "version": "0.1.0",
  "params_schema": [
    {
      "key": "client_id",
      "label": "X API Client ID",
      "description": "X Developer Portal で発行された OAuth 2.0 Client ID",
      "type": "text"
    },
    {
      "key": "client_secret",
      "label": "X API Client Secret",
      "type": "password"
    },
    {
      "key": "skip_confirmation",
      "label": "投稿前確認をスキップ",
      "type": "boolean",
      "default": false,
      "persona_configurable": true
    }
  ],
  "oauth_flows": [
    {
      "key": "x",
      "label": "X アカウント連携",
      "description": "ペルソナを X (Twitter) アカウントに接続します",
      "provider": "oauth2_pkce",
      "authorize_url": "https://twitter.com/i/oauth2/authorize",
      "token_url": "https://api.twitter.com/2/oauth2/token",
      "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access"],
      "client_id_param": "client_id",
      "client_secret_param": "client_secret",
      "callback_path": "/api/oauth/callback/x",
      "result_mapping": {
        "access_token": "x_access_token",
        "refresh_token": "x_refresh_token",
        "expires_at": "x_token_expires_at"
      },
      "post_authorize_handler": "x_handlers:fetch_user_info"
    }
  ]
}
```

**フィールド説明**:

- **`provider`**: OAuthフレーバー。`oauth2_pkce` / `oauth2_client_credentials` / `oauth1_3legged` などをコアがサポート。Bluesky の AT Protocol など特殊な認可は将来対応。
- **`client_id_param` / `client_secret_param`**: グローバル `params_schema` のキー名を参照。ユーザー自身のDeveloper App credentialsを使う設計（`docs/intent/x_integration.md` の方針を継承）。
- **`callback_path`**: コアの汎用OAuthハンドラがこのパスでルートを生やす。アドオン名と key からコア側で自動算出してもよい（要検討）。
- **`result_mapping`**: トークンエンドポイントのレスポンスを `AddonPersonaConfig.params_json` のどのキーに保存するかのマッピング。
- **`post_authorize_handler`**: 認可成功後にユーザー情報取得などを行うフック。`module_name:function_name` 形式で `expansion_data/<addon>/<module>.py` を参照。引数は `(persona_id, tokens, params)`、戻り値は追加でparamsに保存する dict。

### B. コアの汎用 OAuthハンドラ

新規モジュール: `saiverse/oauth/handler.py`

責務:
1. **認可URL生成**: `GET /api/oauth/start/{addon_name}/{flow_key}?persona_id=...` → state生成 → addon.jsonの authorize_url + scopes + PKCE challenge から認可URLを組み立てて返す
2. **コールバック処理**: addon.jsonで宣言された `callback_path` でルートを生やし、code/state を受け取って token endpoint を叩き、`result_mapping` に従って `AddonPersonaConfig` に保存
3. **トークンリフレッシュ**: アドオンのコードから `oauth.get_valid_token(addon_name, flow_key, persona_id)` を呼ぶと、期限切れなら自動でリフレッシュして返す
4. **切断**: `DELETE /api/oauth/{addon_name}/{flow_key}/{persona_id}` で関連paramsをAddonPersonaConfigから削除
5. **ステータス取得**: `GET /api/oauth/{addon_name}/{flow_key}/{persona_id}/status` で接続状態を返す（汎用UIが表示に使う）

state管理は現状の `_pending_oauth: Dict[str, Dict]` パターンを汎用化（in-memory、サーバー再起動で消えるが認可フローは数分で完了するため許容）。将来的にDB保存にする可能性は残すが、Phase 1スコープ外。

**トークンリフレッシュ方式**: Pull型のみ採用。アドオンのコードが API 呼び出し直前に `oauth.get_valid_token(addon_name, flow_key, persona_id)` を呼び、期限切れならその場でリフレッシュして返す。バックグラウンドで期限近づいたら自動更新する Push 型はライフサイクル管理が複雑になるため Phase 1 では実装しない。

### C. `expansion_data/<addon>/integrations/` 自動discovery

`addon_loader.py` に新メソッド `load_addon_integrations(integration_manager)` を追加:

```python
def load_addon_integrations(integration_manager) -> None:
    for addon_dir in EXPANSION_DATA_DIR.iterdir():
        if not is_addon_enabled(addon_dir.name):
            continue
        integrations_dir = addon_dir / "integrations"
        if not integrations_dir.exists():
            continue
        for py_file in integrations_dir.glob("*.py"):
            module = _import_addon_module(addon_dir.name, py_file)
            for cls in _find_base_integration_subclasses(module):
                integration_manager.register(cls())
```

**起動順序**: `main.py` で `IntegrationManager` 初期化 → `load_addon_integrations()` → `integration_manager.start()`。

**ライフサイクル**: 現状の `notify_addon_toggled_sync` (MCP連動で使われている) を拡張し、アドオン有効化/無効化イベントで integration の register/unregister を行う。`IntegrationManager.unregister(name)` を新規追加。

**`_is_integration_enabled` の撤去**: 「登録されているintegrationはすべて有効」とする。アドオンが無効化されたら登録自体が解除されるので、有効性チェックは不要。

### E. アドオン専用ストレージ規約

新規モジュール: `saiverse/addon_paths.py`

```python
from pathlib import Path
from saiverse.data_paths import get_saiverse_home


def get_addon_storage_path(addon_name: str) -> Path:
    """アドオン専用のディスクストレージディレクトリを返す。

    アドオンが独自スキーマの SQLite DB やキャッシュファイルを置く場所。
    コアの saiverse.db には絶対に触らせない。

    Returns:
        ~/.saiverse/addons/<addon_name>/  (存在しなければ作成)
    """
    path = get_saiverse_home() / "addons" / addon_name
    path.mkdir(parents=True, exist_ok=True)
    return path
```

**配置**: `~/.saiverse/addons/<addon_name>/` 配下。`expansion_data/` (リポジトリ内、コード) と分離する。理由は:
- アドオンを `git pull` でアップデートしても、ユーザーデータ（DB、ログ等）が消えない
- ペルソナ別データは `~/.saiverse/personas/<id>/` 配下なので、アドオン共有データはこちらに置く

**アンインストール時のクリーンアップ**: アドオンを物理削除した際、この配下のディレクトリも削除するかは Phase 1 では自動化しない（誤削除防止のためデフォルトは残す）。将来的にアドオン管理UIに「ストレージも削除」オプションを置く想定。

**用途例**:
- X アドオンの `x_reply_log.db` (重複リプライ防止)
- 各アドオンのレート制限カウンタ、キャッシュ
- アドオン独自のローカル状態

### F. フロントエンドの汎用 OAuthボタン

`AddonManagerModal` 内のアドオン詳細ペインで、`oauth_flows` を持つアドオンに対して **接続セクション** を表示:

```
┌─────────────────────────────────┐
│ X アカウント連携                │
│                                 │
│ 状態: 未接続                    │
│ [接続する] ボタン               │
│                                 │
│ ─── または ───                  │
│                                 │
│ 状態: 接続済み (@air_air2026)   │
│ [切断する] ボタン               │
└─────────────────────────────────┘
```

実装方針:
- `XConnectionSection.tsx` の構造（ポップアップ起動 → ポーリング → ステータス更新）を汎用 `OAuthFlowSection.tsx` として書き直す
- `addon.json` の `oauth_flows[].label` / `description` をそのまま表示
- ペルソナ選択UIを横に置く（per_persona なので接続対象ペルソナを選ばせる）
- 旧 `XConnectionSection.tsx` は削除

ペルソナ設定モーダル側からも触れた方が体験はよさそうだが、まずはAddonManagerに集約。将来 SettingsModal から「X連携設定はアドオン管理画面で」リンクを置く形で良い。

### G. X 連携の切り出し作業

新規ディレクトリ: `expansion_data/saiverse-x-addon/`

```
expansion_data/saiverse-x-addon/
├── addon.json                           ← oauth_flows + params_schema
├── api_routes.py                        ← OAuth以外のX固有ルート（必要なら）
├── x_handlers.py                        ← post_authorize_handler の実装
├── tools/
│   ├── x_lib/
│   │   ├── client.py                    ← builtin_data から移動
│   │   └── credentials.py               ← AddonPersonaConfig 経由で読むよう書き換え
│   ├── x_post_tweet.py
│   ├── x_read_mentions.py
│   ├── x_read_timeline.py
│   ├── x_reply_tweet.py
│   └── x_search_tweets.py
├── playbooks/
│   └── public/
│       ├── x_post_playbook.json
│       ├── x_read_mentions_playbook.json
│       ├── x_read_timeline_playbook.json
│       ├── x_reply_playbook.json
│       └── x_search_tweets_playbook.json
├── integrations/
│   └── mentions.py                      ← XMentionIntegration を移動
└── storage/
    └── reply_log.py                     ← x_reply_log の独自テーブル管理
```

**`x_reply_log` の扱い**: コア側の `database/models.py` から `XReplyLog` テーブル定義を削除し、X アドオン専用 SQLite DB として `~/.saiverse/addons/saiverse-x-addon/x_reply_log.db` に移管。`storage/reply_log.py` で SQLAlchemy ベースの薄いラッパーを実装し、`x_reply_tweet.py` ツールから呼び出す。これにより X 側の規約変更（リプライ制限の仕様変更、新しい重複判定キー等）にアドオン更新だけで対応できる。

**コア側の撤去ファイル**:
- `api/routes/people/x_auth.py` 削除
- `saiverse/integrations/x_mentions.py` 削除
- `frontend/src/components/XConnectionSection.tsx` + `.module.css` 削除
- `saiverse/saiverse_manager.py:337-341` の XMentionIntegration ハードコード登録削除
- `saiverse/integration_manager.py:117-128` の `_is_integration_enabled` 撤去（メソッド自体削除、`_poll_integration` から呼び出しも削除）
- `manager/state.py:16-17` の `_default_x_polling_enabled` と `state.x_polling_enabled` 削除
- `builtin_data/tools/x_*.py` (5本) + `x_lib/` 削除
- `builtin_data/playbooks/public/x_*_playbook.json` (5本) 削除
- `database/models.py` のX関連カラム（あれば）+ `XReplyLog` テーブル定義削除 → マイグレーション

**`x_lib/credentials.py` の書き換え**: `XCredentials` dataclass を残しつつ、`load_credentials` / `save_credentials` の中身を `AddonPersonaConfig` 読み書きに置換。ファイルベース保存は廃止。tools 内の呼び出し側はインターフェース変更なし。

**`tools/context.py` への影響なし**: `get_active_persona_id()` で persona_id を取れるので、`load_credentials(persona_id)` の形に変えるだけ。

### H. データベースマイグレーション

新規マイグレーション `database/migrations/NNNN_remove_x_artifacts.py`:
- persona テーブルからX関連カラム（あれば）DROP
- `x_reply_log` テーブル DROP（アドオン側で独自SQLiteとして再構築するため、コア側から消す）
- 既存ユーザーには「X連携を再設定してください」と案内（README/CHANGELOG）

旧 `~/.saiverse/personas/<id>/x_credentials.json` ファイルは触らない（アドオン側からも参照しないので、放置で害なし）。

旧 `x_reply_log` のレコードは破棄する。理由は: 既存トークンを無効化して再認可させる以上、新環境で同じツイートに再リプライが発生する確率は極めて低い。データ移行のコストに見合わない。

## 設計判断の理由

### なぜ `oauth_flows` を `params_schema` の type で表現せず、別セクションにするか

OAuth フローは「ボタンを押す → ポップアップ → コールバック → トークン保存 → リフレッシュサイクル」という**動的な振る舞い**を伴う。一方 `params_schema` は「ユーザーが値を入力するフォーム項目」を宣言的に表現するもの。両者を同じスキーマに混ぜると `params_schema` の意味が肥大化する。

別セクションに分けることで:
- params_schema は純粋なフォーム宣言として保つ
- OAuth結果の保存先は params_schema 側のキー（`x_access_token` 等）として宣言できる → 既存のparams読み出し API がそのまま使える
- 1つのアドオンで複数のOAuth接続（例: Mastodon用とX用）を持つ場合も配列で表現できる

### なぜトークンを `AddonPersonaConfig` に保存し、専用テーブルを作らないか

専用テーブルを作ると:
- アドオンごとにスキーマが異なるカラム（リフレッシュトークンの有無、メタ情報の種類）を表現するために結局 JSON カラムを置くことになる
- アドオンのアンインストール時に「専用テーブルからも消す」処理を別途書く必要がある
- `${persona.addon.<name>.access_token}` のparams参照構文が使えない

`AddonPersonaConfig.params_json` (JSON) に保存すれば、既存の `get_params()` API・MCP env注入・アンインストール時のクリーンアップがすべて自動で効く。

### なぜ「BaseIntegration の有効性判定 = 登録されているか否か」とするか

現状の「登録は常に行い、有効性は別フラグで判定」パターンは、X連携を有効化していないユーザーでも `XMentionIntegration.poll()` がスキップ前にスレッドを起こす無駄を生んでいる。アドオン有効化と連動して register/unregister すれば、無効化されたアドオンのintegrationはスレッドのworking setに乗らず、ロジックも単純になる。

### なぜトークンリフレッシュを Pull 型のみにするか

Push 型（バックグラウンドで期限近づいたら自動更新）は便利だが、ライフサイクル管理が複雑化する:
- リフレッシュスレッドの起動/停止をアドオン有効/無効と連動させる必要がある
- 全アドオン×全ペルソナ分のリフレッシュタイマーをコアが抱える必要がある
- トークン期限管理を複数の主体（コア + アドオンコード）が触ると不整合が起きやすい

Pull 型なら、API 呼び出し直前に `oauth.get_valid_token()` を呼ぶ規約だけで済み、コアは状態を持たない。アドオン作者にとっても「APIを叩く前に1行呼ぶ」だけのシンプルな契約。

### なぜ OAuth state を in-memory で許容するか

OAuth認可フローは「認可URLを開く → ユーザー操作 → コールバック」が数十秒〜数分で完了する短命トランザクション。サーバー再起動が認可中に発生する確率は実用上ほぼゼロで、再起動された場合もユーザーは「再度接続ボタンを押す」だけで復帰できる。

DB保存にすると、stateレコードのGC（古いstateの掃除）、トランザクション境界、テーブル追加によるマイグレーション、と Phase 1 のスコープに見合わないコストが発生する。将来DBバックエンドを足したくなったら handler 内のstate管理を差し替える形で対応可能（インターフェースが内部閉じている）。

### なぜ x_reply_log をアドオン側で独自テーブルにするか

X側の規約（リプライ制限、レート、認証要件、二重判定キー等）は今後も変更される可能性が高い。コアの `database/models.py` にテーブルを置くと、X規約変更のたびにコア側のマイグレーションが必要になり、コアの変更頻度・テストコストが上がる。

アドオン側で独自SQLiteを持てば:
- アドオン更新だけでスキーマ変更可能（コアのマイグレーション不要）
- アドオンを無効化すればテーブルもアクセスされなくなる（孤立DBの問題はあるが、アドオン管理UIで明示削除可能）
- 他のアドオン（Mastodon、Bluesky）も同じパターンで自身の重複防止ログを持てる

このパターンを正規化するために、コア側に `get_addon_storage_path()` だけ用意する（DB管理はアドオンに任せる）。

### なぜ X 連携を `saiverse-x-addon` という名前で expansion_data に置くか

Elyth が `saiverse-elyth-addon`、Voice TTS が `saiverse-voice-tts` という命名なので、外部公式アドオンの命名規約 `saiverse-<service>-addon` に揃える。将来的にこのアドオンを別リポジトリに切り出して `git clone` 提供する場合も、命名が一貫する。

## スコープ

### Phase 1 — 拡張点の整備

1. `addon.json` スキーマに `oauth_flows` セクション追加（バリデーション含む）。Phase 1 では `provider: "oauth2_pkce"` のみ対応
2. `saiverse/oauth/handler.py` 新規実装（認可URL生成・コールバック・Pull型リフレッシュ・切断・ステータス、state は in-memory）
3. `saiverse/addon_paths.py` 新規実装（`get_addon_storage_path()`）
4. `addon_loader.py` に `load_addon_integrations()` 追加 + ライフサイクル連動
5. `IntegrationManager.unregister()` 追加（`_is_integration_enabled` 撤去は Phase 2 と同時実施 — Phase 1 で先に撤去すると、まだハードコード登録されている XMentionIntegration が `SAIVERSE_X_POLLING_ENABLED=false` を明示設定済みのユーザーでも勝手にポーリング開始してしまうため）
6. フロントエンド `OAuthFlowSection.tsx` 汎用コンポーネント実装、AddonManagerModal に統合
7. テスト: 単体テスト（OAuth state管理、Pull型トークンリフレッシュ、integrations自動discovery、addon storage path）

### Phase 2 — X 連携の切り出し

8. `expansion_data/saiverse-x-addon/` ディレクトリ作成、ファイル移動
9. `x_lib/credentials.py` を AddonPersonaConfig 経由に書き換え
10. `XMentionIntegration` を `integrations/mentions.py` へ移動
11. OAuth設定を `addon.json` の `oauth_flows` で宣言、`x_handlers.py` に `fetch_user_info` 実装
12. `storage/reply_log.py` 実装、`x_reply_tweet.py` の DB アクセスをアドオン側 SQLite に切り替え
13. コア側の撤去（api/routes、saiverse/integrations、frontend、saiverse_manager、integration_manager の `_is_integration_enabled`、state.x_polling_enabled、builtin_data、`XReplyLog` モデル）
14. DBマイグレーション（X関連カラム + `x_reply_log` テーブル DROP）
15. 実機検証: まはーが Air で再認可、メンションポーリング、投稿、リプライ（重複防止が効くこと含む）、タイムライン読み込みを通す

### Phase 3 — ドキュメント整備

16. `docs/intent/x_integration.md` を更新（実装パスをアドオン側に変更）
17. `docs/features/addon-extensions.md` 新規（OAuth flow / Integration / Addon Storage の書き方をアドオン作者向けに解説）
18. README/CHANGELOG に「X連携の再設定が必要」を明記

### 将来 Phase（範囲外、メモのみ）

- OAuth state の DB 保存化（再起動耐性）
- `provider` の追加対応: Bluesky AT Protocol、OAuth 1.0a (Discord等)
- Push 型トークンリフレッシュ（バックグラウンド自動更新）
- アドオンアンインストール時の `~/.saiverse/addons/<addon>/` 自動クリーンアップオプション
- Mastodon / Bluesky アドオンの実装（X切り出しの手順書として `docs/features/addon-extensions.md` を参照する形）

## 検証観点

実機検証で必ず通すケース:
- 新規ペルソナで X 認可を最初から実施 → トークン保存 → 投稿成功
- 既存ペルソナで再認可 → 旧credentials.jsonが無視され新トークンが使われる
- アドオン無効化 → メンションポーリングが止まる
- アドオン再有効化 → ポーリング再開、認可情報は失われない
- トークン期限切れ → リフレッシュ → API呼び出し成功
- アドオン同梱の `mcp_servers.json` で `${persona.addon.saiverse-x-addon.x_access_token}` 参照が解決できる（将来MCP経由でX操作したい場合の伏線）

## 補足: 設計上の前提

- **X Developer App は全ペルソナで共有**: `client_id` / `client_secret` はグローバル `params_schema` で1つだけ持ち、全ペルソナの認可で共有する。X Developer App はもともと複数アカウント（複数AITuber）からの認可を受け付ける設計のため問題なし。そもそもエンドユーザーが自分でDeveloper Appを作って運用すること自体が大半のユーザーにとってはイレギュラーな運用形態であり、共有制限を設ける必要性は薄い。
