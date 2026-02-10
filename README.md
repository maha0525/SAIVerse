# SAIVerse

複数のAIペルソナが自律的に活動する仮想世界シミュレーター。建物（Building）と都市（City）で構成される世界にAIを配置し、対話・自律行動・都市間移動を観察できるフルスタック環境です。

## 主な機能

- **マルチエージェント環境** - 複数のAIペルソナが同時に活動し、互いに会話・協力
- **自律行動モード** - AIがパルス駆動で能動的に思考・発言
- **ワールドダイブ** - ユーザー自身がアバターとして世界に参加
- **Playbook/SEA** - AIの行動パターンをJSON形式のフローで定義
- **Memopedia** - 会話から知識を抽出し構造化するナレッジベース
- **都市間連携** - 複数のSAIVerseインスタンスを接続、AIが都市間を移動
- **Discord連携** - DiscordチャンネルとSAIVerse建物を接続

## クイックスタート

### 前提条件

- [Python 3.11以上](https://www.python.org/downloads/)
- [Node.js 18以上](https://nodejs.org/)
- [Git](https://git-scm.com/)
- LLM APIキー（Gemini推奨。初回起動時のチュートリアルで設定可能）

### Windows

```
git clone https://github.com/maha0525/SAIVerse.git
```

1. `SAIVerse` フォルダ内の **`setup.bat`** をダブルクリック
   - Python仮想環境の作成、依存パッケージのインストール、DB初期化、埋め込みモデルのダウンロードを自動実行
2. **`start.bat`** をダブルクリック
3. ブラウザで http://localhost:3000 が自動的に開きます

### macOS / Linux

```bash
git clone https://github.com/maha0525/SAIVerse.git
cd SAIVerse
chmod +x setup.sh start.sh
./setup.sh
./start.sh
```

初回起動時にチュートリアルが表示され、ユーザー名やAPIキーの設定を案内します。

### 手動セットアップ

セットアップスクリプトを使わず手動で構築する場合は [インストールガイド](./docs/getting-started/installation.md) を参照してください。

## 技術スタック

| レイヤー | 技術 |
|---------|------|
| フロントエンド | Next.js + TypeScript |
| バックエンド | Python + FastAPI |
| LLM | OpenAI / Anthropic / Google Gemini / Ollama |
| データベース | SQLite |
| 記憶システム | SAIMemory (SQLite + Embedding) |

## プロジェクト構造

```
SAIVerse/
├── main.py                  # エントリーポイント
├── setup.bat / setup.sh     # セットアップスクリプト
├── start.bat / start.sh     # 起動スクリプト
│
├── saiverse/                # コアパッケージ
│   ├── saiverse_manager.py  #   中央オーケストレーター
│   ├── model_configs.py     #   LLMモデル設定管理
│   ├── data_paths.py        #   データパス管理
│   └── ...                  #   その他コアモジュール群
│
├── api/                     # FastAPI バックエンド
├── frontend/                # Next.js フロントエンド
├── persona/                 # ペルソナの実装
├── sea/                     # SEA Playbook実行エンジン
├── manager/                 # マネージャーMixin群
├── sai_memory/              # SAIMemory 記憶システム
├── saiverse_memory/         # SAIMemory アダプター
├── tools/                   # AIツールレジストリ
├── database/                # DBモデル・マイグレーション
├── llm_clients/             # LLMプロバイダクライアント
├── builtin_data/            # 組み込みデフォルトデータ
├── docs/                    # ドキュメント
├── scripts/                 # ユーティリティスクリプト
└── tests/                   # テストスイート
```

## ユーザーデータ

ユーザーデータはリポジトリ外の `~/.saiverse/` に保存されます。

```
~/.saiverse/
├── user_data/               # カスタム設定・データベース
│   ├── database/            #   SQLiteデータベース
│   ├── tools/               #   カスタムツール
│   ├── playbooks/           #   カスタムPlaybook
│   ├── models/              #   カスタムモデル設定
│   └── logs/                #   セッションログ
├── personas/                # ペルソナ別の記憶DB
├── cities/                  # 都市・建物のログ
└── image/                   # アップロード画像
```

`builtin_data/` のデフォルト設定より `user_data/` のカスタム設定が優先されます。

## テスト環境

本番データを使わずにバックエンドをテストするための隔離されたテスト環境が用意されています。

```bash
python test_fixtures/setup_test_env.py     # テスト環境セットアップ
./test_fixtures/start_test_server.sh       # テストサーバー起動（ポート18000）
python test_fixtures/test_api.py --quick   # クイックテスト（LLM除く）
```

詳細は [docs/test_environment.md](./docs/test_environment.md) を参照してください。

## ドキュメント

詳細なドキュメントは [docs/](./docs/) を参照してください：

- [インストール](./docs/getting-started/installation.md) - 環境構築の詳細手順
- [クイックスタート](./docs/getting-started/quickstart.md) - 起動と初期操作
- [設定](./docs/getting-started/configuration.md) - 環境変数・モデル設定
- [GPU セットアップ](./docs/getting-started/gpu-setup.md) - Embedding高速化
- [基本概念](./docs/concepts/) - アーキテクチャ・City/Building・ペルソナ
- [ユーザーガイド](./docs/user-guide/) - UIの使い方
- [機能詳細](./docs/features/) - 各機能の詳細解説
- [開発者ガイド](./docs/developer-guide/) - コントリビューション・拡張方法
- [リファレンス](./docs/reference/) - DB・API・ツール・スクリプト一覧

## コントリビューション

プルリクエスト歓迎！詳細は [開発者ガイド](./docs/developer-guide/contributing.md) を参照してください。

## ライセンス

未定
