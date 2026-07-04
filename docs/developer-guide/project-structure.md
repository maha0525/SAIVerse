# プロジェクト構造

SAIVerse のディレクトリ構成を説明する。全体の概念関係は [`overview/landscape.md`](../overview/landscape.md)、各概念の実装入口は [`concepts/`](../concepts/README.md) を参照。

## ルート構造

```
SAIVerse/
├── main.py                 # エントリーポイント（City インスタンス起動）
├── sds_server.py           # SDS（ディレクトリサービス）エントリーポイント
├── setup.bat / setup.sh    # セットアップスクリプト
├── start.bat / start.sh    # 起動スクリプト
├── update.bat / update.sh  # アップデートスクリプト
├── requirements.txt        # Python 依存
├── CLAUDE.md / AGENTS.md   # AI エージェント向け作業指示
│
├── saiverse/               # コアパッケージ（マネージャ・設定・ユーティリティ）
├── api/                    # FastAPI ルート定義
├── frontend/               # Next.js フロントエンド
├── persona/                # ペルソナ実装（PersonaCore）
├── sea/                    # SEA ランタイム（Playbook 実行エンジン）
├── manager/                # SAIVerseManager の Mixin 群
├── sai_memory/             # SAIMemory 記憶システム本体
├── saiverse_memory/        # SAIMemory アダプター
├── tools/                  # ツールレジストリ（ロード機構）
├── phenomena/              # Phenomena システム（外部イベント入口）
├── database/               # DB モデル・マイグレーション
├── llm_clients/            # LLM プロバイダクライアント
├── discord_gateway/        # Discord ゲートウェイ（任意）
├── unity_gateway/          # Unity ゲートウェイ（3D/VR 連携）
├── unity_client/           # Unity クライアント資材
│
├── builtin_data/           # 組み込みデフォルト（git 追跡・最低優先）
├── expansion_data/         # ユーザー導入の拡張パック（gitignore・中間優先）
│
├── docs/                   # ドキュメント
├── scripts/                # 保守スクリプト
├── tests/                  # テストスイート
├── test_fixtures/          # 隔離テスト環境
├── sbert/                  # 埋め込みモデル（存在すれば利用、なければ自動DL）
└── assets/                 # ロゴ・ガイド画像
```

> **重要**: ユーザーデータ（DB・カスタム設定・ペルソナ記憶）はリポジトリ外の `~/.saiverse/` に保存される（後述）。リポジトリ直下に `user_data/` があっても、起動時に `~/.saiverse/user_data/` へ移行される名残。

## リソースの3層優先順位

ツール・Playbook・Phenomena・モデル・プロバイダ等の拡張リソースは以下で解決される:

```
~/.saiverse/user_data/  >  expansion_data/<addon>/  >  builtin_data/
（最優先）                   （中間）                    （最低）
```

## 主要ディレクトリ

### saiverse/

世界の中核ロジック。かつてルート直下にあったマネージャ群はすべてこのパッケージに収まっている。

```
saiverse/
├── saiverse_manager.py     # 中央オーケストレーター（SAIVerseManager）
├── occupancy_manager.py    # 移動・占有管理（OccupancyManager）
├── conversation_manager.py # 自律会話駆動（旧プロトタイプ・実質 no-op）
├── action_handler.py       # ::act ブロック解析・実行
├── autonomy_manager.py     # 自律バイオリズムの大リズム（50分 tick）
├── pulse_scheduler.py      # SubLineScheduler（30秒ポーリングで自律 Track を回す）
├── event_scheduler.py      # スケジュール実行
├── clock.py                # 仮想クロック（時刻の一元供給源、一日シミュレータ用）
├── day_simulator.py        # DES ドライバ（仮想時刻でイベントキューを早回し）
├── day_plan.py             # 時間割の保存とコマ発火配線（自律行動 v2 §4.2）
├── desire_engine.py        # 欲求の帳簿（六型・鮮度・再訪・淘汰の決定論処理、自律行動 v2 §5）
├── judgment_points.py      # 判断点コーディネータ（起床/セッション終了の動的スキーマ + 起動、judgment_points.md）
├── internal_alert_poller.py# 内部 alert ポーリング
├── llm_router.py           # ツール呼び出し判定
├── model_configs.py        # モデル設定管理
├── model_defaults.py       # 組み込みデフォルトモデル
├── meta_layer.py           # メタ判断まわり
├── buildings.py            # Building モデルヘルパ
├── data_paths.py           # パス管理（user_data/builtin_data）
├── addon_*.py              # アドオン機構（loader/installer/registry 等）
├── life_purpose.py         # 自律の源泉（欲求・目的）
├── note_manager.py         # メモ機構
├── observer_manager.py     # Observer（定期観測 Fixture）
└── ...                     # その他コアモジュール
```

### api/

FastAPI のエンドポイント。ルートは `routes/` サブパッケージに分割されている。

```
api/
├── main.py           # FastAPI アプリ生成
├── deps.py           # 依存性注入
├── routes/           # エンドポイント群
│   ├── chat.py       #   チャット（NDJSON ストリーミング）
│   ├── config.py     #   設定
│   ├── mcp.py        #   MCP
│   ├── addon*.py     #   アドオン（catalog / actions / events）
│   ├── admin.py      #   管理機能
│   └── ...
└── utils/
```

### persona/

ペルソナの実装。

```
persona/
├── core.py           # PersonaCore メインクラス
├── bootstrap.py      # 初期化
├── constants.py      # 定数
├── emotion_module.py # 感情モジュール（実質未活用）
├── history.py / history_manager.py # 履歴
├── mixins/           # 機能別ミックスイン（emotion / generation / history / movement）
└── tasks/            # タスク管理（storage.py / store.py。現状ほぼ未運用）
```

