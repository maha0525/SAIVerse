# 🧩 SAIVerse

複数のAIペルソナが自律的に活動する仮想世界シミュレーター。建物（Building）と都市（City）で構成される世界にAIを配置し、対話・自律行動・都市間移動を観察できるフルスタック環境です。

## ✨ 主な機能

- **マルチエージェント環境** - 複数のAIペルソナが同時に活動し、互いに会話・協力
- **自律行動モード** - AIがパルス駆動で能動的に思考・発言
- **ワールドダイブ** - ユーザー自身がアバターとして世界に参加
- **Playbook/SEA** - AIの行動パターンをJSON形式のフローで定義
- **Memopedia** - 会話から知識を抽出し構造化するナレッジベース
- **都市間連携** - 複数のSAIVerseインスタンスを接続、AIが都市間を移動
- **Discord連携** - DiscordチャンネルとSAIVerse建物を接続

## 🚀 クイックスタート

```bash
# 1. クローン
git clone https://github.com/maha/SAIVerse.git
cd SAIVerse

# 2. 仮想環境作成・依存インストール
python -m venv .venv
.venv\Scripts\activate  # Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

# 3. .envファイル作成
cp .env.example .env
# .envを編集してAPIキーを設定

# 4. データベース初期化
python database/seed.py

# 5. 起動
python main.py city_a
```

起動後、ブラウザで http://localhost:3000 を開いてフロントエンドにアクセス。

## 🧪 テスト環境

本番データを使わずにバックエンドをテストするための隔離されたテスト環境が用意されています。

```bash
# テスト環境のセットアップ
python test_fixtures/setup_test_env.py

# テストサーバー起動（ポート18000）
./test_fixtures/start_test_server.sh

# APIテスト実行
python test_fixtures/test_api.py         # フルテスト（LLM呼び出し含む）
python test_fixtures/test_api.py --quick # クイックテスト（LLM除く）
```

詳細は [docs/test_environment.md](./docs/test_environment.md) を参照してください。

## 📚 ドキュメント

詳細なドキュメントは [docs/](./docs/) を参照してください：

- [はじめに](./docs/getting-started/) - インストール・設定・クイックスタート
- [基本概念](./docs/concepts/) - アーキテクチャ・City/Building・ペルソナ
- [ユーザーガイド](./docs/user-guide/) - UIの使い方
- [機能詳細](./docs/features/) - 各機能の詳細解説
- [開発者ガイド](./docs/developer-guide/) - コントリビューション・拡張方法
- [リファレンス](./docs/reference/) - DB・API・ツール・スクリプト一覧

## 🛠️ 技術スタック

| レイヤー | 技術 |
|---------|------|
| フロントエンド | Next.js + TypeScript |
| バックエンド | Python + FastAPI |
| LLM | OpenAI / Anthropic / Google Gemini / Ollama |
| データベース | SQLite |
| 記憶システム | SAIMemory (SQLite + SBERT埋め込み) |

## 📁 プロジェクト構造

```
SAIVerse/
├── main.py              # エントリーポイント
├── frontend/            # Next.js フロントエンド
├── api/                 # FastAPI バックエンド
├── persona/             # ペルソナの実装
├── sea/                 # Playbook実行エンジン
├── manager/             # 各種マネージャーMixin
├── sai_memory/          # 記憶システム
├── tools/               # AIが使用するツール群
├── database/            # DBモデル・シード
├── llm_clients/         # LLMクライアント
├── builtin_data/        # 組み込みデフォルトデータ
├── user_data/           # ユーザーカスタムデータ（gitignore）
└── docs/                # ドキュメント
```

## 🤝 コントリビューション

プルリクエスト歓迎！詳細は [開発者ガイド](./docs/developer-guide/contributing.md) を参照してください。

## 📄 ライセンス

未定
