# Tool

> 開発者向け概念リファレンス。**全体の位置づけ**は [landscape §4](../overview/landscape.md)、**作り方**は [開発者ガイド: ツールの追加](../developer-guide/adding-tools.md)、**一覧**は [リファレンス: ツールカタログ](../reference/tool-catalog.md) を参照。

## 一言で

ペルソナが実行できる処理の単位（`tools/` registry に登録された関数 + schema）。

## 役割

計算・画像生成・アイテム操作・タスク管理・メモリ検索など、LLM の外で実際に「何かをする」処理を担う。ペルソナは [Playbook](playbook.md) の TOOL ノード、または [Spell](spell.md) 構文（平文応答内）から Tool を呼ぶ。

## 仕組み

### 登録とロード

- **Registry**: `tools/__init__.py` が `TOOL_REGISTRY` dict（function_name → schema + callable）を公開
- **ロード元（3層優先）**: `~/.saiverse/user_data/tools/` > `expansion_data/<addon>/tools/` > `builtin_data/tools/`
- **サブディレクトリ**: `schema.py` を置けばサブディレクトリでも登録される（git clone したツールパック等）
- **Context 注入**: `tools/context.py` が contextvars でペルソナ/マネージャ参照を実行時に注入

### Spell 化

schema に `spell=True`（+ `spell_display_name`）を設定すると、その Tool は平文応答から `/spell` 構文で呼べるようになる（→ [Spell](spell.md)）。

### 戻り値の形式（重要）

ネイティブツールは **`str` か `(str, dict)` の2形式で return する**。4-tuple 等は `str()` 化されて壊れる（→ issue `native_tool_return_4tuple_bug.md`）。戻り値テキストはキャラ付けせず、客観 + 丁寧語で書く（例: 「温度: 32.3°C」）。

## 増やし方

1. `builtin_data/tools/`（または `~/.saiverse/user_data/tools/`）に `schema()` + 同名 callable で定義
2. サブディレクトリなら `schema.py`（`schemas()`）を置く
3. 起動時に自動登録される
4. ペルソナに使わせるには **Spell 化**（`spell=True`）するか、**Playbook の TOOL ノード**で実行する（旧 `BuildingToolLink` 方式は現在未使用）

詳細は[開発者ガイド: ツールの追加](../developer-guide/adding-tools.md)。

## 実装

- Registry: `tools/__init__.py`（`TOOL_REGISTRY`）、`tools/core.py`
- Context 注入: `tools/context.py`
- ビルトイン定義: `builtin_data/tools/`
- 代表例: `calculator.py`（AST 式評価）/ `image_generator.py`（Gemini Image）/ `item_*.py` / `task_*.py` / `memory_recall.py` / `run_playbook.py`

## 関連概念

- [Spell](spell.md) — 平文応答から Tool を呼ぶ構文
- [Playbook](playbook.md) — TOOL ノードで Tool を固定実行
- [Addon](addon.md) — Tool を配布・導入する単位
- [MCP](mcp.md) — 外部サーバーの Tool を取り込む

## 参照

- 開発者ガイド: [`adding-tools.md`](../developer-guide/adding-tools.md)
- リファレンス: [`tool-catalog.md`](../reference/tool-catalog.md)
- 地図: [`landscape.md`](../overview/landscape.md) §4