### sea/

SEA ランタイム（Playbook 実行エンジン、LangGraph ベース）。**Playbook の JSON ファイル自体は `builtin_data/playbooks/` にあり、この配下にはない。**

```
sea/
├── runtime.py            # 実行エンジン本体（SEARuntime）
├── runtime_llm.py        # LLM ノード実行 / Spell loop（_run_spell_loop）
├── runtime_*.py          # engine / graph / nodes / runner / state / context / emitters
├── langgraph_runner.py   # LangGraph 統合
├── playbook_models.py    # ノード定義スキーマ（LLMNodeDef / ToolNodeDef 等）
├── pulse_controller.py   # PulseController（優先度制御・割り込み）
├── pulse_context.py      # PulseContext（Aspect / line 階層）
├── pulse_root_context.py # ルートコンテキスト
├── mode_spell_permissions.py # モード別 Spell 許可
├── work_session.py       # 予算付き作業セッションランナー（自律行動 v2 §4.3）
├── cancellation.py       # キャンセル
└── head_pipeline/        # head（キャッシュの効く安定領域）の構築
    ├── pipeline.py / registry.py / store.py / types.py / integration.py
    └── sections/         #   各 Section（common_prompt / persona_self / building 等）
```

### manager/

SAIVerseManager の Mixin クラス群。

```
manager/
├── admin.py          # 管理機能
├── background.py     # バックグラウンド処理
├── blueprints.py     # ブループリント
├── gateway.py        # Gateway 連携
├── history.py        # 履歴管理
├── initialization.py # 初期化
├── items.py          # アイテム
├── persona.py / persona_events.py # ペルソナ管理・イベント
├── runtime.py        # ランタイム
├── sds.py            # SDS 連携
├── state.py / user_state.py # 状態管理
└── visitors.py       # 訪問者管理（inter-city）
```

### sai_memory/

SAIMemory 記憶システム本体（per-persona の `memory.db`）。

```
sai_memory/
├── memory/           # 生ログ（Thread/Message）・recall・entity_extractor
├── arasuji/          # Chronicle（あらすじ）生成（generator/storage/context）
├── memopedia/        # Memopedia（知識グラフ。core/storage/generator）
├── unified_recall.py # 統合想起
├── backup.py         # rdiff-backup
├── config.py / cli.py / logging_utils.py
└── README.md
```

### tools/

ツールの**ロード機構**。組み込みツールの定義は `builtin_data/tools/` にある。

```
tools/
├── __init__.py       # レジストリ（TOOL_REGISTRY。3層から読み込み）
├── core.py           # コア
├── context.py        # contextvars でペルソナ/マネージャ参照を注入
├── confirmation.py   # 発動確認
├── fuzzy.py          # Spell の fuzzy パース
├── mcp_client.py / mcp_config.py # MCP クライアント・設定
├── adapters/         # アダプタ
└── utilities/        # ユーティリティ
```

**ツールの優先順位**: `~/.saiverse/user_data/tools/` > `expansion_data/<addon>/tools/` > `builtin_data/tools/`

### database/

データベース関連。

```
database/
├── models.py         # SQLAlchemy モデル
├── api_server.py     # DB API サーバー
├── db_manager.py     # DB マネージャ
├── migrate.py        # マイグレーション（自動バックアップ付き）
├── seed.py           # 初期データ（⚠️ 全データ削除）
├── backup.py         # 起動時バックアップ
├── building_messages.py # Building メッセージ
├── schema_sync.py    # スキーマ同期
├── paths.py          # DB パス管理
└── session.py        # セッション
```

**現在の DB 格納場所**: `~/.saiverse/user_data/database/saiverse.db`

### builtin_data/

組み込みデフォルト（git 追跡・最低優先）。

```
builtin_data/
├── tools/            # 組み込みツール定義（*.py 直下）
├── playbooks/        # 組み込み Playbook
│   └── public/       #   router_callable な Playbook 群（meta_judgment*.json 等）
├── models/           # 組み込みモデル設定（1モデル1JSON）
├── providers/        # 組み込みプロバイダ設定（接続先定義）
├── phenomena/        # 組み込み Phenomena
├── prompts/          # 組み込みプロンプト
├── icons/            # 組み込みアイコン
├── cities.json       # City 初期設定
├── seed_data.json    # シード用データ
└── action_priority.json
```

## ユーザーデータ（`~/.saiverse/`）

ユーザーデータはリポジトリ外に保存される（`SAIVERSE_HOME` env で変更可）。

```
~/.saiverse/
├── user_data/              # ユーザーカスタム（最優先）
│   ├── database/           #   saiverse.db（本番 DB）
│   ├── tools/ playbooks/ models/ providers/ phenomena/ prompts/ icons/
│   ├── addon_data/<id>/    #   アドオン永続データ
│   └── logs/               #   セッションログ
├── personas/<id>/          # ペルソナ別記憶（memory.db / tasks.db）
├── cities/<city>/          # 都市・建物のログ
├── image/ documents/       # アップロード画像・文書
└── backups/                # DB・記憶バックアップ
```

**移行**: 起動時に `main.py` が旧 `user_data/`（リポジトリ内）を `~/.saiverse/user_data/` へ自動移行する。テスト時は `SAIVERSE_USER_DATA_DIR` で上書きできる。

## 次のステップ

- [概念リファレンス](../concepts/README.md) - 各概念の実装入口
- [ツールの追加](./adding-tools.md) - 新しいツールの実装
- [Playbook 作成](./creating-playbooks.md) - 独自 Playbook の作成
- [俯瞰地図](../overview/landscape.md) - 概念どうしの関係
