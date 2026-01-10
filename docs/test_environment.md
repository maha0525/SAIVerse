# テスト環境

本番データ（`~/.saiverse`、`user_data/`）を使わずにバックエンドをテストするための隔離されたテスト環境です。

## 概要

テスト環境は以下の構成で本番環境から完全に分離されています：

```
test_fixtures/                    # git管理（テスト定義）
├── definitions/
│   └── test_data.json           # テストデータ定義
├── setup_test_env.py            # セットアップスクリプト
├── start_test_server.sh         # サーバー起動スクリプト
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

### start_test_server.sh

環境変数を設定してテストサーバーを起動します。

```bash
# 通常起動
./test_fixtures/start_test_server.sh

# セットアップ込み
./test_fixtures/start_test_server.sh --setup

# クリーンセットアップ込み
./test_fixtures/start_test_server.sh --clean
```

内部で以下の環境変数が設定されます：
- `SAIVERSE_HOME=test_data/.saiverse`
- `SAIVERSE_USER_DATA_DIR=test_data/user_data`

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
    "CITYNAME": "test_city",
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
      "LIGHTWEIGHT_MODEL": "gemini-2.5-flash-lite-preview-09-2025",
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
