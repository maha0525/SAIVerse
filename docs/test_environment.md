# テスト環境

本番データ（`~/.saiverse`、`user_data/`）を使わずにバックエンドをテストするための隔離されたテスト環境です。

## 概要

テスト環境のデータ（DB・記憶・ログ）は以下の構成で本番環境から分離されています。
ただしリポジトリの `.env` と `expansion_data/` のアドオン本体は本番と同じものを読みます
（詳しくは後述の「外部連携の扱い」）：

```
test_fixtures/                    # git管理（テスト定義）
├── definitions/
│   └── test_data.json           # テストデータ定義
├── setup_test_env.py            # セットアップスクリプト
├── start_test_server.sh         # サーバー起動スクリプト
├── start_test_server.bat        # サーバー起動スクリプト（Windows 版）
├── start_test_frontend.bat      # テスト用バックエンドにつなぐフロントエンド起動（Windows）
└── test_api.py                  # APIテストスクリプト

test_data/                        # gitignore（生成されるデータ）
├── .saiverse/                   # ~/.saiverseの代替
│   ├── personas/                # ペルソナごとのメモリ
│   ├── qdrant/                  # ベクトルDB
│   └── ...
└── user_data/                   # user_data/の代替
    └── database/
        └── saiverse.db          # テスト用データベース
```

## クイックスタート

```bash
# 1. テスト環境のセットアップ
python test_fixtures/setup_test_env.py

# 2. テストサーバー起動（ポート18000）
./test_fixtures/start_test_server.sh

# 3. 別ターミナルでAPIテスト実行
python test_fixtures/test_api.py         # フルテスト
python test_fixtures/test_api.py --quick # クイックテスト（LLM除く）
```

## 状態の検分 (inspect_world.py)

本番・テスト環境どちらの状態も、読み取り専用の検分 CLI で確認できます
（sqlite を直接叩く必要はありません。設計は `docs/intent/agent_inspection_cli.md`）：

```bash
python scripts/inspect_world.py personas --env test          # ペルソナ一覧
python scripts/inspect_world.py memory <persona> --env test  # 記憶 (タグ/期間/grep フィルタ)
python scripts/inspect_world.py tracks <persona> --env test  # Track 状態
python scripts/inspect_world.py day-plan <persona> --env test # 時間割
python scripts/inspect_world.py llm-io --env test            # LLM I/O ログ
python scripts/inspect_world.py errors                       # WARNING 以上のダイジェスト
# --env test を省略すると本番 (~/.saiverse)。すべて読み取り専用
```

## 会話テスト (run_conversation.py)

台本 (複数ターンのユーザー発話) をテスト環境のペルソナへ実チャット経路で流し、
transcript を得ます（実 LLM・実コスト。設計は `docs/intent/conversation_runner.md`）：

```bash
python scripts/run_conversation.py --persona <id> --message "おはよう" --message "昨日何してた？"
python scripts/run_conversation.py --script <台本.json>
# 環境変数未設定なら自動で test_data/ を指す。本番 DB を指すと起動拒否
```

Discord ゲートウェイの設定は、起動スクリプトと同じ無効値で上書きしてから `SAIVerseManager` を作ります（一日シム `scripts/run_day_sim.py --real` も同じです。理由は後述の「外部連携の扱い」）。

## コマンド詳細

### setup_test_env.py

テスト環境のセットアップと管理を行います。

```bash
# フルセットアップ（初回実行時）
python test_fixtures/setup_test_env.py

# クリーンセットアップ（全削除して再作成）
python test_fixtures/setup_test_env.py --clean

# データベースのみリセット
python test_fixtures/setup_test_env.py --reset-db

# SAIMemoryデータのみリセット
python test_fixtures/setup_test_env.py --reset-memory
```

### start_test_server.sh / start_test_server.bat

環境変数を設定してテストサーバーを起動します。`.bat` は Windows 版で、`--setup` / `--clean` の引数はありません（テスト DB が無いときだけ自動でセットアップします）。

```bash
# 通常起動
./test_fixtures/start_test_server.sh

# セットアップ込み
./test_fixtures/start_test_server.sh --setup

# クリーンセットアップ込み
./test_fixtures/start_test_server.sh --clean
```

```bat
rem Windows
test_fixtures\start_test_server.bat
```

内部で以下の環境変数が設定されます：
- `SAIVERSE_HOME=test_data/.saiverse`
- `SAIVERSE_USER_DATA_DIR=test_data/user_data`
- Discord ゲートウェイの 4 つの変数（次の「外部連携の扱い」）

### 外部連携の扱い

#### Discord ゲートウェイは起動スクリプトが必ず無効にする

`main.py` などは起動時に `load_dotenv()` でリポジトリの `.env` を読みます。`load_dotenv()` は**まだ設定されていない**環境変数だけを `.env` の値で埋めるので、起動スクリプトが何もしなければ、本番用に `.env` へ書いたゲートウェイの設定（接続先・トークン・チャンネル対応表）がテストサーバーにそのまま入ります。`.env` でゲートウェイを有効にしている時期にテストサーバーを起動すると、テストの世界が本番のトークンで本番のゲートウェイにつながります。

そのため、両方の起動スクリプトは次の値で上書きします。

| 変数 | 起動スクリプトが入れる値 |
|---|---|
| `SAIVERSE_GATEWAY_ENABLED` | `0` |
| `SAIVERSE_GATEWAY_WS_URL` | `ws://127.0.0.1:9/test-disabled` |
| `SAIVERSE_GATEWAY_TOKEN` | `test-disabled` |
| `SAIVERSE_GATEWAY_CHANNEL_MAP` | `[]` |

