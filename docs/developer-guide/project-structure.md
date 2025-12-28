# プロジェクト構造

SAIVerseのディレクトリ構成を説明します。

## ルート構造

```
SAIVerse/
├── main.py                 # エントリーポイント
├── saiverse_manager.py     # 世界の管理者（メインロジック）
├── conversation_manager.py # 自律会話管理
├── occupancy_manager.py    # 移動・占有管理
├── action_handler.py       # アクション解析・実行
├── llm_router.py           # ツール呼び出し判定
├── buildings.py            # Buildingモデル
│
├── frontend/               # Next.js フロントエンド
├── api/                    # FastAPI ルート定義
├── database/               # データベース関連
├── persona/                # ペルソナ実装
├── sea/                    # Playbook実行エンジン
├── manager/                # 各種マネージャーMixin
├── sai_memory/             # 記憶システム
├── tools/                  # AIツール
├── llm_clients/            # LLMクライアント
├── scripts/                # 保守スクリプト
├── tests/                  # テスト
├── docs/                   # ドキュメント
├── docs_legacy/            # 旧ドキュメント
│
├── ui/                     # [レガシー] Gradio UI
├── saiverse_memory/        # [レガシー] 記憶アダプター
│
├── models.json             # モデル定義
├── cities.json             # City設定
└── requirements.txt        # Python依存
```

## 主要ディレクトリ

### frontend/

Next.js + TypeScript のフロントエンド。

```
frontend/
├── src/
│   ├── app/          # Next.js App Router
│   ├── components/   # Reactコンポーネント
│   ├── hooks/        # カスタムフック
│   └── lib/          # ユーティリティ
├── package.json
└── tsconfig.json
```

### api/

FastAPI のエンドポイント定義。

```
api/
├── chat.py           # チャット関連
├── buildings.py      # Building操作
├── personas.py       # ペルソナ操作
├── memory.py         # メモリ操作
└── ...
```

### persona/

ペルソナの実装。

```
persona/
├── core.py           # PersonaCore メインクラス
├── bootstrap.py      # 初期化
├── mixins/           # 機能別ミックスイン
│   ├── memory.py     # 記憶関連
│   ├── movement.py   # 移動関連
│   └── ...
└── tasks/            # タスク管理
```

### sea/

Playbook実行エンジン。

```
sea/
├── runtime.py        # 実行エンジン本体
├── playbook_models.py# データモデル
├── langgraph_runner.py # LangGraph統合
├── pulse_controller.py # パルス制御
└── playbooks/        # Playbook定義ファイル
    ├── meta_user.json
    ├── meta_auto.json
    └── ...
```

### manager/

SAIVerseManagerのMixinクラス群。

```
manager/
├── admin.py          # 管理機能
├── blueprints.py     # ブループリント
├── gateway.py        # Gateway連携
├── history.py        # 履歴管理
├── persona.py        # ペルソナ管理
├── runtime.py        # ランタイム
├── sds.py            # SDS連携
├── state.py          # 状態管理
└── visitors.py       # 訪問者管理
```

### sai_memory/

記憶システム。

```
sai_memory/
├── storage.py        # ストレージ本体
├── memopedia/        # Memopedia
│   ├── core.py       # コアAPI
│   └── storage.py    # DBアクセス
└── ...
```

### tools/

AIが使用するツール。

```
tools/
├── __init__.py       # レジストリ
├── context.py        # コンテキスト管理
├── defs/             # ツール定義
│   ├── calculator.py
│   ├── image_generator.py
│   ├── item_pickup.py
│   └── ...
└── utilities/        # ユーティリティ
```

### database/

データベース関連。

```
database/
├── models.py         # SQLAlchemyモデル
├── api_server.py     # FastAPIサーバー
├── seed.py           # 初期データ
└── data/             # DBファイル格納
```

## 次のステップ

- [ツールの追加](./adding-tools.md) - 新しいツールの実装
- [Playbook作成](./creating-playbooks.md) - 独自Playbookの作成
