# Playbook

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §4](../overview/landscape.md)、**作り方**は [開発者ガイド: Playbook作成](../developer-guide/creating-playbooks.md) を参照。

## 一言で

LLM / tool / speak ノードのグラフで、条件分岐・反復が組める構造化された行動フロー。

## 役割

ペルソナの1回の [Pulse](pulse.md) は Playbook を1つ実行する。「ユーザー入力を捌く」「自律稼働する」「メモリを検索する」といった振る舞いを、LLM ノードとツール実行ノードの有向グラフとして宣言的に定義する。JSON ファイルまたは DB の `playbooks` テーブルに格納される。

## 仕組み

### メタ Playbook

Pulse の入口となる2つの Playbook がある:

- **`meta_user`** 系 — ユーザー入力を捌く（`track_user_conversation.json` 等）
- **`meta_auto`** 系 — 自律 Pulse を捌く（[Meta-Judgment](meta-judgment.md) の `meta_judgment*.json`、時間割の判断点 `judgment_*.json` 等。v1 の `track_autonomous.json` は時間割移行で退役済み）

### ノードの種類（主要）

- **LLM ノード** — LLM を呼ぶ。`response_schema` で構造化出力を指定できる。使うモデルの重量級/軽量はそのラインの [aspect](line.md) から導出される（ノード個別の `model_type` 指定は廃止）
- **TOOL ノード** — [Tool](tool.md) を固定実行。`args_input` で state 変数をツール引数にマッピング
- **MEMORIZE ノード** — 結果を [SAIMemory](saimemory.md) に保存
- **speak ノード** — 発話（[Beat](beat.md) を生む）

### 設計哲学（重要）

- **メタ判断の Playbook 選択は決定論的**: どの `meta_judgment_*` を走らせるかは MetaLayer が Track / persona 状態から決める（`_SITUATION_PLAYBOOK_MAP`）。軽量 LLM ルーターは存在しない（廃止された）
- **引数は Playbook 内で決める**: 各 Playbook が、利用可能なコンテキスト（インベントリ・建物アイテム・会話履歴等）を見て tool 引数を決める LLM ノードを持つ
- **function calling を使わない**: ネイティブツールコールはキャッシュを壊す。structured output + tool ノード固定実行が正道（→ [Spell](spell.md) の目的）
- **リファレンス実装**: `generate_image_playbook.json`（`decide_prompt` LLM(response_schema) → `generate` TOOL(args_input) → `record` MEMORIZE の3段）

### サブラインとしての起動

メインライン LLM が発話中に `/spell run_playbook name='...'` を書くと、指定 Playbook が **サブライン**として動的に起動される（[Spell](spell.md) × Playbook の接続点）。`router_callable=true` の Playbook のみ呼べる。入れ子は最大4段。

## 増やし方 / 変更のしかた

1. `builtin_data/playbooks/`（または `~/.saiverse/user_data/playbooks/`）の JSON を編集
2. **新しいノードフィールドを足す場合は `sea/playbook_models.py` の node 定義（`LLMNodeDef` / `ToolNodeDef` 等）も更新する**（しないと Pydantic 検証で黙って落ちる）
3. `python scripts/import_playbook.py --file <path>` で DB に取り込む
4. `next` ポインタが有効な DAG（意図しないループなし）か検証する
5. DB 反映を確認: `sqlite3 ... "SELECT nodes_json FROM playbooks WHERE name='<name>'"`

> `save_playbook` ツール（グラフを検証してから保存）を使う手もある。

## 実装

- 実行ランタイム: `sea/runtime.py` / `sea/runtime_llm.py`（SEARuntime、LangGraph ベース）
- ノード定義スキーマ: `sea/playbook_models.py`
- ビルトイン: `builtin_data/playbooks/public/*.json`
- インポート: `scripts/import_playbook.py` / `scripts/import_all_playbooks.py`

## 関連概念

- [Pulse](pulse.md) — Playbook を実行する駆動単位
- [Beat](beat.md) — 発話ノードの出力が Beat になる
- [Spell](spell.md) — `run_playbook` Spell が Playbook をサブライン起動する
- [Tool](tool.md) — TOOL ノードが呼ぶ実行単位
- [Meta-Judgment](meta-judgment.md) — メタ Playbook として実装される

## 参照

- 開発者ガイド: [`creating-playbooks.md`](../developer-guide/creating-playbooks.md)
- 地図: [`landscape.md`](../overview/landscape.md) §4