**値を空にしてはいけません。** Windows の cmd では `set "VAR="` が「空にする」ではなく「変数を消す」になり、消えた変数は `load_dotenv()` が `.env` の本番値で埋め直します（`discord_gateway/config.py` も `.env` を自分で読みます）。

#### 起動スクリプトが止めていないもの

- **LLM の API キー**（`.env`）: そのまま使われます。テストのペルソナの発話や自律行動は実際の課金になります。
- **メール送信**（`.env` の `SMTP_*`）: スペル `send_email_to_user` は実行時に `.env` の SMTP 設定を読むので、SMTP 設定が入っていれば、テストのペルソナが使ったときに実際にメールが送られます。
- **Unity Gateway**: `UNITY_GATEWAY_ENABLED` の既定が有効で、テストサーバーも `0.0.0.0:8765`（本番と同じ既定ポート）で待ち受けます。
- **アドオン**（`expansion_data/`）: 本番と同じフォルダを読み、テスト DB にアドオン設定の行が無いものは有効として扱われます。SwitchBot・X・Elyth・stackchan の資格情報はテスト DB のアドオン設定と `test_data/user_data/addon_data/` 側にあるので、テスト環境で設定しない限り本番のアカウントや機体にはつながりません。本番の世界を丸ごと複製した場合は、`--keep-addons` を付けない限り複製スクリプトがアドオンを無効にします（`docs/intent/sandbox_world_clone.md` §3）。
- **SDS**（`SDS_URL`）: テスト都市の定義はオフライン起動（`START_IN_ONLINE_MODE: false`）なので、起動時には登録しません。

### test_api.py

APIエンドポイントのテストを実行します。

```bash
# フルテスト（LLM呼び出し含む）
python test_fixtures/test_api.py

# クイックテスト（LLM呼び出しなし、高速）
python test_fixtures/test_api.py --quick

# チャットテストのみ
python test_fixtures/test_api.py --chat

# カスタムURL
python test_fixtures/test_api.py --base-url http://127.0.0.1:18000
```

## テストデータ定義

`test_fixtures/definitions/test_data.json`でテストデータを定義します：

```json
{
  "user": {
    "USERID": 1,
    "USERNAME": "test_user",
    "PASSWORD": "test_password",
    "LOGGED_IN": true,
    "CURRENT_CITYID": 1,
    "CURRENT_BUILDINGID": "test_lobby"
  },
  "city": {
    "CITYID": 1,
    "CITY_SLUG": "test_city",
    "UI_PORT": 18000,
    "API_PORT": 18001,
    ...
  },
  "buildings": [...],
  "personas": [
    {
      "AIID": "test_persona_a",
      "AINAME": "Test Persona A",
      "DEFAULT_MODEL": "gemini-2.5-flash-preview-09-2025",
      "LIGHTWEIGHT_MODEL": "gemini-3.1-flash-lite-preview",
      "start_building": "test_lobby",
      ...
    }
  ],
  "playbooks": [
    "basic_chat",
    "meta_user",
    "meta_auto",
    "sub_router_user",
    "sub_speak_meta",
    ...
  ]
}
```

### 重要なフィールド

| フィールド | 説明 |
|-----------|------|
| `user.CURRENT_BUILDINGID` | ユーザーの初期位置。チャットテストに必須 |
| `user.LOGGED_IN` | ログイン状態。`true`推奨 |
| `personas[].LIGHTWEIGHT_MODEL` | routerノード用の軽量モデル。未設定だと環境変数のデフォルトが使われる |
| `personas[].start_building` | ペルソナの初期配置ビルディング |
| `playbooks` | インポートするプレイブック名のリスト |

## テスト内容

`test_api.py`は以下をテストします：

### データ検証テスト
- **City**: test_data.jsonの都市が正しくDBに存在するか
- **Buildings**: 定義したビルディングが存在するか
- **Personas**: 定義したペルソナが存在するか（モデル設定含む）
- **Playbooks**: 必要なプレイブックがインポートされているか

### APIエンドポイントテスト
- **Models Config**: `/api/config/models` - モデル一覧取得
- **User Status**: `/api/user/status` - ユーザー状態取得
- **User Buildings**: `/api/user/buildings` - ビルディング一覧取得
- **Chat (LLM)**: `/api/chat/send` - チャット送信（LLM呼び出し）

## トラブルシューティング

### "User is not in any building" エラー

`test_data.json`で`user.CURRENT_BUILDINGID`が設定されていることを確認してください。

### Ollamaモデルが見つからないエラー

ペルソナに`LIGHTWEIGHT_MODEL`が設定されていない場合、環境変数のデフォルト（Ollamaモデル）が使われます。`test_data.json`で`LIGHTWEIGHT_MODEL`を明示的に設定してください。

### プレイブックが見つからないエラー

`test_data.json`の`playbooks`配列に必要なプレイブック名を追加し、`--reset-db`を実行してください。

```bash
python test_fixtures/setup_test_env.py --reset-db
```

## AIエージェント向け情報

Claude Codeなどのエージェントがテストを実行する場合：

1. **セットアップ**: `python test_fixtures/setup_test_env.py`
2. **サーバー起動**: バックグラウンドで`./test_fixtures/start_test_server.sh`を実行
3. **起動待機**: 10秒程度待つ
4. **テスト実行**: `python test_fixtures/test_api.py --quick`（高速）または`python test_fixtures/test_api.py`（フル）
5. **サーバー停止**: `pkill -f "python main.py test_city"`

ストリーミングレスポンス（NDJSON形式）を返すAPIがあるため、`test_api.py`では`streaming=True`パラメータで適切に処理しています。
